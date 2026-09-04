"""pytest configuration for yt-research tests.

Sets PROMPT_DIR to the local prompts directory so that _load_prompt works
during test runs without requiring the container's /app/prompts path.
"""
from pathlib import Path

import pytest

import main

_REPO_PROMPTS = Path(__file__).parent.parent / "prompts"


@pytest.fixture(autouse=True)
def _patch_prompt_dir(monkeypatch):
    """Redirect main.PROMPT_DIR to the local prompts folder for all tests."""
    monkeypatch.setattr(main, "PROMPT_DIR", _REPO_PROMPTS)
