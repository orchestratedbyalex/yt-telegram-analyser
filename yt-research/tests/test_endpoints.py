"""Endpoint tests for yt-research using FastAPI's TestClient with codex
subprocess monkey-patched to a deterministic fixture."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main

FIXTURE = (Path(__file__).parent / "fixtures" / "codex_verify_sample.txt").read_text()


@pytest.fixture()
def client(monkeypatch):
    # Stub `_run_codex` to return our captured fixture without hitting the CLI
    monkeypatch.setattr(main, "_run_codex",
                        lambda prompt, timeout, progress=None: main.parse_codex_output(FIXTURE))
    return TestClient(main.app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_verify_happy_path(client):
    body = {
        "transcript": "Python is a programming language. It is widely used.",
        "gemini_analysis": "Python is popular for data science.",
        "video_url": "https://youtu.be/abc12345678",
        "video_id": "abc12345678",
    }
    r = client.post("/verify", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "## Independent Take" in data["markdown"]
    assert data["tokens_used"] > 0
    assert data["model"].startswith("gpt-")
    assert data["duration_s"] >= 0


def test_roadmap_accepts_missing_gemini_analysis(client):
    body = {
        "transcript": "...",
        "video_url": "https://youtu.be/abc12345678",
        "video_id": "abc12345678",
    }
    r = client.post("/roadmap", json=body)
    assert r.status_code == 200, r.text


def test_verify_rejects_missing_transcript(client):
    body = {"video_url": "https://youtu.be/x", "video_id": "x"}
    r = client.post("/verify", json=body)
    assert r.status_code == 422


def test_endpoint_surfaces_codex_subprocess_error(monkeypatch):
    def boom(prompt, timeout, progress=None):
        raise main.CodexInvocationError("codex auth expired")
    monkeypatch.setattr(main, "_run_codex", boom)
    c = TestClient(main.app)
    r = c.post("/verify", json={
        "transcript": "x",
        "gemini_analysis": "x",
        "video_url": "https://youtu.be/abc12345678",
        "video_id": "abc12345678",
    })
    assert r.status_code == 500
    assert "codex auth expired" in r.json()["detail"]
