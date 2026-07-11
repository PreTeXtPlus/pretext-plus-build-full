"""HTTP API for the full build server.

Async by design: a build (LaTeX + Sage) can take minutes, so POST /builds
returns a job id immediately and the client polls GET /builds/{id}. This is the
deliberate sibling of the lightweight server, which returns rendered output
inline because its snippet builds are sub-second.
"""
import logging
import os
import shutil
import tarfile
import uuid
import zipfile

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from .config import settings
from .jobs import queue, store
from .notify import is_allowed_callback_url

logger = logging.getLogger(__name__)

app = FastAPI(title="PreTeXt Plus — Full Build Server")


def _check_token(form_token: str | None, authorization: str | None) -> None:
    token = form_token
    if token is None and authorization:
        token = authorization[7:] if authorization.lower().startswith("bearer ") else authorization
    if not settings.build_token or token != settings.build_token:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


def _safe_extract(archive_path: str, dest: str, filename: str | None) -> None:
    """Extract a .zip or .tar.gz project archive, guarding against path
    traversal. (Defense in depth — the build itself is also sandboxed.)"""
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


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/builds", status_code=202)
async def create_build(
    archive: UploadFile = File(...),
    target: str = Form(...),
    token: str | None = Form(None),
    callback_url: str | None = Form(None),
    authorization: str | None = Header(None),
):
    _check_token(token, authorization)

    if callback_url is not None and not is_allowed_callback_url(callback_url):
        # is_allowed_callback_url already logged the specific reason.
        raise HTTPException(status_code=422, detail="Invalid or disallowed callback_url")

    job_id = uuid.uuid4().hex
    logger.info("create_build: job=%s target=%s callback_url=%s", job_id, target, callback_url or "<none>")
    job_dir = os.path.join(settings.data_dir, "jobs", job_id)
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

    fields = {"target": target}
    if callback_url:
        fields["callback_url"] = callback_url
    store.create(job_id, **fields)
    queue.enqueue("src.build.run_build", job_id, target, job_id=job_id)
    return {"job_id": job_id, "status": "queued", "status_url": f"/builds/{job_id}"}


@app.get("/builds/{job_id}")
def get_build(job_id: str):
    data = store.get(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    resp = {"job_id": job_id, **data}
    if data.get("log") is not None:
        resp["log_url"] = f"/builds/{job_id}/log"
    if data.get("status") == "success":
        resp["artifact_url"] = f"/builds/{job_id}/artifact"
    return resp


@app.get("/builds/{job_id}/log")
def get_log(job_id: str):
    """The full, untruncated build log (combined stdout+stderr) as plain text.

    The callback payload carries only a truncated tail; this is where the
    receiver fetches the whole thing. Present as soon as the build reaches a
    terminal state; empty while still queued/running."""
    data = store.get(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    return PlainTextResponse(data.get("log") or "")


@app.get("/builds/{job_id}/artifact")
def get_artifact(job_id: str):
    data = store.get(job_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    if data.get("status") != "success":
        raise HTTPException(status_code=409, detail=f"Build not ready (status={data.get('status')})")
    zip_path = os.path.join(settings.data_dir, "jobs", job_id, "output.zip")
    if not os.path.isfile(zip_path):
        raise HTTPException(status_code=404, detail="Artifact missing or expired")
    return FileResponse(zip_path, media_type="application/zip", filename=f"{job_id}-output.zip")
