"""Unit tests for the environment handed to job containers
(src/build.py:_container_environment). No Docker or Redis needed:

    pip install -r requirements.txt pytest httpx
    python -m pytest tests
"""
import os

os.environ.setdefault("BUILD_TOKEN", "testtoken")

from src import build  # noqa: E402
from src.config import settings  # noqa: E402

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


def test_nothing_configured_sets_nothing(monkeypatch):
    # An empty environment means run_kwargs carries no "environment" at all,
    # so the image's own (HOME with the baked ~/.ptx) stands.
    monkeypatch.setattr(settings, "build_home", "")
    monkeypatch.setattr(settings, "build_proxy", "")
    assert build._container_environment() == {}


def test_proxy_sets_both_spellings_and_no_proxy(monkeypatch):
    monkeypatch.setattr(settings, "build_home", "")
    monkeypatch.setattr(settings, "build_proxy", "http://egress-proxy:8888")
    env = build._container_environment()
    for name in PROXY_VARS:
        assert env[name] == "http://egress-proxy:8888"
    assert env["NO_PROXY"] == env["no_proxy"] == "localhost,127.0.0.1"
    assert "HOME" not in env


def test_home_and_proxy_combine(monkeypatch):
    # Before the proxy, HOME was the only variable and was set by replacing
    # the whole environment; both must now survive together.
    monkeypatch.setattr(settings, "build_home", "/home/builder")
    monkeypatch.setattr(settings, "build_proxy", "http://egress-proxy:8888")
    env = build._container_environment()
    assert env["HOME"] == "/home/builder"
    assert env["HTTPS_PROXY"] == "http://egress-proxy:8888"
