"""Runtime configuration, all driven by environment variables.

The build backend (image + command) is intentionally swappable so the same
code path can run a fast `alpine` "fake build" locally or the real
`pretextbook/pretext-full` image in production. See .env.example.
"""
import os


class Settings:
    # --- Auth ---
    # Shared secret with pretext.plus. Mirrors the lightweight server's
    # BUILD_TOKEN scheme so both servers use one credential.
    build_token = os.getenv("BUILD_TOKEN", "")

    # --- Redis / queue ---
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")

    # --- Build backend (swappable) ---
    build_image = os.getenv("BUILD_IMAGE", "pretextbook/pretext-full")
    # `{target}` is substituted with the (shell-quoted) requested target name.
    build_command = os.getenv("BUILD_COMMAND", "pretext build {target}")

    # --- Sandbox limits applied to every build container ---
    build_network = os.getenv("BUILD_NETWORK", "none")
    build_mem_limit = os.getenv("BUILD_MEM_LIMIT", "2g")
    build_cpus = float(os.getenv("BUILD_CPUS", "2"))
    build_pids_limit = int(os.getenv("BUILD_PIDS_LIMIT", "512"))
    build_timeout = int(os.getenv("BUILD_TIMEOUT", "600"))  # seconds
    # User the build runs as. Empty = the image's default user. This MUST match
    # the user whose home holds the baked ~/.ptx in the warm image (root by
    # default), or PreTeXt re-does its first-run setup every build.
    build_user = os.getenv("BUILD_USER", "")
    # HOME for the build. Empty = don't override (use the image's HOME, where
    # ~/.ptx was baked). Only set this if you know the home you want.
    build_home = os.getenv("BUILD_HOME", "")
    # Directory (relative to the project root) whose contents are zipped as the
    # artifact after a successful build.
    output_subdir = os.getenv("OUTPUT_SUBDIR", "output")

    # --- Completion callback (optional webhook) ---
    # If a /builds request includes a callback_url, the worker POSTs the final
    # job status there once the build reaches a terminal state (success/failed).
    # Seconds to wait for the callback POST before giving up.
    callback_timeout = int(os.getenv("CALLBACK_TIMEOUT", "10"))
    # How many times to attempt the callback POST (>=1).
    callback_retries = int(os.getenv("CALLBACK_RETRIES", "3"))
    # Optional comma-separated allowlist of hostnames permitted as callback
    # targets. Empty = allow any *public* host (callback_url is only reachable
    # by token holders). Set this in production to pin it, e.g. "pretext.plus".
    callback_allowed_hosts = [
        h.strip().lower().rstrip(".")
        for h in os.getenv("CALLBACK_ALLOWED_HOSTS", "").split(",")
        if h.strip()
    ]
    # SSRF guard: by default, callback URLs that resolve to loopback/private/
    # link-local/reserved IPs are rejected. Set true ONLY for local dev where
    # the receiver is on localhost or a private network.
    callback_allow_private_ips = os.getenv("CALLBACK_ALLOW_PRIVATE_IPS", "").lower() in ("1", "true", "yes")
    # Secret used to HMAC-sign the callback payload (sent as an X-PreTeXt-
    # Signature header) so the receiver can verify authenticity. The secret
    # itself is NEVER transmitted. Falls back to BUILD_TOKEN if unset, but a
    # dedicated value is preferred so the callback path and the submit path
    # don't share one credential.
    callback_secret = os.getenv("CALLBACK_SECRET", "") or build_token

    # --- Storage ---
    # Path *inside* the api/worker containers where job data lives.
    data_dir = os.getenv("DATA_DIR", "/data")
    # Absolute path of the same directory *on the host*. Required by the worker
    # to bind-mount per-job dirs into sibling build containers (Docker-out-of-
    # Docker translates container paths -> host paths). `make up` sets this.
    host_data_dir = os.getenv("HOST_DATA_DIR", "")
    # How long (seconds) job status + artifacts are retained.
    job_ttl = int(os.getenv("JOB_TTL", "86400"))


settings = Settings()
