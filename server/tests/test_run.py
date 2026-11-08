"""/run: the allowlist, the token, and the round trip through the real store."""

from __future__ import annotations

import subprocess

from conftest import TOKEN
from fastapi.testclient import TestClient

from memhub.settings import Settings


def run(client: TestClient, tool: str, *argv: str, scope: dict | None = None):
    body: dict = {"tool": tool, "argv": list(argv)}
    if scope is not None:
        body["scope"] = scope
    return client.post("/run", json=body)


def git_log(settings: Settings, fmt: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(settings.rules_dir), "log", f"--format={fmt}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.splitlines()


def test_memo_note_then_wake_round_trip(client: TestClient):
    noted = run(client, "memo", "note", "the build host reboots on Sundays")
    assert noted.status_code == 200
    assert noted.json()["exit_code"] == 0
    assert noted.json()["stdout"] == "Saved as #1.\n"

    woke = run(client, "memo", "wake")
    assert "#1 " in woke.json()["stdout"]
    assert "the build host reboots on Sundays" in woke.json()["stdout"]


def test_edit_drop_merge_round_trip(client: TestClient, settings: Settings):
    for text in ("#net one", "#net two", "#net three", "#net four"):
        assert run(client, "memo", "note", text).json()["exit_code"] == 0

    edited = run(client, "memo", "edit", "1", "#net one, corrected").json()
    dropped = run(client, "memo", "drop", "#2", "superseded").json()
    merged = run(client, "memo", "merge", "3,4", "#net three and four").json()

    assert (edited["exit_code"], edited["stdout"]) == (0, "Edited #1.\n")
    assert (dropped["exit_code"], dropped["stdout"]) == (0, "Dropped #2.\n")
    assert (merged["exit_code"], merged["stdout"]) == (0, "Saved as #5.\n")
    lines = (settings.rules_dir / "RULES.txt").read_text(encoding="utf-8")
    assert [ln.split(" ", 2)[::2] for ln in lines.splitlines()] == [
        ["#1", "#net one, corrected"],
        ["#5", "#net three and four"],
    ]
    assert git_log(settings, "%s")[:3] == [
        "merge #3,#4 -> #5",
        "drop #2: superseded",
        "edit #1",
    ]


def test_an_edit_to_the_same_text_succeeds_and_records_scope(
    client: TestClient, settings: Settings
):
    """Nothing changes in RULES.txt, but the write still commits and the scope
    row is still re-recorded."""
    scope = {"machine": "nuc", "host": "h1"}
    run(client, "memo", "note", "unchanged", scope=scope)
    r = run(client, "memo", "edit", "1", "unchanged", scope=scope)

    assert r.status_code == 200
    assert (r.json()["exit_code"], r.json()["stdout"]) == (0, "Edited #1.\n")
    assert git_log(settings, "%s")[0] == "edit #1"
    meta = (settings.rules_dir / "META.jsonl").read_text(encoding="utf-8")
    assert len(meta.splitlines()) == 2


def test_a_write_is_authored_by_the_scope_machine_and_host(
    client: TestClient, settings: Settings
):
    run(client, "memo", "note", "with scope", scope={"machine": "nuc", "host": "h1"})
    run(client, "memo", "note", "without scope")

    assert git_log(settings, "%an <%ae>")[:2] == [
        "memhub <memhub@localhost>",
        "nuc h1 <nuc@h1>",
    ]


def test_a_user_error_is_the_tools_exit_code_not_an_http_error(client: TestClient):
    r = run(client, "memo", "drop", "41")
    assert r.status_code == 200
    assert r.json()["exit_code"] == 1
    assert "no rule #41" in r.json()["stderr"]


def test_an_uninitialised_store_is_exit_3_in_the_body(
    client: TestClient, settings: Settings
):
    """Not a 500: the tool's own refusal reaches the caller like any result."""
    (settings.rules_dir / "RULES.txt").unlink()

    for argv in (["wake"], ["note", "lost"]):
        r = run(client, "memo", *argv)
        assert r.status_code == 200
        assert r.json()["exit_code"] == 3
        assert "no rules store" in r.json()["stderr"]


def test_argv_is_never_shell(client: TestClient):
    """A rule full of shell syntax is text, because argv goes to the tool's
    own parser as a list."""
    hostile = 'rm -rf /; $(touch pwned) `id` "quoted" & || #'
    assert run(client, "memo", "note", hostile).json()["exit_code"] == 0

    assert hostile in run(client, "memo", "recall", "quoted").json()["stdout"]


def test_unknown_tool_is_400(client: TestClient):
    r = client.post("/run", json={"tool": "rm", "argv": ["-rf", "/"]})
    assert r.status_code == 400
    assert "unknown tool" in r.json()["detail"]


def test_verb_outside_the_allowlist_is_400(client: TestClient):
    for verb in ("delete", "nap", "zoom", "forget", "config", "import"):
        r = run(client, "memo", verb, "everything")
        assert r.status_code == 400, verb
        assert "not allowed" in r.json()["detail"]


def test_memo_init_is_not_served(client: TestClient):
    """The store is created once, at provisioning. Serving init would let a
    typo create a second identity."""
    assert run(client, "memo", "init").status_code == 400


def test_daylog_is_not_served(client: TestClient):
    assert run(client, "daylog", "note", "a day happened").status_code == 400


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

    body = anon.get("/health").json()

    assert body == {
        "ok": True,
        "rule_count": 1,
        "scope_rows": 0,  # the write sent no scope
    }


def test_health_of_an_uninitialised_store(anon: TestClient, settings: Settings):
    (settings.rules_dir / "RULES.txt").unlink()

    assert anon.get("/health").json() == {
        "ok": False,
        "rule_count": 0,
        "scope_rows": 0,
    }
