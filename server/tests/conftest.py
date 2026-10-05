"""Fixtures: a real store in a tmp dir.

The tests drive the vendored `memo.py` for real — it is the
whole point of the passthrough design, and mocking them would test nothing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memhub.app import create_app
from memhub.settings import Settings

TOKEN = "0123456789abcdef"


@pytest.fixture
def vendor_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "vendor"


@pytest.fixture
def settings(tmp_path: Path, vendor_dir: Path) -> Settings:
    """A provisioned store: `memo init` has run, the token file exists."""
    data = tmp_path / "data"
    data.mkdir()
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    s = Settings(
        data_dir=data,
        token_file=token_file,
        vendor_dir=vendor_dir,
        # One machine-bound project, so applicability derivation has something to derive.
        host_projects=("machine-config",),
    )
    subprocess.run(
        [sys.executable, str(s.memo_py), "init"],
        env={"MEMORY_DIR": str(s.optmem_dir), "PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
    )
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
