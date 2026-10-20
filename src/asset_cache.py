"""Per-project persistent cache of generated assets, kept on the host under
DATA_DIR/asset-cache/<project_id>/ so it outlives the ephemeral build
containers.

The PreTeXt CLI already caches the expensive asset types (latex-image,
asymptote, sageplot, prefigure) inside the project at
<generated-cache>/<type>/<md5 of the extracted asset source>.<ext>, and copies
a cached file instead of regenerating the asset whenever the key matches.
Every job here starts from a fresh project dir, so without help that cache
is empty on every build. This module gives each project (as named by the
caller's `project_id`) a cache directory on the host and connects it to jobs
in two ways:

- A **build** job bind-mounts the project's `<type>` cache dirs over the
  project's own `<generated-cache>/<type>` dirs, so the CLI reads and writes
  the persistent cache directly. Only the per-type subdirs are mounted, never
  the cache root: the CLI's per-target hash table (`.web_assets.json`) in the
  root would otherwise claim an asset type is up to date when the (fresh)
  generated-assets dir holds nothing. Leaving it ephemeral makes every build
  re-run extraction and take each file from the cache -- cheap, and correct.
- A **generate** job keeps its private, empty cache so its artifact and
  `files` list are exactly what it produced (that is its API contract); the
  worker then *harvests* those files into the project's cache afterwards, so
  a previewed figure is already cached when the full build runs.

Why per project and not one shared cache: the sandbox runs user code as root
with the cache mounted read-write, so a user could plant a file under
another user's key and inject content into their book. Scoping by project
limits the blast radius to the project itself. It also keeps the hit rate:
the win is rebuilding the same project, not sharing figures across books.

Anything that reaches this module from a job dir was written by sandboxed
user code, so names are validated (hex digests only) and symlinks are
skipped before a file is copied into the cache. Copies are made to a temp
name and renamed into place, so a reader never sees a half-written file.
"""
import logging
import os
import posixpath
import re
import shutil
import time

from .config import settings

logger = logging.getLogger(__name__)

# The directory name the API accepts as a project id. Also the only check
# that stands between a form field and a path on the host, so keep it tight:
# no leading dot (so no `.`, `..`, or hidden dirs), no separators.
PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Cache file names as the CLI writes them: an md5 hex digest, an optional
# -annotations suffix (prefigure), and an extension. Mirrors the CLI's
# CACHE_FILENAME_PATTERN in pretext/project/generate.py.
_CACHE_FILENAME = re.compile(r"^[0-9a-f]{32}(?:-annotations)?\.[A-Za-z0-9]+$")
# Subdirectories inside a type dir (prefigure keeps tactile output in one).
_SUBDIR_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _root() -> str:
    return os.path.join(settings.data_dir, settings.asset_cache_subdir)


def _host_root() -> str:
    # A path on the Docker host (always Linux), handed to the daemon as-is.
    return posixpath.join(settings.host_data_dir, settings.asset_cache_subdir)


def project_dir(project_id: str) -> str:
    """The project's cache dir, as seen from the worker container."""
    if not PROJECT_ID.match(project_id):
        raise ValueError(f"invalid project_id: {project_id!r}")
    return os.path.join(_root(), project_id)


def touch(project_id: str) -> None:
    """Record that the project was used now (the dir's mtime is its last-use
    stamp, which `prune` reads), creating its type dirs if needed.

    Dirs are world-writable because the build may run as a non-root user
    (BUILD_USER) and must be able to write new cache files into them."""
    pdir = project_dir(project_id)
    for asset_type in settings.asset_cache_types:
        d = os.path.join(pdir, asset_type)
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o777)
    os.utime(pdir, None)


def mounts(project_id: str, work_dir: str) -> dict:
    """Docker `volumes` entries that put the project's per-type cache dirs over
    the job's `<generated-cache>/<type>` dirs. Call `touch` first.

    The mount points are created inside the job's project dir here, rather
    than left to Docker, so that they exist with known permissions whatever
    the archive contained."""
    pdir = project_dir(project_id)
    host_pdir = posixpath.join(_host_root(), project_id)
    cache_root = os.path.join(work_dir, settings.generate_output_subdir)
    os.makedirs(cache_root, exist_ok=True)
    # The CLI writes its hash table into the (ephemeral) cache root.
    os.chmod(cache_root, 0o777)
    volumes = {}
    for asset_type in settings.asset_cache_types:
        os.makedirs(os.path.join(cache_root, asset_type), exist_ok=True)
        volumes[posixpath.join(host_pdir, asset_type)] = {
            "bind": posixpath.join("/work", settings.generate_output_subdir, asset_type),
            "mode": "rw",
        }
    logger.debug("asset cache for project %s mounted from %s", project_id, pdir)
    return volumes


def harvest(project_id: str, work_dir: str) -> list[str]:
    """Copy the cache files a job left in its project dir into the project's
    persistent cache. Returns the paths (relative to the cache root) added.

    Used after a generate job (which has no mount), and before a build is
    started, to promote any cache files the caller shipped inside the archive
    (the mount would otherwise hide them). Files already in the cache are
    left alone: the key is a hash of the input, so same name means same
    content, and not rewriting avoids disturbing a concurrent reader."""
    pdir = project_dir(project_id)
    cache_root = os.path.join(work_dir, settings.generate_output_subdir)
    added: list[str] = []
    for asset_type in settings.asset_cache_types:
        src_type_dir = os.path.join(cache_root, asset_type)
        if not os.path.isdir(src_type_dir) or os.path.islink(src_type_dir):
            continue
        for root, dirs, files in os.walk(src_type_dir):
            rel_root = os.path.relpath(root, src_type_dir)
            parts = [] if rel_root == "." else rel_root.split(os.sep)
            # Only descend into plainly named, real subdirectories.
            dirs[:] = [
                d for d in dirs
                if _SUBDIR_NAME.match(d) and not os.path.islink(os.path.join(root, d))
            ]
            for name in files:
                src = os.path.join(root, name)
                if not _CACHE_FILENAME.match(name):
                    logger.debug("not a cache file, skipped: %s", src)
                    continue
                if os.path.islink(src) or not os.path.isfile(src):
                    logger.warning("skipping symlink/non-file in job cache: %s", src)
                    continue
                dest_dir = os.path.join(pdir, asset_type, *parts)
                dest = os.path.join(dest_dir, name)
                if os.path.exists(dest):
                    continue
                os.makedirs(dest_dir, exist_ok=True)
                tmp = os.path.join(dest_dir, f".{name}.tmp-{os.getpid()}-{time.time_ns()}")
                try:
                    shutil.copyfile(src, tmp)
                    os.replace(tmp, dest)
                except OSError as e:
                    logger.warning("could not cache %s: %s", src, e)
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    continue
                added.append("/".join([asset_type, *parts, name]))
    if added:
        logger.info("asset cache for project %s: added %d file(s)", project_id, len(added))
    return added


def _dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def prune(keep_project_id: str | None = None) -> None:
    """Evict whole project caches: first any unused for longer than
    ASSET_CACHE_MAX_AGE_DAYS, then the least recently used until the total is
    under ASSET_CACHE_MAX_GB. `keep_project_id` (the job that just ran) is
    never evicted. Best effort: a failure here never affects a job.

    Eviction is per project, not per file, because a file's mtime is when it
    was generated, not when it was last used (the CLI copies a hit out of the
    cache without touching it), whereas `touch` stamps the project dir on
    every job. A project that is built regularly keeps its whole cache; one
    nobody has touched in a month goes."""
    root = _root()
    if not os.path.isdir(root):
        return
    now = time.time()
    try:
        projects = []
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name == keep_project_id or not entry.is_dir(follow_symlinks=False):
                    continue
                projects.append((entry.stat(follow_symlinks=False).st_mtime, entry.path, entry.name))
    except OSError as e:
        logger.warning("listing asset cache failed: %s", e)
        return

    live = []
    for mtime, path, name in projects:
        if now - mtime > settings.asset_cache_max_age:
            shutil.rmtree(path, ignore_errors=True)
            logger.info("asset cache: evicted project %s (unused for %d days)", name, (now - mtime) // 86400)
        else:
            live.append((mtime, path, name))

    budget = settings.asset_cache_max_bytes
    if budget <= 0:
        return
    sizes = {path: _dir_size(path) for _, path, _ in live}
    total = sum(sizes.values())
    if keep_project_id:
        total += _dir_size(os.path.join(root, keep_project_id))
    for mtime, path, name in sorted(live):  # oldest first
        if total <= budget:
            break
        shutil.rmtree(path, ignore_errors=True)
        total -= sizes[path]
        logger.info("asset cache: evicted project %s (%d MB) to stay under the size cap", name, sizes[path] >> 20)
