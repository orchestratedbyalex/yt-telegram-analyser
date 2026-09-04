"""yt-research FastAPI sidecar.

Wraps `codex exec --skip-git-repo-check --sandbox danger-full-access "<prompt>"`
to run gpt-5.4 with web tools, using a Codex login persisted in a bind-mounted
~/.codex directory.

This file contains two halves:
  1. Parsers — legacy text-frame `parse_codex_output` (kept for its fixture
     tests) and the live `CodexEventCollector` for `codex exec --json`.
  2. Live progress — `TelegramProgress` edits the n8n ack message as codex
     searches (the "glass box").
  3. FastAPI app — `/health`, `/verify`, `/roadmap` endpoints.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


class CodexParseError(ValueError):
    """Raised when Codex stdout doesn't match the expected structure."""


@dataclass
class CodexResult:
    markdown: str
    tokens_used: int
    model: str
    searches: int = 0


_DASH_LINE = "--------"
_TOKENS_HEADER = "tokens used"
# Lines we strip from inside the body
_NOISE_PREFIXES = ("web search:",)


def parse_codex_output(raw: str) -> CodexResult:
    """Parse Codex CLI stdout into a clean markdown body + metadata.

    Codex emits this rough structure:
        Reading additional input from stdin...
        OpenAI Codex v<version> (research preview)
        --------
        workdir: <path>
        model: <id>
        ...
        --------
        user
        <echoed prompt, possibly multi-line>
        codex
        <body line>
        web search: <query>      (zero or more)
        codex
        <more body>
        ...
        tokens used
        <integer>
        <possibly a final stdout-buffered copy of the last codex line>

    We strip everything outside the body and the noise lines inside it.
    """
    lines = raw.splitlines()

    # 1. Find the second `--------` -- end of preamble metadata block
    dash_idxs = [i for i, ln in enumerate(lines) if ln.strip() == _DASH_LINE]
    if len(dash_idxs) < 2:
        raise CodexParseError("preamble dashes not found")
    preamble_end = dash_idxs[1]
    preamble = lines[: preamble_end + 1]

    # 2. After preamble: locate `user` (echoed prompt), then `codex` (body start)
    after_preamble = lines[preamble_end + 1 :]
    try:
        user_idx = next(i for i, ln in enumerate(after_preamble) if ln.strip() == "user")
        codex_idx = next(
            i for i, ln in enumerate(after_preamble[user_idx + 1 :], start=user_idx + 1)
            if ln.strip() == "codex"
        )
    except StopIteration as exc:
        raise CodexParseError("user/codex anchors not found after preamble") from exc

    # 3. Find the `tokens used` trailer
    body_and_after = after_preamble[codex_idx + 1 :]
    try:
        tokens_idx = next(
            i for i, ln in enumerate(body_and_after) if ln.strip() == _TOKENS_HEADER
        )
    except StopIteration as exc:
        raise CodexParseError("tokens used trailer not found") from exc

    # 4. Body lines: between `codex` and `tokens used`,
    #    drop noise and inline `codex`/`user` markers and `web search:` lines
    body_lines = []
    for ln in body_and_after[:tokens_idx]:
        stripped = ln.strip()
        if stripped in {"codex", "user"}:
            continue
        if any(stripped.startswith(p) for p in _NOISE_PREFIXES):
            continue
        body_lines.append(ln)

    markdown = "\n".join(body_lines).strip()
    if not markdown:
        raise CodexParseError("empty body after stripping anchors")

    # 5. Extract tokens (line right after `tokens used`).
    # Codex prints with thousands separators (e.g. "14,595"); strip non-digits.
    if tokens_idx + 1 >= len(body_and_after):
        raise CodexParseError("no integer line after tokens used")
    tokens_raw = body_and_after[tokens_idx + 1].strip()
    digits = re.sub(r"[^\d]", "", tokens_raw)
    if not digits:
        raise CodexParseError(f"tokens used has no digits: {tokens_raw!r}")
    tokens_used = int(digits)

    # 6. Extract model from preamble (`model: gpt-5.4` line)
    model = "unknown"
    for ln in preamble:
        m = re.match(r"\s*model:\s*(\S+)", ln)
        if m:
            model = m.group(1)
            break

    return CodexResult(markdown=markdown, tokens_used=tokens_used, model=model)


# ---------------------------------------------------------------------------
# codex --json event stream
# ---------------------------------------------------------------------------
# `codex exec --json` emits one JSON object per stdout line:
#   {"type":"thread.started",...} {"type":"turn.started"}
#   {"type":"item.completed","item":{"type":"web_search","query":"...","action":{"type":"search"|"other"}}}
#   {"type":"item.completed","item":{"type":"agent_message","text":"..."}}   (interim or final)
#   {"type":"turn.completed","usage":{"input_tokens":N,"output_tokens":N,...}}
# The final answer is the last agent_message before turn.completed. This is
# the live parse path; `parse_codex_output` above is the legacy text-frame
# parser kept for reference and its fixture tests.

_URL_RE = re.compile(r"https?://\S+")


def _short_url(text: str, limit: int = 60) -> str:
    m = _URL_RE.search(text)
    if not m:
        return text[:limit]
    u = m.group(0).rstrip("'\")]")
    u = re.sub(r"^https?://(www\.)?", "", u)
    return u if len(u) <= limit else u[: limit - 1] + "…"


class CodexEventCollector:
    """Incrementally consumes codex --json lines; yields human-readable steps."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.searches = 0
        self.last_message: Optional[str] = None
        self.usage: dict = {}
        self.error: Optional[str] = None

    def feed(self, line: str) -> Optional[str]:
        line = line.strip()
        if not line.startswith("{"):
            return None
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return None
        t = ev.get("type")
        if t == "item.completed":
            item = ev.get("item") or {}
            kind = item.get("type")
            if kind == "web_search":
                action = item.get("action") or {}
                q = (item.get("query") or action.get("query") or "").strip()
                self.searches += 1
                if action.get("type") == "search" or "http" not in q:
                    step = f"🔎 {q[:90]}"
                else:
                    step = f"📄 {_short_url(q)}"
                self.steps.append(step)
                return step
            if kind == "agent_message":
                self.last_message = item.get("text") or ""
        elif t == "turn.completed":
            self.usage = ev.get("usage") or {}
        elif t in ("turn.failed", "error"):
            err = ev.get("error")
            self.error = (err.get("message") if isinstance(err, dict) else None) \
                or ev.get("message") or json.dumps(ev)[:300]
        return None

    def result(self, model: str) -> CodexResult:
        md = (self.last_message or "").strip()
        if not md:
            raise CodexParseError("no agent_message in codex event stream")
        tokens = int(self.usage.get("input_tokens", 0) or 0) + int(self.usage.get("output_tokens", 0) or 0)
        return CodexResult(markdown=md, tokens_used=tokens, model=model, searches=self.searches)


# ---------------------------------------------------------------------------
# Live progress → Telegram ("glass box")
# ---------------------------------------------------------------------------

def render_progress(title: str, steps: Iterable[str], status: str, max_lines: int = 6) -> str:
    steps = list(steps)
    tail = steps[-max_lines:]
    hidden = len(steps) - len(tail)
    lines = [f"<b>{html.escape(title)}</b>"]
    if hidden > 0:
        lines.append(f"<i>… {hidden} earlier steps</i>")
    lines += [html.escape(s) for s in tail]
    lines.append("")
    lines.append(f"<i>{html.escape(status)}</i>")
    return "\n".join(lines)


class TelegramProgress:
    """Edits one Telegram message in place as codex works. Never raises."""

    def __init__(self, token: str, chat_id: int, message_id: int, title: str,
                 min_interval: float = 2.5, max_lines: int = 6) -> None:
        self.token = token
        self.chat_id = chat_id
        self.message_id = message_id
        self.title = title
        self.min_interval = min_interval
        self.max_lines = max_lines
        self.steps: list[str] = []
        self.status = "thinking…"
        self._last_flush = 0.0
        self._last_text = ""
        self._lock = threading.Lock()

    def step(self, text: str) -> None:
        with self._lock:
            self.steps.append(text)
            self.status = f"researching… ({len(self.steps)} steps)"
        self.flush()

    def finish(self, status: str) -> None:
        with self._lock:
            self.status = status
        self.flush(force=True)

    def flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_flush < self.min_interval:
            return
        with self._lock:
            text = render_progress(self.title, self.steps, self.status, self.max_lines)
        if text == self._last_text:
            return
        self._last_flush = now
        self._last_text = text
        self._edit(text)

    def _edit(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/editMessageText"
        body = json.dumps({
            "chat_id": self.chat_id, "message_id": self.message_id,
            "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
        }).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as exc:  # noqa: BLE001 — progress is best-effort
            log.warning("telegram progress edit failed: %s", str(exc)[:200])


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
log = logging.getLogger("yt-research")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="yt-research", version="1.0.0")

PROMPT_DIR = Path(os.environ.get("YT_RESEARCH_PROMPT_DIR", "/app/prompts"))
try:
    CODEX_TIMEOUT_S = int(os.environ.get("CODEX_TIMEOUT_S", "180"))
except ValueError:
    CODEX_TIMEOUT_S = 180

# Pin the model explicitly. Codex CLI's built-in default (gpt-5.3-codex) is not
# permitted for ChatGPT-account auth and 400s immediately, so we must pass one
# the shared auth accepts. Env-overridable without a rebuild.
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.4")
# Reasoning effort for the research runs. Codex defaults to none for exec runs,
# which is the wrong trade for fact-checking; "medium" measured 2026-09-02.
CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "medium")

# Bot token of the n8n YoutubeAnalyser bot, used only to edit the "researching…"
# ack message with live progress. Empty → progress disabled, behaviour as before.
TELEGRAM_BOT_TOKEN = os.environ.get("YT_BOT_TOKEN", "")
PROGRESS_TITLES = {"verify": "🔍 Verify", "roadmap": "🗺️ Roadmap"}


class CodexInvocationError(RuntimeError):
    """Raised when invoking the codex CLI subprocess fails."""


# --- Context trimming ------------------------------------------------------
# By default `codex exec` injects its full coding-agent context into every run:
# a ~15k-char coding persona, the built-in skills catalogue, a "recommended
# plugins" marketplace list and shell/image/browser tool schemas — ~12k input
# tokens before our prompt, none of which /verify or /roadmap need. These flags
# reduce that to ~3.5k and keep only web search. They are per-invocation, so
# the mounted ~/.codex/config.toml (bind-mounted for auth) is
# untouched. Measured 2026-09-02 on codex-cli 0.152.1; re-measure after a bump
# (`codex features list` shows the flag names).
_BUILTIN_SKILLS = (
    "imagegen", "skill-installer", "skill-creator",
    "review-agent", "openai-docs", "plugin-creator",
)
_DISABLED_FEATURES = (
    "personality", "plugins", "apps", "goals", "hooks", "tool_suggest",
    "skill_search", "image_generation", "computer_use", "browser_use",
    "view_image", "sleep_tool", "unified_exec", "shell_tool",
)


def _codex_context_flags() -> list[str]:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    skills_cfg = ",".join(
        f'{{path="{codex_home}/skills/.system/{name}/SKILL.md",enabled=false}}'
        for name in _BUILTIN_SKILLS
    )
    flags = [
        "--ignore-user-config",
        "-c", f"model_reasoning_effort={CODEX_REASONING_EFFORT}",
        "-c", f"model_instructions_file={PROMPT_DIR / 'codex-instructions.md'}",
        "-c", "include_apps_instructions=false",
        "-c", "include_collaboration_mode_instructions=false",
        "-c", "include_environment_context=false",
        "-c", "personality=none",
        "-c", "suppress_unstable_features_warning=true",
        "-c", f"skills.config=[{skills_cfg}]",
    ]
    for feat in _DISABLED_FEATURES:
        flags += ["--disable", feat]
    return flags


class ResearchRequest(BaseModel):
    transcript: str = Field(..., min_length=1)
    gemini_analysis: Optional[str] = None
    video_url: str
    video_id: str
    # Optional: Telegram message to edit with live progress (the n8n ack message)
    progress_chat_id: Optional[int] = None
    progress_message_id: Optional[int] = None


class ResearchResponse(BaseModel):
    markdown: str
    tokens_used: int
    model: str
    duration_s: float
    searches: int = 0


def _load_prompt(name: str) -> str:
    path = PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        raise CodexInvocationError(f"prompt file missing: {path}")
    return path.read_text()


def _build_prompt(system_prompt: str, req: ResearchRequest) -> str:
    """Concatenate system prompt + structured user payload."""
    payload_parts = [
        f"VIDEO_URL: {req.video_url}",
        f"VIDEO_ID: {req.video_id}",
    ]
    if req.gemini_analysis:
        payload_parts.append("\nPRIOR_ANALYSIS:\n" + req.gemini_analysis)
    payload_parts.append("\nTRANSCRIPT:\n" + req.transcript)
    return system_prompt + "\n\n---\n\n" + "\n".join(payload_parts)


def _run_codex(prompt: str, timeout: int, progress: Optional[TelegramProgress] = None) -> CodexResult:
    """Invoke `codex exec --json`, streaming events to `progress` as they arrive.

    stdout is the JSON event stream (consumed line by line on a thread so
    progress edits happen live); stderr goes to a temp file so a chatty codex
    can never block on a full pipe. Raises CodexInvocationError on timeout,
    non-zero exit or a turn.failed event.
    """
    workdir = Path("/tmp/codex-runs")
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "codex", "exec", "--json",
        "--skip-git-repo-check",
        "--model", CODEX_MODEL,
        "--sandbox", "danger-full-access",
        *_codex_context_flags(),
        prompt,
    ]
    collector = CodexEventCollector()
    with tempfile.TemporaryFile(mode="w+") as err_file:
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(workdir), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=err_file, text=True,
            )
        except FileNotFoundError as exc:
            raise CodexInvocationError("codex CLI not installed in container") from exc

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                step = collector.feed(line)
                if step and progress is not None:
                    progress.step(step)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait()
            raise CodexInvocationError(f"codex timeout after {timeout}s") from exc
        reader.join(timeout=10)
        err_file.seek(0)
        stderr = err_file.read()

    if collector.error:
        raise CodexInvocationError(f"codex turn failed: {collector.error}")
    if proc.returncode != 0:
        out = stderr.strip()
        err_lines = [ln for ln in out.splitlines() if "ERROR" in ln or "error" in ln.lower()]
        detail = " | ".join(err_lines[:3]) if err_lines else out[-500:]
        raise CodexInvocationError(f"codex exited {proc.returncode}: {detail}")
    return collector.result(CODEX_MODEL)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _build_progress(name: str, req: ResearchRequest) -> Optional[TelegramProgress]:
    if not TELEGRAM_BOT_TOKEN or req.progress_chat_id is None or req.progress_message_id is None:
        return None
    return TelegramProgress(
        TELEGRAM_BOT_TOKEN, req.progress_chat_id, req.progress_message_id,
        title=PROGRESS_TITLES.get(name, name),
    )


def _handle(name: str, req: ResearchRequest) -> ResearchResponse:
    started = time.monotonic()
    progress = _build_progress(name, req)
    try:
        system_prompt = _load_prompt(name)
        full_prompt = _build_prompt(system_prompt, req)
        log.info("cmd=%s video_id=%s prompt_chars=%d progress=%s",
                 name, req.video_id, len(full_prompt), progress is not None)
        if progress is not None:
            progress.flush(force=True)
        result = _run_codex(full_prompt, CODEX_TIMEOUT_S, progress)
    except CodexInvocationError as exc:
        log.warning("cmd=%s video_id=%s codex_error=%s", name, req.video_id, exc)
        if progress is not None:
            progress.finish(f"❌ failed: {str(exc)[:120]}")
        raise HTTPException(status_code=500, detail=str(exc))
    except CodexParseError as exc:
        log.warning("cmd=%s video_id=%s parse_error=%s", name, req.video_id, exc)
        if progress is not None:
            progress.finish("❌ failed: unparseable codex output")
        raise HTTPException(status_code=500, detail=f"codex output unparseable: {exc}")
    duration = time.monotonic() - started
    log.info(
        "cmd=%s video_id=%s tokens=%d searches=%d duration_s=%.1f",
        name, req.video_id, result.tokens_used, result.searches, duration,
    )
    if progress is not None:
        progress.finish(f"✅ done · {result.searches} searches · {duration:.0f} s · {result.tokens_used:,} tokens")
    return ResearchResponse(
        markdown=result.markdown,
        tokens_used=result.tokens_used,
        model=result.model,
        duration_s=duration,
        searches=result.searches,
    )


@app.post("/verify", response_model=ResearchResponse)
def verify(req: ResearchRequest) -> ResearchResponse:
    return _handle("verify", req)


@app.post("/roadmap", response_model=ResearchResponse)
def roadmap(req: ResearchRequest) -> ResearchResponse:
    return _handle("roadmap", req)
