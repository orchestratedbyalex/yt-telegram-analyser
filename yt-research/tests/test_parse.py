"""Tests for the Codex stdout parser used by yt-research.

Codex CLI emits structural preamble + trailer that we have to strip
before sending the result back to Telegram.
"""
from pathlib import Path

import pytest

from main import parse_codex_output, CodexParseError

FIXTURE = Path(__file__).parent / "fixtures" / "codex_verify_sample.txt"


def test_fixture_exists():
    assert FIXTURE.is_file(), "Run Task 2 to capture the fixture before this test."


def test_parse_returns_markdown_only():
    raw = FIXTURE.read_text()
    result = parse_codex_output(raw)

    # Header lines from prompts must be present in the body
    assert "## Independent Take" in result.markdown
    assert "## Bottom Line" in result.markdown

    # Structural anchors must be gone
    assert "Reading additional input" not in result.markdown
    assert "OpenAI Codex v" not in result.markdown
    assert "--------" not in result.markdown
    assert "tokens used" not in result.markdown
    assert not result.markdown.strip().startswith("user")
    assert not result.markdown.strip().startswith("codex")


def test_parse_extracts_token_count():
    raw = FIXTURE.read_text()
    result = parse_codex_output(raw)
    assert isinstance(result.tokens_used, int)
    assert result.tokens_used == 22388


def test_parse_extracts_model_id():
    raw = FIXTURE.read_text()
    result = parse_codex_output(raw)
    # Codex preamble shows e.g. `model: gpt-5.4`
    assert result.model == "gpt-5.4"


def test_parse_strips_web_search_annotations():
    raw = FIXTURE.read_text()
    result = parse_codex_output(raw)
    assert "web search:" not in result.markdown


def test_parse_strips_web_search_within_body():
    """_NOISE_PREFIXES filter must strip web-search lines that appear inside the body.

    The fixture-based test above passes even without the filter because its
    web-search lines sit in the prompt-echo zone (before the first `codex`
    marker).  This synthetic test places a web-search line inside the body
    to exercise the actual filter path.
    """
    synthetic = (
        "Reading additional input from stdin...\n"
        "OpenAI Codex v0.30.0 (research preview)\n"
        "--------\n"
        "model: gpt-5.4\n"
        "--------\n"
        "user\n"
        "ping\n"
        "codex\n"
        "Real body line one.\n"
        "web search: this should be stripped\n"
        "Real body line two.\n"
        "tokens used\n"
        "100\n"
    )
    result = parse_codex_output(synthetic)
    assert "web search:" not in result.markdown
    assert "Real body line one." in result.markdown
    assert "Real body line two." in result.markdown


def test_parse_raises_on_garbage_input():
    with pytest.raises(CodexParseError):
        parse_codex_output("not a codex output at all")


def test_parse_raises_on_empty_body():
    minimal = (
        "Reading additional input from stdin...\n"
        "OpenAI Codex v0.30.0 (research preview)\n"
        "--------\n"
        "model: gpt-5.4\n"
        "--------\n"
        "user\n"
        "ping\n"
        "codex\n"
        "\n"
        "tokens used\n"
        "100\n"
    )
    with pytest.raises(CodexParseError):
        parse_codex_output(minimal)
