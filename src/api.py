"""HTTP API for the full build server.

Two job kinds share one pipeline -- upload a project archive, queue a job,
poll its status, fetch the artifact -- and differ only in what the worker
runs and which queue (hence which worker) runs it:

    /builds    -> `pretext build <target>`                 queue "builds"
    /generate  -> `pretext generate <assets> -t <target>`  queue "generate"

Async by design: a build (LaTeX + Sage) can take minutes, so POST returns a
job id immediately and the client polls GET, or supplies a callback_url. This
is the deliberate sibling of the lightweight server, which returns rendered
output inline because its snippet builds are sub-second.
"""
import json
import logging
import mimetypes
import os
import re
import shutil
import tarfile
import time
import uuid
import zipfile

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from rq.exceptions import InvalidJobOperation, NoSuchJobError
from rq.job import Job

from .config import settings
from .jobs import JOB_PREFIXES, build_queue, generate_queue, store
from .notify import is_allowed_callback_url

logger = logging.getLogger(__name__)

app = FastAPI(title="PreTeXt Plus — Full Build Server")

TERMINAL_STATUSES = {"success", "failed", "cancelled"}

# Asset type names become positional arguments to `pretext generate`, which
# validates them against its own list (latex-image, asymptote, sageplot,
# prefigure, ...). The API only keeps them shell-safe, so it needn't track
# the CLI's list.
_ASSET_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")


def _now() -> str:
    return f"{time.time():.0f}"


def _check_token(form_token: str | None, authorization: str | None) -> None:
    token = form_token
    if token is None and authorization:
        token = authorization[7:] if authorization.lower().startswith("bearer ") else authorization
    if not settings.build_token or token != settings.build_token:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


def _safe_extract(archive_path: str, dest: str, filename: str | None) -> None:
    """Extract a .zip or .tar.gz project archive, guarding against path
    traversal. (Defense in depth — the job itself is also sandboxed.)"""
    dest_root = os.path.realpath(dest)
    if (filename or "").lower().endswith(".zip") or zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as z:
            for member in z.namelist():
                target = os.path.realpath(os.path.join(dest, member))
                if target != dest_root and not target.startswith(dest_root + os.sep):
                    raise ValueError(f"unsafe path in archive: {member}")
            z.extractall(dest)
    elif tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path) as t:
            t.extractall(dest, filter="data")  # Python 3.12+ traversal filter
    else:
        raise ValueError("unsupported archive: use .zip or .tar.gz")


def _job_dir(job_id: str) -> str:
    return os.path.join(settings.data_dir, "jobs", job_id)


async def _intake(archive: UploadFile, callback_url: str | None, **fields) -> str:
    """Validate the callback, unpack the archive into a fresh job dir, and
    create the job record. Returns the new job id. Shared by both kinds."""
    if callback_url is not None and not is_allowed_callback_url(callback_url):
        # is_allowed_callback_url already logged the specific reason.
        raise HTTPException(status_code=422, detail="Invalid or disallowed callback_url")

    job_id = uuid.uuid4().hex
    logger.info("create %s: job=%s %s callback_url=%s", fields.get("kind"), job_id,
                " ".join(f"{k}={v!r}" for k, v in fields.items() if k != "kind"), callback_url or "<none>")
    job_dir = _job_dir(job_id)
    work_dir = os.path.join(job_dir, "work")
    os.makedirs(work_dir, exist_ok=True)

    archive_path = os.path.join(job_dir, "upload")
    with open(archive_path, "wb") as f:
        f.write(await archive.read())
    try:
        _safe_extract(archive_path, work_dir, archive.filename)
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=f"Could not extract archive: {e}")
    os.remove(archive_path)

    if callback_url:
        fields["callback_url"] = callback_url
    store.create(job_id, **fields)
    return job_id


def _queued(job_id: str, kind: str) -> dict:
    return {"job_id": job_id, "status": "queued", "status_url": f"{JOB_PREFIXES[kind]}/{job_id}"}


def _load(job_id: str, kind: str) -> dict:
    """The job record, or 404 -- also when the id belongs to the other kind,
    so /generate/<build id> doesn't answer. Records written before generate
    jobs existed carry no kind; they are builds."""
    data = store.get(job_id)
    if data is None or data.get("kind", "build") != kind:
        raise HTTPException(status_code=404, detail="Unknown job")
    return data


def _status(job_id: str, kind: str) -> dict:
    data = _load(job_id, kind)
    prefix = JOB_PREFIXES[kind]
    resp = {"job_id": job_id, **data}
    if "files" in data:
        resp["files"] = json.loads(data["files"])
    if data.get("log") is not None:
        resp["log_url"] = f"{prefix}/{job_id}/log"
    if os.path.isfile(os.path.join(_job_dir(job_id), "output.zip")):
        resp["artifact_url"] = f"{prefix}/{job_id}/artifact"
    return resp


def _log(job_id: str, kind: str) -> PlainTextResponse:
    """The full, untruncated log (combined stdout+stderr) as plain text.

    The callback payload carries only a truncated tail; this is where the
    receiver fetches the whole thing. Present as soon as the job reaches a
    terminal state; empty while still queued/running."""
    data = _load(job_id, kind)
    return PlainTextResponse(data.get("log") or "")


def _artifact(job_id: str, kind: str) -> FileResponse:
    data = _load(job_id, kind)
    if data.get("status") not in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"Job not finished (status={data.get('status')})")
    zip_path = os.path.join(_job_dir(job_id), "output.zip")
    if not os.path.isfile(zip_path):
        raise HTTPException(status_code=404, detail="Artifact missing, expired, or job produced no output")
    return FileResponse(zip_path, media_type="application/zip", filename=f"{job_id}-output.zip")


def _cancel(job_id: str, kind: str) -> dict:
    data = _load(job_id, kind)
    if data.get("status") in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"Job already finished (status={data.get('status')})")

    # Flag first: this is what a job that's already running (or about to
    # start) checks to stop itself — see the poll loop and pre-run check in
    # build.py. Set unconditionally so it's in place before the queued-job
    # fast path below, which only saves the RQ dequeue if the job is still
    # sitting in the queue.
    store.update(job_id, cancel_requested="1")
    logger.info("cancel %s job %s: cancel requested", kind, job_id)

    if data.get("status") == "queued":
        try:
            Job.fetch(job_id, connection=build_queue.connection).cancel()
            store.update(job_id, status="cancelled", finished_at=_now())
            return {"job_id": job_id, "status": "cancelled"}
        except (NoSuchJobError, InvalidJobOperation):
            # Already picked up by a worker (or already gone) between our status
            # read and here; the cancel_requested flag above still covers it.
            pass

    return {"job_id": job_id, "status": "cancel_requested"}


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- /builds ---

@app.post("/builds", status_code=202)
async def create_build(
    archive: UploadFile = File(...),
    target: str = Form(...),
    token: str | None = Form(None),
    callback_url: str | None = Form(None),
    authorization: str | None = Header(None),
):
    _check_token(token, authorization)
    job_id = await _intake(archive, callback_url, kind="build", target=target)
    build_queue.enqueue("src.build.run_build", job_id, target, job_id=job_id)
    return _queued(job_id, "build")


@app.post("/builds/{job_id}/cancel")
def cancel_build(job_id: str, token: str | None = Form(None), authorization: str | None = Header(None)):
    _check_token(token, authorization)
    return _cancel(job_id, "build")


@app.get("/builds/{job_id}")
def get_build(job_id: str):
    return _status(job_id, "build")


@app.get("/builds/{job_id}/log")
def get_build_log(job_id: str):
    return _log(job_id, "build")


@app.get("/builds/{job_id}/artifact")
def get_build_artifact(job_id: str):
    return _artifact(job_id, "build")


# -------------------------------------------------------------- /generate ---

@app.post("/generate", status_code=202)
async def create_generate(
    archive: UploadFile = File(...),
    target: str = Form("web"),
    assets: list[str] = Form([]),
    token: str | None = Form(None),
    callback_url: str | None = Form(None),
    authorization: str | None = Header(None),
):
    """Queue `pretext generate` on the uploaded project.

    `assets` may be repeated, or hold comma/space-separated names; empty
    means every asset type present in the source. The artifact is the
    project's generated-asset cache (see GENERATE_OUTPUT_SUBDIR), whose
    files are named by each asset's cache key."""
    _check_token(token, authorization)
    names = [a for item in assets for a in re.split(r"[,\s]+", item) if a]
    bad = [a for a in names if not _ASSET_NAME.match(a)]
    if bad:
        raise HTTPException(status_code=422, detail=f"Invalid asset type name(s): {bad}")
    job_id = await _intake(archive, callback_url, kind="generate", target=target, assets=" ".join(names))
    generate_queue.enqueue("src.build.run_generate", job_id, target, names, job_id=job_id)
    return _queued(job_id, "generate")


@app.post("/generate/{job_id}/cancel")
def cancel_generate(job_id: str, token: str | None = Form(None), authorization: str | None = Header(None)):
    _check_token(token, authorization)
    return _cancel(job_id, "generate")


@app.get("/generate/{job_id}")
def get_generate(job_id: str):
    return _status(job_id, "generate")


@app.get("/generate/{job_id}/log")
def get_generate_log(job_id: str):
    return _log(job_id, "generate")


@app.get("/generate/{job_id}/artifact")
def get_generate_artifact(job_id: str):
    return _artifact(job_id, "generate")


@app.get("/generate/{job_id}/files/{path:path}")
def get_generated_file(job_id: str, path: str):
    """One file of a generate job's artifact, by its exact path in `files`,
    so the caller needn't download and unzip the whole artifact -- e.g. the
    SVG an editor shows as the preview.

    Served out of output.zip, never the live work dir: the work dir was
    written by sandboxed user code (which can plant symlinks), while the zip
    was built by the worker with symlinks skipped, and `files` is the
    worker's own list of what it put there."""
    data = _load(job_id, "generate")
    if data.get("status") not in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"Job not finished (status={data.get('status')})")
    zip_path = os.path.join(_job_dir(job_id), "output.zip")
    names = json.loads(data["files"]) if data.get("files") else []
    if path not in names or not os.path.isfile(zip_path):
        raise HTTPException(status_code=404, detail="No such file in this job's output")
    with zipfile.ZipFile(zip_path) as z:
        content = z.read(path)
    media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{os.path.basename(path)}"'},
    )
