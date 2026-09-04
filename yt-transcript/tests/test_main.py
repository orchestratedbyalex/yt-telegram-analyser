"""Unit tests for the yt-transcript sidecar (no network access required)."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from youtube_transcript_api._errors import RequestBlocked, TranscriptsDisabled

import main

client = TestClient(main.app)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ&t=10", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://example.com/not-a-video", None),
        ("just some text", None),
    ],
)
def test_extract_video_id(url, expected):
    assert main.extract_video_id(url) == expected


def test_fmt_time():
    assert main._fmt_time(0) == "00:00:00"
    assert main._fmt_time(65.9) == "00:01:05"
    assert main._fmt_time(3600 + 125) == "01:02:05"


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_bad_url_is_400():
    resp = client.post("/transcript", json={"url": "https://example.com"})
    assert resp.status_code == 400


class _Snippet:
    def __init__(self, start, text):
        self.start, self.text = start, text


class _Result:
    video_id = "dQw4w9WgXcQ"
    language_code = "en"
    is_generated = False
    snippets = [_Snippet(0.0, "hello"), _Snippet(61.0, "world")]


class _FakeApi:
    def __init__(self, exc=None):
        self._exc = exc

    def fetch(self, video_id, languages):
        if self._exc:
            raise self._exc
        return _Result()


def test_transcript_plain_and_timestamped():
    with patch.object(main, "_make_api", return_value=_FakeApi()):
        plain = client.post("/transcript", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
        assert plain.status_code == 200
        assert plain.json()["transcript"] == "hello world"
        assert plain.json()["language"] == "en"

        ts = client.post(
            "/transcript",
            json={"url": "https://youtu.be/dQw4w9WgXcQ", "with_timestamps": True},
        )
        assert ts.json()["transcript"] == "[00:00:00] hello\n[00:01:01] world"


def test_no_captions_is_404():
    with patch.object(main, "_make_api", return_value=_FakeApi(TranscriptsDisabled("x"))):
        resp = client.post("/transcript", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
        assert resp.status_code == 404


def test_blocked_without_searchapi_key_is_429():
    with patch.object(main, "_make_api", return_value=_FakeApi(RequestBlocked("x"))), \
         patch.object(main, "SEARCHAPI_KEY", ""):
        resp = client.post("/transcript", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
        assert resp.status_code == 429


def test_blocked_falls_back_to_searchapi():
    segments = [{"start": 0.0, "text": "from"}, {"start": 1.0, "text": "searchapi"}]
    with patch.object(main, "_make_api", return_value=_FakeApi(RequestBlocked("x"))), \
         patch.object(main, "_searchapi_fallback", return_value=segments):
        resp = client.post("/transcript", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
        assert resp.status_code == 200
        assert resp.json()["transcript"] == "from searchapi"
        assert resp.json()["is_generated"] is True
