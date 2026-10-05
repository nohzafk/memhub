"""/run: the allowlist, the token, and the round trip through the real tools."""

from __future__ import annotations

from conftest import TOKEN
from fastapi.testclient import TestClient

from memhub.settings import Settings


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
        "memo_count": 1,
        "scope_rows": 0,  # the write sent no scope
    }
