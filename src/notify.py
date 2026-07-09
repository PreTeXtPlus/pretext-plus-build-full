"""Optional build-completion webhook.

When a `/builds` request carries a `callback_url`, the worker calls
`send_callback()` once the build reaches a terminal state (success or failed).
This pushes the result to the initiating host (pretext.plus) so it need not
poll `GET /builds/{id}`.

Security model — the callback URL is supplied by whoever submits the build, so
it is treated as untrusted even though submitting requires the build token:
- SSRF guard: the URL must be http(s), and its hostname must NOT resolve to a
  loopback/private/link-local/reserved address (unless explicitly allowed for
  local dev). Re-checked at send time too, to defeat DNS rebinding.
- Redirects are disabled, so a 30x can't bounce the request (and any header)
  to an unintended origin.
- We never transmit a reusable credential. Instead the payload is HMAC-signed
  with CALLBACK_SECRET; the receiver recomputes the HMAC to authenticate.
- Best-effort: a failing callback is recorded on the job, never raised, so it
  can't turn a successful build into a failed one.
"""
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
from urllib.parse import urlparse

import requests

from .config import settings
from .jobs import store

logger = logging.getLogger(__name__)


def _resolves_to_blocked_ip(hostname: str) -> bool:
    """True if the hostname resolves to ANY non-public address. Checks every
    A/AAAA record so a host that mixes a public and a loopback record can't
    sneak the internal one through."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return True  # unresolvable -> treat as blocked (fail closed)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_unspecified
            or ip.is_multicast
        ):
            logger.warning(
                "callback host %r resolves to non-public address %s -- blocked "
                "(set CALLBACK_ALLOW_PRIVATE_IPS=true for local dev)",
                hostname, ip,
            )
            return True
    return False


def is_allowed_callback_url(url: str) -> bool:
    """Validate a callback target: http(s) only, on the allowlist if one is set,
    and not pointed at an internal address. Used both at submit time (fast 422
    to the client) and again at send time (DNS-rebinding defense)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        logger.warning("callback_url %r could not be parsed", url)
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in ("http", "https") or not host:
        logger.warning("callback_url %r has no http(s) scheme or hostname", url)
        return False
    if settings.callback_allowed_hosts and host not in settings.callback_allowed_hosts:
        logger.warning(
            "callback host %r is not in CALLBACK_ALLOWED_HOSTS=%s",
            host, settings.callback_allowed_hosts,
        )
        return False
    if not settings.callback_allow_private_ips and _resolves_to_blocked_ip(host):
        return False
    return True


def _build_payload(job_id: str, data: dict) -> dict:
    """Construct the JSON body POSTed to the callback URL.

    TODO(oscar): shape this to whatever pretext.plus wants to act on. `data` is
    the full job hash from Redis (status, target, timestamps, log, and the
    callback_url itself). Think about: which fields does the receiver actually
    need, what should it NOT receive (the raw build log can be large — send it,
    truncate it, or just a URL?), and how does it fetch the artifact on success?
    """
    payload = {
        "job_id": job_id,
        "status": data.get("status"),
        "target": data.get("target"),
    }
    if data.get("status") == "success":
        payload["artifact_url"] = f"/builds/{job_id}/artifact"
    return payload


def _sign(body: bytes) -> str:
    """HMAC-SHA256 of the raw request body, hex-encoded. The receiver recomputes
    this over the bytes it received to authenticate the callback. The secret is
    never sent on the wire."""
    return hmac.new(settings.callback_secret.encode(), body, hashlib.sha256).hexdigest()


def send_callback(job_id: str) -> None:
    """Best-effort, signed POST of the final job status to its callback_url."""
    data = store.get(job_id)
    if not data:
        logger.warning("send_callback(%s): no job record found, skipping", job_id)
        return
    url = data.get("callback_url")
    if not url:
        logger.debug("send_callback(%s): no callback_url set, skipping", job_id)
        return

    # Re-validate at send time: DNS may have rebound since submit, and the
    # allowlist/IP rules are the authoritative gate right before the request.
    if not is_allowed_callback_url(url):
        logger.warning("send_callback(%s): callback_url %r blocked at send time", job_id, url)
        store.update(job_id, callback_status="blocked", callback_error="callback_url failed validation at send time")
        return

    payload = _build_payload(job_id, data)
    logger.debug("send_callback(%s): payload=%s", job_id, payload)
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if settings.callback_secret:
        headers["X-PreTeXt-Signature"] = f"sha256={_sign(body)}"

    retries = max(1, settings.callback_retries)
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            logger.info(
                "send_callback(%s): POST %s (attempt %d/%d, status=%s)",
                job_id, url, attempt, retries, data.get("status"),
            )
            resp = requests.post(
                url,
                data=body,
                headers=headers,
                timeout=settings.callback_timeout,
                allow_redirects=False,
            )
            resp.raise_for_status()
            logger.info("send_callback(%s): delivered, HTTP %d", job_id, resp.status_code)
            store.update(job_id, callback_status="delivered")
            return
        except requests.RequestException as e:
            last_error = e
            logger.warning("send_callback(%s): attempt %d/%d failed: %s", job_id, attempt, retries, e)
    logger.error("send_callback(%s): giving up after %d attempt(s): %s", job_id, retries, last_error)
    store.update(job_id, callback_status="failed", callback_error=str(last_error))
