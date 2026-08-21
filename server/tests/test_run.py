"""/run: the allowlist, the token, and the round trip through the real tools."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from conftest import TOKEN
from fastapi.testclient import TestClient

from memhub.settings import Settings
from memhub.store import parse_daylog


def run(client: TestClient, tool: str, *argv: str):
    return client.post("/run", json={"tool": tool, "argv": list(argv)})


def test_memo_note_then_wake_round_trip(client: TestClient):
    noted = run(client, "memo", "note", "the build host reboots on Sundays")
    assert noted.status_code == 200
    assert noted.json()["exit_code"] == 0
    assert "Saved as #0." in noted.json()["stdout"]

    woke = run(client, "memo", "wake")
    assert "the build host reboots on Sundays" in woke.json()["stdout"]


def test_argv_is_never_shell(client: TestClient):
    """A memory full of shell syntax is text, because argv goes to the tool's
    own parser as a list."""
    hostile = 'rm -rf /; $(touch pwned) `id` "quoted" & || #'
    assert run(client, "memo", "note", hostile).json()["exit_code"] == 0

    assert hostile in run(client, "memo", "recall", "quoted").json()["stdout"]


def test_unknown_tool_is_400(client: TestClient):
    r = client.post("/run", json={"tool": "rm", "argv": ["-rf", "/"]})
    assert r.status_code == 400
    assert "unknown tool" in r.json()["detail"]


def test_verb_outside_the_allowlist_is_400(client: TestClient):
    r = run(client, "memo", "delete", "everything")
    assert r.status_code == 400
    assert "not allowed" in r.json()["detail"]


def test_memo_init_is_not_served(client: TestClient):
    """The store is created once, at provisioning. Serving init would let a
    typo create a second identity."""
    assert run(client, "memo", "init").status_code == 400


def test_daylog_verb_allowlist(client: TestClient):
    assert run(client, "daylog", "note", "a day happened").json()["exit_code"] == 0
    assert run(client, "daylog", "wake").status_code == 400


def test_missing_verb_is_400(client: TestClient):
    assert client.post("/run", json={"tool": "memo", "argv": []}).status_code == 400


def test_bad_token_is_401(anon: TestClient):
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
        r = anon.post("/run", json={"tool": "memo", "argv": ["wake"]}, headers=headers)
        assert r.status_code == 401, headers


def test_verify_needs_the_token(anon: TestClient):
    assert anon.get("/verify").status_code == 401


def test_missing_token_file_is_503(settings: Settings):
    """A server with no token configured fails closed — but says which failure
    it is, because 401 would send the user hunting on the client."""
    settings.token_file.unlink()
    from memhub.app import create_app

    client = TestClient(create_app(settings), headers={"Authorization": "Bearer x"})
    r = client.post("/run", json={"tool": "memo", "argv": ["wake"]})
    assert r.status_code == 503
    assert "no token configured" in r.json()["detail"]


def test_health_needs_no_token(anon: TestClient, client: TestClient):
    run(client, "memo", "note", "one fact")
    run(client, "daylog", "note", "one day")

    body = anon.get("/health").json()

    assert body == {
        "ok": True,
        "memo_count": 1,
        "daylog_days": 1,
        "scope_rows": 0,  # neither write sent a scope
    }


def test_concurrent_daylog_notes_all_survive(client: TestClient, settings: Settings):
    """daylog.py prepends read-modify-write, which is not concurrent-safe: two
    unserialised writers each copy the old file and one rename wins. The server's
    write lock is what makes this test pass."""
    texts = [f"entry number {i}" for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda t: run(client, "daylog", "note", t), texts))

    assert all(r.json()["exit_code"] == 0 for r in results)
    day = next(iter(settings.daily_dir.glob("*.md")))
    written = {d.text for d in parse_daylog(day)}
    assert written == set(texts)


def test_daylog_grep_and_read_return_content(client: TestClient):
    """`grep` and `read` are the verbs that make a day readable from another
    machine, and the reason they can be: both return content, so they cross the
    client/server boundary. `path` cannot, which is why it stays local-only.

    This replaces a marker test that asserted the opposite — the allowlist
    carried both verbs while the vendored tool was the bash copy that
    predated them, so each came back as a 200 carrying exit_code 1. Re-vendoring
    `daylog.py` is what flipped it.
    """
    marker = "a line worth finding later"
    assert run(client, "daylog", "note", marker).json()["exit_code"] == 0

    found = run(client, "daylog", "grep", "worth finding").json()
    assert found["exit_code"] == 0
    assert marker in found["stdout"]
    # `<date>.md:<line>: text` — the date leads, because it is what `read` takes.
    assert re.match(r"^\d{4}-\d{2}-\d{2}\.md:\d+: ", found["stdout"])

    whole = run(client, "daylog", "read").json()
    assert whole["exit_code"] == 0
    assert marker in whole["stdout"]


def test_daylog_grep_misses_are_not_errors_in_transport(client: TestClient):
    """A pattern that matches nothing is exit_code 1 from the tool, carried by a
    200. The allowlist rejects an unknown verb with a 400 instead, and keeping
    those two apart is what lets a caller tell "no such day" from "no such verb".
    """
    body = run(client, "daylog", "grep", "zzz-nothing-matches-this").json()
    assert body["exit_code"] == 1
    assert "no line matches" in body["stderr"]
