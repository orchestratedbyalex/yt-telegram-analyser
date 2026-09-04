"""Tests for the codex --json event collector and the Telegram progress renderer."""
import json

import pytest

import main

EVENTS = [
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message",
                                        "text": "Checking sources first."}},
    {"type": "item.started", "item": {"id": "item_1", "type": "web_search", "query": "",
                                      "action": {"type": "other"}}},
    {"type": "item.completed", "item": {"id": "item_1", "type": "web_search",
                                        "query": "site:github.com/openai codex cli latest release",
                                        "action": {"type": "search",
                                                   "query": "site:github.com/openai codex cli latest release"}}},
    {"type": "item.completed", "item": {"id": "item_2", "type": "web_search",
                                        "query": "https://github.com/openai/codex/releases",
                                        "action": {"type": "other"}}},
    {"type": "item.completed", "item": {"id": "item_3", "type": "web_search",
                                        "query": "'0.152.1' in https://github.com/openai/codex/releases",
                                        "action": {"type": "other"}}},
    {"type": "item.completed", "item": {"id": "item_4", "type": "agent_message",
                                        "text": "## Independent Take\n- Current version is 0.152.1"}},
    {"type": "turn.completed", "usage": {"input_tokens": 24264, "cached_input_tokens": 1792,
                                         "output_tokens": 390, "reasoning_output_tokens": 317}},
]


def _feed_all(collector, events):
    return [s for s in (collector.feed(json.dumps(e)) for e in events) if s]


def test_collector_extracts_steps_and_final_message():
    c = main.CodexEventCollector()
    steps = _feed_all(c, EVENTS)
    assert steps == [
        "🔎 site:github.com/openai codex cli latest release",
        "📄 github.com/openai/codex/releases",
        "📄 github.com/openai/codex/releases",
    ]
    r = c.result(model="gpt-5.4")
    assert r.markdown.startswith("## Independent Take")
    assert r.tokens_used == 24264 + 390
    assert r.model == "gpt-5.4"
    assert c.searches == 3


def test_collector_ignores_garbage_lines():
    c = main.CodexEventCollector()
    assert c.feed("not json") is None
    assert c.feed("") is None


def test_collector_records_turn_failure():
    c = main.CodexEventCollector()
    c.feed(json.dumps({"type": "turn.failed", "error": {"message": "model not permitted"}}))
    assert c.error == "model not permitted"


def test_collector_without_message_raises():
    c = main.CodexEventCollector()
    c.feed(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
    with pytest.raises(main.CodexParseError):
        c.result(model="gpt-5.4")


def test_render_progress_shows_tail_and_escapes_html():
    steps = [f"🔎 query {i} <b>" for i in range(8)]
    text = main.render_progress("🔍 Verify", steps, status="researching…", max_lines=5)
    assert text.startswith("<b>🔍 Verify</b>")
    assert "query 7 &lt;b&gt;" in text
    assert "query 2" not in text          # only the last 5 survive
    assert "… 3 earlier steps" in text
    assert text.rstrip().endswith("<i>researching…</i>")


def test_render_progress_empty_steps():
    text = main.render_progress("🗺️ Roadmap", [], status="thinking…")
    assert "thinking…" in text


def test_endpoint_without_token_ignores_progress_fields(monkeypatch):
    from fastapi.testclient import TestClient
    captured = {}

    def fake_run(prompt, timeout, progress=None):
        captured["progress"] = progress
        return main.CodexResult(markdown="## Independent Take\nok", tokens_used=5, model="gpt-5.4", searches=1)

    monkeypatch.setattr(main, "_run_codex", fake_run)
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", "")
    r = TestClient(main.app).post("/verify", json={
        "transcript": "x", "video_url": "https://youtu.be/abc12345678", "video_id": "abc12345678",
        "progress_chat_id": 123, "progress_message_id": 456,
    })
    assert r.status_code == 200, r.text
    assert captured["progress"] is None
    assert r.json()["searches"] == 1


def test_endpoint_with_token_builds_progress(monkeypatch):
    from fastapi.testclient import TestClient
    captured = {}

    def fake_run(prompt, timeout, progress=None):
        captured["progress"] = progress
        return main.CodexResult(markdown="ok", tokens_used=5, model="gpt-5.4")

    monkeypatch.setattr(main, "_run_codex", fake_run)
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", "123:abc")
    r = TestClient(main.app).post("/roadmap", json={
        "transcript": "x", "video_url": "https://youtu.be/abc12345678", "video_id": "abc12345678",
        "progress_chat_id": 123, "progress_message_id": 456,
    })
    assert r.status_code == 200, r.text
    p = captured["progress"]
    assert isinstance(p, main.TelegramProgress)
    assert p.chat_id == 123 and p.message_id == 456 and p.title == "🗺️ Roadmap"


def test_context_flags_carry_reasoning_effort(monkeypatch):
    monkeypatch.setattr(main, "CODEX_REASONING_EFFORT", "high")
    flags = main._codex_context_flags()
    assert "model_reasoning_effort=high" in flags
    assert "--ignore-user-config" in flags
