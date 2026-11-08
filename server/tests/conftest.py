"""Fixtures: a real rules store in a tmp dir.

The tests drive the rules store for real, git included — it is the whole point
of the passthrough design, and mocking it would test nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memhub import rules
from memhub.app import create_app
from memhub.settings import Settings

TOKEN = "0123456789abcdef"


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """A provisioned store: `rules.py init` has run, the token file exists."""
    # The commit author must come from the request, not from this shell.
    monkeypatch.delenv("MEMHUB_AUTHOR", raising=False)
    data = tmp_path / "data"
    data.mkdir()
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    s = Settings(
        data_dir=data,
        token_file=token_file,
        # One machine-bound project, so applicability derivation has something to derive.
        host_projects=("machine-config",),
    )
    rules.init(s.rules_dir)
    return s


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(
        create_app(settings), headers={"Authorization": f"Bearer {TOKEN}"}
    )


@pytest.fixture
def anon(settings: Settings) -> TestClient:
    """The same app with no credentials attached."""
    return TestClient(create_app(settings))
