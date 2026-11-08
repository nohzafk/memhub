"""tools/promote_local.py against a real local rules store and a stub hub."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_client import Stub

from memhub import rules

ROOT = Path(__file__).resolve().parents[2]
PROMOTE = ROOT / "tools" / "promote_local.py"
RULES_PY = ROOT / "server" / "memhub" / "rules.py"


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.stop()


@pytest.fixture
def store(tmp_path: Path) -> Path:
    d = tmp_path / "rules"
    rules.init(d)
    for text in ("first #a", "second", "third #b"):
        rules.note(d, text)
    rules.drop(d, 2)
    return d


def promote(tmp_path, store, url, *extra):
    token = tmp_path / "token"
    token.write_text("sekret-token\n")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "MEMHUB_MEMO_PY": str(RULES_PY),
    }
    return subprocess.run(
        [
            sys.executable,
            str(PROMOTE),
            "--store",
            str(store),
            "--url",
            url,
            "--token-file",
            str(token),
            "--machine",
            "work",
            *extra,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_dry_run_prints_the_rules_and_sends_nothing(tmp_path, store, stub):
    r = promote(tmp_path, store, stub.url, "--dry-run")

    assert r.returncode == 0
    assert [line.split(" ", 2)[::2] for line in r.stdout.splitlines()] == [
        ["#1", "first #a"],
        ["#3", "third #b"],
    ]
    assert stub.requests == []


def test_each_rule_is_noted_in_id_order_then_the_store_is_archived(
    tmp_path, store, stub
):
    stub.reply({"exit_code": 0, "stdout": "Saved as #40.\n", "stderr": ""})

    r = promote(tmp_path, store, stub.url)

    assert r.returncode == 0, r.stderr
    assert [q["path"] for q in stub.requests] == ["/run", "/run"]
    assert [q["body"]["argv"] for q in stub.requests] == [
        ["note", "first #a"],
        ["note", "third #b"],
    ]
    assert stub.requests[0]["body"]["scope"]["machine"] == "work"
    assert stub.requests[0]["body"]["scope"]["project"] is None
    archived = [p for p in tmp_path.iterdir() if p.name.startswith("rules.promoted-")]
    assert len(archived) == 1
    # A fresh, empty store takes the old one's place.
    assert rules.read_rules(store) == []


def test_a_refusal_stops_names_what_was_sent_and_archives_nothing(
    tmp_path, store, stub
):
    stub.then(
        {"exit_code": 0, "stdout": "Saved as #40.\n", "stderr": ""},
        {"exit_code": 1, "stdout": "", "stderr": "the text is empty\n"},
    )

    r = promote(tmp_path, store, stub.url)

    assert r.returncode == 1
    assert "already sent: #1" in r.stderr
    assert "local rule #3 was refused" in r.stderr
    assert [r.id for r in rules.read_rules(store)] == [1, 3]
