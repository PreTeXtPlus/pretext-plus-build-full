"""Unit tests for the per-project asset cache (src/asset_cache.py) and the
API's project_id validation. No Docker or Redis needed:

    pip install -r requirements.txt pytest httpx
    python -m pytest tests
"""
import os
import time

import pytest

os.environ.setdefault("BUILD_TOKEN", "testtoken")

from src import asset_cache  # noqa: E402
from src.config import settings  # noqa: E402

KEY_A = "0123456789abcdef0123456789abcdef"
KEY_B = "fedcba9876543210fedcba9876543210"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "host_data_dir", "/host/data")
    monkeypatch.setattr(settings, "asset_cache_max_age", 30 * 86400)
    monkeypatch.setattr(settings, "asset_cache_max_bytes", 0)
    return tmp_path / "data"


def _write(path, content=b"<svg/>"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ------------------------------------------------------------- project ids --

@pytest.mark.parametrize("good", ["book-42", "a", "A_b-C9", "x" * 64])
def test_project_id_accepts_slugs(good):
    assert asset_cache.PROJECT_ID.match(good)


@pytest.mark.parametrize("bad", ["", ".", "..", ".hidden", "-x", "_x", "a/b", "a\\b", "a b", "x" * 65, "é"])
def test_project_id_rejects_paths_and_junk(bad):
    assert not asset_cache.PROJECT_ID.match(bad)


def test_project_dir_refuses_bad_id(data_dir):
    with pytest.raises(ValueError):
        asset_cache.project_dir("../etc")


# ------------------------------------------------------------ touch/mounts --

def test_touch_creates_type_dirs_and_stamps(data_dir):
    asset_cache.touch("book-42")
    pdir = data_dir / "asset-cache" / "book-42"
    for t in settings.asset_cache_types:
        assert (pdir / t).is_dir()
    assert time.time() - pdir.stat().st_mtime < 5


def test_mounts_bind_each_type_dir_and_create_mountpoints(data_dir, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    asset_cache.touch("book-42")
    vols = asset_cache.mounts("book-42", str(work))
    assert set(vols) == {
        f"/host/data/asset-cache/book-42/{t}" for t in settings.asset_cache_types
    }
    for host_path, spec in vols.items():
        t = host_path.rsplit("/", 1)[1]
        assert spec == {"bind": f"/work/.cache/{t}", "mode": "rw"}
        assert (work / ".cache" / t).is_dir()


# ---------------------------------------------------------------- harvest --

def test_harvest_copies_only_cache_named_regular_files(data_dir, tmp_path):
    work = tmp_path / "work"
    _write(work / ".cache" / "latex-image" / f"{KEY_A}.svg")
    _write(work / ".cache" / "latex-image" / f"{KEY_A}.pdf")
    _write(work / ".cache" / "prefigure" / "tactile" / f"{KEY_B}.pdf")
    _write(work / ".cache" / "prefigure" / f"{KEY_B}-annotations.xml")
    _write(work / ".cache" / "latex-image" / "figure1.svg")  # not a key
    _write(work / ".cache" / ".web_assets.json")  # hash table, not an asset
    _write(work / ".cache" / "webwork" / f"{KEY_A}.xml")  # not a cached type
    _write(work / ".cache" / "prefigure" / "..evil" / f"{KEY_A}.svg")  # odd subdir
    link = work / ".cache" / "asymptote" / f"{KEY_B}.html"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(str(work / ".cache" / "latex-image" / f"{KEY_A}.svg"), str(link))
        have_symlink = True
    except (OSError, NotImplementedError):
        have_symlink = False

    added = asset_cache.harvest("book-42", str(work))

    assert added == sorted(added)
    assert set(added) == {
        f"latex-image/{KEY_A}.svg",
        f"latex-image/{KEY_A}.pdf",
        f"prefigure/tactile/{KEY_B}.pdf",
        f"prefigure/{KEY_B}-annotations.xml",
    }
    pdir = data_dir / "asset-cache" / "book-42"
    assert (pdir / "latex-image" / f"{KEY_A}.svg").read_bytes() == b"<svg/>"
    assert (pdir / "prefigure" / "tactile" / f"{KEY_B}.pdf").is_file()
    assert not (pdir / "latex-image" / "figure1.svg").exists()
    assert not (pdir / "webwork").exists()
    assert not (pdir / "prefigure" / "..evil").exists()
    if have_symlink:
        assert not (pdir / "asymptote" / f"{KEY_B}.html").exists()
    # No temp files left behind.
    assert not [p for p in pdir.rglob(".*tmp-*")]


def test_harvest_keeps_existing_files_and_reports_only_new(data_dir, tmp_path):
    work = tmp_path / "work"
    _write(work / ".cache" / "latex-image" / f"{KEY_A}.svg", b"new")
    _write(work / ".cache" / "latex-image" / f"{KEY_B}.svg", b"second")
    existing = _write(data_dir / "asset-cache" / "book-42" / "latex-image" / f"{KEY_A}.svg", b"old")

    added = asset_cache.harvest("book-42", str(work))

    assert added == [f"latex-image/{KEY_B}.svg"]
    assert existing.read_bytes() == b"old"


def test_harvest_with_no_cache_dir_is_a_noop(data_dir, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    assert asset_cache.harvest("book-42", str(work)) == []


# ------------------------------------------------------------------ prune --

def _project(data_dir, name, age_days=0, size=0):
    pdir = data_dir / "asset-cache" / name / "latex-image"
    _write(pdir / f"{KEY_A}.svg", b"x" * size)
    stamp = time.time() - age_days * 86400
    os.utime(data_dir / "asset-cache" / name, (stamp, stamp))
    return data_dir / "asset-cache" / name


def test_prune_evicts_unused_projects_but_keeps_current(data_dir, monkeypatch):
    stale = _project(data_dir, "stale", age_days=45)
    fresh = _project(data_dir, "fresh", age_days=1)
    current = _project(data_dir, "current", age_days=45)

    asset_cache.prune(keep_project_id="current")

    assert not stale.exists()
    assert fresh.exists()
    assert current.exists()


def test_prune_evicts_least_recently_used_over_size_cap(data_dir, monkeypatch):
    monkeypatch.setattr(settings, "asset_cache_max_bytes", 2500)
    oldest = _project(data_dir, "oldest", age_days=10, size=1000)
    middle = _project(data_dir, "middle", age_days=5, size=1000)
    newest = _project(data_dir, "newest", age_days=1, size=1000)

    asset_cache.prune()

    assert not oldest.exists()
    assert middle.exists()
    assert newest.exists()


def test_prune_counts_current_project_toward_cap(data_dir, monkeypatch):
    monkeypatch.setattr(settings, "asset_cache_max_bytes", 1500)
    other = _project(data_dir, "other", age_days=1, size=1000)
    current = _project(data_dir, "current", age_days=0, size=1000)

    asset_cache.prune(keep_project_id="current")

    assert not other.exists()
    assert current.exists()


def test_prune_without_cache_dir_is_a_noop(data_dir):
    asset_cache.prune()  # nothing to do, must not raise


# -------------------------------------------------------------------- API --

def test_api_rejects_bad_project_id(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from src import api

    monkeypatch.setattr(settings, "build_token", "testtoken")
    client = TestClient(api.app)
    r = client.post(
        "/builds",
        data={"token": "testtoken", "target": "web", "project_id": "../etc"},
        files={"archive": ("p.zip", b"PK", "application/zip")},
    )
    assert r.status_code == 422
    assert "project_id" in r.json()["detail"]


@pytest.mark.parametrize("raw, expected", [(None, {}), ("", {}), ("  ", {}), (" book-42 ", {"project_id": "book-42"})])
def test_api_project_id_field(raw, expected):
    from src import api

    assert api._project_id(raw) == expected
