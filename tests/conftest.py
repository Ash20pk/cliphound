"""Every test runs against a throwaway data folder, never the real one."""
import importlib
import sys

import pytest


@pytest.fixture()
def cliphound(tmp_path, monkeypatch):
    import cliphound.config as config
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "CLIENT_SECRET", tmp_path / "client_secret.json")
    monkeypatch.setattr(config, "TOKEN_FILE", tmp_path / "youtube_token.json")
    (tmp_path / "jobs").mkdir()
    for name in ("cliphound.jobs", "cliphound.app"):
        sys.modules.pop(name, None)
    jobs = importlib.import_module("cliphound.jobs")
    monkeypatch.setattr(jobs, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(jobs, "CACHE", tmp_path / "cache")
    return config, jobs


@pytest.fixture()
def client(cliphound, monkeypatch):
    from fastapi.testclient import TestClient
    config, jobs = cliphound
    monkeypatch.setattr(jobs, "start", lambda: None)
    app = importlib.import_module("cliphound.app")
    with TestClient(app.app, base_url="http://127.0.0.1:8765") as c:
        yield c, jobs
