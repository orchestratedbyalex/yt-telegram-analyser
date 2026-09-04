import http.cookiejar
import logging
import os
import re

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    InvalidVideoId,
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
)

log = logging.getLogger("yt-transcript")
app = FastAPI()

COOKIE_PATH = os.environ.get("COOKIE_PATH", "")
SEARCHAPI_KEY = os.environ.get("SEARCHAPI_KEY", "")
SEARCHAPI_URL = "https://www.searchapi.io/api/v1/search"

VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?.*v=|youtu\.be/|youtube\.com/embed/"
    r"|youtube\.com/shorts/|youtube\.com/live/)([a-zA-Z0-9_-]{11})"
)


def _make_api() -> YouTubeTranscriptApi:
    if COOKIE_PATH and os.path.isfile(COOKIE_PATH):
        cookie_jar = http.cookiejar.MozillaCookieJar(COOKIE_PATH)
        cookie_jar.load(ignore_discard=True, ignore_expires=True)
        session = requests.Session()
        session.cookies = cookie_jar
        return YouTubeTranscriptApi(http_client=session)
    return YouTubeTranscriptApi()


def extract_video_id(url: str) -> str | None:
    m = VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def _fmt_time(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _searchapi_fallback(video_id: str, language: str) -> list[dict] | None:
    """Fetch transcript from SearchAPI.io. Returns list of {start, text} or None.

    Free tier is 100 requests/month, so this only runs after the primary path
    (youtube-transcript-api) has been blocked.
    """
    if not SEARCHAPI_KEY:
        log.warning("SEARCHAPI_KEY not set; SearchAPI fallback unavailable")
        return None

    try:
        resp = requests.get(
            SEARCHAPI_URL,
            params={
                "engine": "youtube_transcripts",
                "video_id": video_id,
                "lang": language,
            },
            headers={"Authorization": f"Bearer {SEARCHAPI_KEY}"},
            timeout=30,
        )
    except requests.RequestException as e:
        log.warning("SearchAPI request error for %s: %s", video_id, e)
        return None

    if resp.status_code == 401:
        log.error("SearchAPI rejected the API key (401)")
        return None
    if resp.status_code == 403:
        log.error("SearchAPI quota exhausted or forbidden (403)")
        return None
    if resp.status_code != 200:
        log.warning(
            "SearchAPI returned %d for %s: %s",
            resp.status_code, video_id, resp.text[:300],
        )
        return None

    try:
        data = resp.json()
    except ValueError:
        log.warning("SearchAPI returned non-JSON for %s", video_id)
        return None

    transcripts = data.get("transcripts")
    if not transcripts:
        log.info("SearchAPI returned no transcripts for %s", video_id)
        return None

    log.info("SearchAPI fallback: %d segments for %s", len(transcripts), video_id)
    return [{"start": float(t["start"]), "text": t["text"]} for t in transcripts]


class TranscriptRequest(BaseModel):
    url: str
    language: str = "en"
    with_timestamps: bool = False


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/transcript")
def get_transcript(req: TranscriptRequest):
    video_id = extract_video_id(req.url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Could not extract video ID from URL")

    langs = [req.language]
    if req.language != "en":
        langs.append("en")

    # --- Attempt 1: youtube-transcript-api (fast, no quota) ---
    use_fallback = False
    try:
        api = _make_api()
        result = api.fetch(video_id, languages=langs)

        if req.with_timestamps:
            text = "\n".join(
                f"[{_fmt_time(snippet.start)}] {snippet.text}"
                for snippet in result.snippets
            )
        else:
            text = " ".join(snippet.text for snippet in result.snippets)

        return {
            "transcript": text,
            "video_id": result.video_id,
            "language": result.language_code,
            "is_generated": result.is_generated,
        }

    except (RequestBlocked, IpBlocked):
        log.info("youtube-transcript-api blocked for %s, trying SearchAPI fallback", video_id)
        use_fallback = True
    except (TranscriptsDisabled, NoTranscriptFound):
        raise HTTPException(
            status_code=404,
            detail="No captions available for this video in the requested language(s)",
        )
    except VideoUnavailable:
        raise HTTPException(status_code=404, detail="Video is unavailable")
    except InvalidVideoId:
        raise HTTPException(status_code=400, detail="Invalid video ID")
    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e).lower()
        if "429" in error_msg or "too many" in error_msg:
            log.info("youtube-transcript-api 429 for %s, trying SearchAPI fallback", video_id)
            use_fallback = True
        else:
            raise HTTPException(status_code=502, detail=f"Transcript API error: {e}")

    # --- Attempt 2: SearchAPI.io (paid quota, handles bot-walled videos) ---
    if use_fallback:
        segments = _searchapi_fallback(video_id, req.language)
        if segments is None:
            raise HTTPException(
                status_code=429,
                detail="Transcript fetch failed (primary blocked, SearchAPI unavailable or quota exhausted)",
            )

        if req.with_timestamps:
            text = "\n".join(
                f"[{_fmt_time(seg['start'])}] {seg['text']}"
                for seg in segments
            )
        else:
            text = " ".join(seg["text"] for seg in segments)

        return {
            "transcript": text,
            "video_id": video_id,
            "language": req.language,
            "is_generated": True,
        }
