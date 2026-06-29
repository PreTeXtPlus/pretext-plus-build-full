"""The build job: runs ONE PreTeXt build inside a locked-down, ephemeral
sibling container, then packages the output.

This module runs in the *worker* process, the only component with access to
the Docker socket. The build container it spawns has no socket, no network,
dropped capabilities, and hard resource/time limits — so even though it runs
user-submitted source (which can execute code via Sage/LaTeX), it can't reach
the host or the rest of the system.
"""
import os
import shlex
import time
import zipfile

import docker
from docker.errors import ImageNotFound, APIError
from requests.exceptions import ReadTimeout, ConnectionError as ReqConnectionError

from .config import settings
from .jobs import store
from .notify import send_callback


def _now() -> str:
    return f"{time.time():.0f}"


def _zip_dir(src_dir: str, zip_path: str) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(src_dir):
            for name in files:
                full = os.path.join(root, name)
                z.write(full, os.path.relpath(full, src_dir))


def run_build(job_id: str, target: str) -> None:
    """Run the build, then fire the completion callback regardless of outcome.

    The callback lives in a `finally` so every terminal path — success, build
    failure, timeout, misconfig — notifies the initiating host exactly once,
    and a failing callback never propagates back into the build's status."""
    try:
        _run_build(job_id, target)
    finally:
        send_callback(job_id)


def _run_build(job_id: str, target: str) -> None:
    if not settings.host_data_dir:
        store.update(
            job_id,
            status="failed",
            finished_at=_now(),
            log="Server misconfigured: HOST_DATA_DIR is unset, so build dirs "
            "cannot be mounted into build containers. See README.",
        )
        return

    store.update(job_id, status="running", started_at=_now())

    job_dir = os.path.join(settings.data_dir, "jobs", job_id)
    work_dir = os.path.join(job_dir, "work")
    # Host-side path of the same dir, for the sibling container's bind mount.
    host_work = os.path.join(settings.host_data_dir, "jobs", job_id, "work")

    # The build runs as a non-root user; make the project root writable so it
    # can create the output/ and generated-asset directories.
    os.chmod(work_dir, 0o777)

    command = settings.build_command.format(target=shlex.quote(target))
    run_kwargs = dict(
        image=settings.build_image,
        command=["sh", "-c", command],
        detach=True,
        working_dir="/work",
        volumes={host_work: {"bind": "/work", "mode": "rw"}},
        network_mode=settings.build_network,
        mem_limit=settings.build_mem_limit,
        nano_cpus=int(settings.build_cpus * 1_000_000_000),
        pids_limit=settings.build_pids_limit,
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
            result = container.wait(timeout=settings.build_timeout)
            exit_code = int(result.get("StatusCode", 1))
        except (ReadTimeout, ReqConnectionError):
            container.kill()
            logs = container.logs().decode(errors="replace")
            store.update(
                job_id,
                status="failed",
                finished_at=_now(),
                log=logs + f"\n\n[build exceeded {settings.build_timeout}s and was killed]",
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

    if exit_code != 0:
        store.update(job_id, status="failed", finished_at=_now(), log=logs)
        return

    output_dir = os.path.join(work_dir, settings.output_subdir)
    if not os.path.isdir(output_dir):
        store.update(
            job_id,
            status="failed",
            finished_at=_now(),
            log=logs + f"\n\n[build succeeded but produced no '{settings.output_subdir}/' directory]",
        )
        return

    _zip_dir(output_dir, os.path.join(job_dir, "output.zip"))
    store.update(job_id, status="success", finished_at=_now(), log=logs)
