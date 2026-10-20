"""The worker's jobs: run ONE PreTeXt command -- `pretext build` for a build
job, `pretext generate` for an asset-generation job -- inside a locked-down,
ephemeral sibling container, then package the output.

This module runs in the *worker* processes, the only components with access
to the Docker socket. The container they spawn has no socket, no network,
dropped capabilities, and hard resource/time limits -- so even though it runs
user-submitted source (which can execute code via Sage/LaTeX/Asymptote), it
can't reach the host or the rest of the system.

The two job kinds share everything except the command, the limits, which
project directory becomes the artifact (see Sandbox), and how they meet the
project's persistent asset cache when the job names a project_id: a build
mounts it, a generate job is harvested into it (see asset_cache.py).
"""
import json
import logging
import os
import shlex
import shutil
import time
import zipfile
from dataclasses import dataclass

import docker
from docker.errors import ImageNotFound, APIError
from requests.exceptions import ReadTimeout, ConnectionError as ReqConnectionError

from . import asset_cache
from .config import settings
from .jobs import store
from .notify import send_callback

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sandbox:
    """Resource limits for one kind of job, plus which directory of the
    project (relative to its root) is zipped as the artifact."""

    mem_limit: str
    cpus: float
    pids_limit: int
    timeout: int
    output_subdir: str
    # Leave top-level dotfiles out of the artifact. The CLI keeps its
    # per-target asset hash table (.web_assets.json) in the cache root; it is
    # bookkeeping for `pretext generate --only-changed`, not an asset.
    skip_top_level_dotfiles: bool = False


# Builds get the big limits. Generate jobs render a few assets in seconds and
# run on their own queue/worker, so they get a smaller slice that fits
# alongside a running build on the same host.
BUILD_SANDBOX = Sandbox(
    mem_limit=settings.build_mem_limit,
    cpus=settings.build_cpus,
    pids_limit=settings.build_pids_limit,
    timeout=settings.build_timeout,
    output_subdir=settings.output_subdir,
)
GENERATE_SANDBOX = Sandbox(
    mem_limit=settings.generate_mem_limit,
    cpus=settings.generate_cpus,
    pids_limit=settings.generate_pids_limit,
    timeout=settings.generate_timeout,
    output_subdir=settings.generate_output_subdir,
    skip_top_level_dotfiles=True,
)


def _now() -> str:
    return f"{time.time():.0f}"


def _zip_dir(src_dir: str, zip_path: str, skip_top_level_dotfiles: bool = False) -> list[str]:
    """Zip a directory tree. Returns the archived paths, relative and sorted.

    Symlinks are skipped, and os.walk never descends into symlinked dirs. The
    tree was written by user code running as root in the sandbox, which can
    plant a link to anything the worker can read (another job's project,
    /proc/1/environ with this process's secrets); following it would copy
    that into a downloadable artifact."""
    names = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(src_dir):
            for name in files:
                if skip_top_level_dotfiles and root == src_dir and name.startswith("."):
                    continue
                full = os.path.join(root, name)
                if os.path.islink(full):
                    logger.warning("skipping symlink in job output: %s", full)
                    continue
                rel = os.path.relpath(full, src_dir)
                z.write(full, rel)
                names.append(rel)
    return sorted(names)


def _prune_expired_jobs(current_job_id: str) -> None:
    """Delete job dirs whose Redis record has expired.

    The record's TTL is set once, at submission (Store.create), and the job dir
    is created at the same moment, so a dir untouched for longer than job_ttl
    belongs to a job the API already 404s on. Runs in the worker because build
    containers run as root and leave root-owned files the host user can't
    remove. Best effort: a failure here never affects a job."""
    cutoff = time.time() - settings.job_ttl
    try:
        with os.scandir(os.path.join(settings.data_dir, "jobs")) as entries:
            for entry in entries:
                if entry.name == current_job_id or not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    shutil.rmtree(entry.path, ignore_errors=True)
                    logger.info("pruned expired job dir %s", entry.name)
    except OSError as e:
        logger.warning("pruning expired job dirs failed: %s", e)


class _Cancelled(Exception):
    """Raised internally when a cancel request is noticed mid-job."""


def _wait_for_container(job_id: str, container, timeout: int) -> dict:
    """Wait for the container to finish, polling in short increments instead
    of one long blocking call so a cancel request or the overall timeout can
    be noticed promptly.

    Re-raises the underlying ReadTimeout/ConnectionError once the *total*
    elapsed wait reaches `timeout`; raises _Cancelled if a cancel request
    lands first.
    """
    elapsed = 0
    poll = settings.build_poll_interval
    while True:
        try:
            return container.wait(timeout=poll)
        except (ReadTimeout, ReqConnectionError):
            elapsed += poll
            if elapsed >= timeout:
                raise
            if (store.get(job_id) or {}).get("cancel_requested") == "1":
                raise _Cancelled()


def run_build(job_id: str, target: str) -> None:
    """RQ entry point for a build job (queue "builds")."""
    command = settings.build_command.format(target=shlex.quote(target))
    _run_job(job_id, "build", command, BUILD_SANDBOX)


def run_generate(job_id: str, target: str, assets: list[str]) -> None:
    """RQ entry point for an asset-generation job (queue "generate").

    `assets` is the list of asset types to generate (e.g. ["latex-image"]);
    empty means every type the CLI finds in the source."""
    command = settings.generate_command.format(
        target=shlex.quote(target),
        assets=" ".join(shlex.quote(a) for a in assets),
    )
    # The artifact lists its files: a generate job's cache holds a few files
    # per asset, so the list is small enough to ride along in the status and
    # callback, and it is how the caller learns each asset's cache key.
    _run_job(job_id, "generate", command, GENERATE_SANDBOX, list_files=True)


def _run_job(job_id: str, kind: str, command: str, sandbox: Sandbox, list_files: bool = False) -> None:
    """Run the job, then fire the completion callback regardless of outcome.

    The callback lives in a `finally` so every terminal path -- success,
    failure, timeout, misconfig -- notifies the initiating host exactly once,
    and a failing callback never propagates back into the job's status."""
    logger.info("%s job %s: starting, command=%r", kind, job_id, command)
    try:
        _run_sandboxed(job_id, kind, command, sandbox, list_files)
    finally:
        data = store.get(job_id) or {}
        logger.info("%s job %s: finished with status=%s, sending callback", kind, job_id, data.get("status"))
        send_callback(job_id)
        # After the callback, so a large deletion never delays the caller.
        _prune_expired_jobs(job_id)
        try:
            asset_cache.prune(keep_project_id=data.get("project_id") or None)
        except Exception as e:  # best effort, like the job pruning above
            logger.warning("pruning the asset cache failed: %s", e)


def _run_sandboxed(job_id: str, kind: str, command: str, sandbox: Sandbox, list_files: bool) -> None:
    if not settings.host_data_dir:
        store.update(
            job_id,
            status="failed",
            finished_at=_now(),
            log="Server misconfigured: HOST_DATA_DIR is unset, so job dirs "
            "cannot be mounted into build containers. See README.",
        )
        return

    data = store.get(job_id) or {}
    if data.get("cancel_requested") == "1":
        store.update(job_id, status="cancelled", finished_at=_now(), log=f"{kind} cancelled before it started.")
        return
    # Validated by the API before it was stored; the id names a dir on the host.
    project_id = data.get("project_id") or None

    store.update(job_id, status="running", started_at=_now())

    job_dir = os.path.join(settings.data_dir, "jobs", job_id)
    work_dir = os.path.join(job_dir, "work")
    # Host-side path of the same dir, for the sibling container's bind mount.
    host_work = os.path.join(settings.host_data_dir, "jobs", job_id, "work")

    # The container may run as a non-root user; make the project root writable
    # so it can create the output/ and generated-asset directories.
    os.chmod(work_dir, 0o777)

    volumes = {host_work: {"bind": "/work", "mode": "rw"}}
    if project_id:
        try:
            asset_cache.touch(project_id)
            if kind == "build":
                # Cache files the caller shipped inside the archive would be
                # hidden by the mount, so promote them into the persistent
                # cache first.
                seeded = asset_cache.harvest(project_id, work_dir)
                if seeded:
                    logger.info("%s job %s: %d cache file(s) from the archive added to project %s",
                                kind, job_id, len(seeded), project_id)
                volumes.update(asset_cache.mounts(project_id, work_dir))
        except Exception as e:
            # The cache is an optimization: run the job without it rather
            # than fail it. (A generate job's later harvest is guarded too.)
            logger.warning("%s job %s: asset cache for project %s unavailable, running without: %s",
                           kind, job_id, project_id, e)
            volumes = {host_work: {"bind": "/work", "mode": "rw"}}

    run_kwargs = dict(
        image=settings.build_image,
        command=["sh", "-c", command],
        detach=True,
        working_dir="/work",
        volumes=volumes,
        network_mode=settings.build_network,
        mem_limit=sandbox.mem_limit,
        nano_cpus=int(sandbox.cpus * 1_000_000_000),
        pids_limit=sandbox.pids_limit,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
    )
    if settings.build_user:
        run_kwargs["user"] = settings.build_user
    # Only override HOME if explicitly configured; otherwise keep the image's
    # HOME so the baked ~/.ptx is found.
    if settings.build_home:
        run_kwargs["environment"] = {"HOME": settings.build_home}

    client = docker.from_env()
    container = None
    try:
        container = client.containers.run(**run_kwargs)
        try:
            result = _wait_for_container(job_id, container, sandbox.timeout)
            exit_code = int(result.get("StatusCode", 1))
        except _Cancelled:
            container.kill()
            logs = container.logs().decode(errors="replace")
            store.update(
                job_id,
                status="cancelled",
                finished_at=_now(),
                log=logs + f"\n\n[{kind} cancelled by request]",
            )
            return
        except (ReadTimeout, ReqConnectionError):
            container.kill()
            logs = container.logs().decode(errors="replace")
            store.update(
                job_id,
                status="failed",
                finished_at=_now(),
                log=logs + f"\n\n[{kind} exceeded {sandbox.timeout}s and was killed]",
            )
            return
        logs = container.logs().decode(errors="replace")
    except ImageNotFound:
        store.update(
            job_id,
            status="failed",
            finished_at=_now(),
            log=f"Build image not found locally: {settings.build_image}. "
            f"Run `docker pull {settings.build_image}`.",
        )
        return
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except APIError:
                pass

    # Zip whatever output exists regardless of exit code: a nonzero exit
    # doesn't always mean the job produced nothing usable (e.g. one target
    # in a multi-target build failed, or one of several assets failed to
    # generate after the others were written). Callers can tell success from
    # a failure-with-output by status; the artifact is offered either way.
    output_dir = os.path.join(work_dir, sandbox.output_subdir)
    # A symlinked output dir would be the same trick as above, one level up.
    has_output = os.path.isdir(output_dir) and not os.path.islink(output_dir)
    extra = {}
    if has_output:
        names = _zip_dir(output_dir, os.path.join(job_dir, "output.zip"), sandbox.skip_top_level_dotfiles)
        if list_files:
            extra["files"] = json.dumps(names)

    # A generate job has no cache mount (its artifact must be only what it
    # made), so copy its results into the project's cache now. Only after a
    # clean exit: a killed container can leave a half-written file behind.
    if project_id and kind == "generate" and exit_code == 0:
        try:
            cached = asset_cache.harvest(project_id, work_dir)
            extra["cached_files"] = json.dumps(cached)
        except Exception as e:  # the job itself still succeeded
            logger.warning("%s job %s: caching results for project %s failed: %s", kind, job_id, project_id, e)

    if exit_code != 0:
        log = logs
        if has_output:
            log += (
                f"\n\n[{kind} exited with code {exit_code}, but produced output in "
                f"'{sandbox.output_subdir}/' -- see artifact_url]"
            )
        store.update(job_id, status="failed", finished_at=_now(), log=log, exit_code=exit_code, **extra)
        return

    if not has_output:
        store.update(
            job_id,
            status="failed",
            finished_at=_now(),
            exit_code=exit_code,
            log=logs + f"\n\n[{kind} succeeded but produced no '{sandbox.output_subdir}/' directory]",
        )
        return

    store.update(job_id, status="success", finished_at=_now(), log=logs, exit_code=exit_code, **extra)
