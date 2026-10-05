"""The routed client.

The property that matters here more than anything else in the repo:

The routing rule is the confidentiality boundary: if a cwd
under MEMHUB_LOCAL_ROOTS ever resolves to "shared", work text leaves the machine
and no later component can call it back. So the matrix below is exhaustive about
the cases that decide it — prefix collisions, symlinks, trailing slashes, stray
colons.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

CLIENT_DIR = Path(__file__).resolve().parents[2] / "client"
MEMO = CLIENT_DIR / "memo-client.py"

# Routing tests are parametrised by (tool, script, a read verb).
BOTH = [("memo", MEMO, "wake")]
BOTH_IDS = ["memo"]

# An address that accepts nothing, for the "shared but the server is off" branch.
DEAD_URL = "http://127.0.0.1:1"
# TEST-NET-1: routable nowhere, so a connect attempt hits the timeout instead of
# a refusal. This is what bounds the 2s connect budget.
BLACKHOLE_URL = "http://192.0.2.1:8900"


# ----------------------------------------------------------------- harness


class Stub:
    """A memhub the clients can actually talk to."""

    def __init__(self):
        self.requests: list[dict] = []
        self.status = 200
        self.payload: dict = {"exit_code": 0, "stdout": "", "stderr": ""}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # BaseHTTPRequestHandler names it, not us
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) or b"{}"
                stub.requests.append(
                    {
                        "path": self.path,
                        "body": json.loads(raw),
                        "auth": self.headers.get("Authorization"),
                    }
                )
                out = json.dumps(stub.payload).encode()
                self.send_response(stub.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def reply(self, payload: dict, status: int = 200) -> None:
        self.payload, self.status = payload, status

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.stop()


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    p = tmp_path / "token"
    p.write_text("sekret-token\n", encoding="utf-8")
    return p


@pytest.fixture
def local_stubs(tmp_path: Path) -> dict[str, Path]:
    """Stand-ins for the vendored tools, so a local route is observable.

    They exit 7, which no client produces on its own — that is how the tests know
    an exit code came through the exec rather than from the client."""
    memo = tmp_path / "memo.py"
    memo.write_text(
        "import os, sys\n"
        "print('LOCAL memo', os.environ.get('MEMORY_DIR'), sys.argv[1:])\n"
        "sys.exit(7)\n",
        encoding="utf-8",
    )
    return {"memo": memo}


def run_client(
    script: Path,
    argv: list[str],
    *,
    cwd: Path,
    roots: str | None = None,
    url: str = DEAD_URL,
    token: Path | None = None,
    local_stubs: dict[str, Path] | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd),
        "MEMHUB_URL": url,
        # The client refuses to run without both of these, so every test that is
        # not about the gate itself gets a working pair.
        "MEMHUB_MACHINE": "omarchy",
        "MEMHUB_LOCAL_ROOTS": "none",
    }
    if env_extra:
        env.update(env_extra)
    if roots is not None:
        env["MEMHUB_LOCAL_ROOTS"] = roots
    if token is not None:
        env["MEMHUB_TOKEN_FILE"] = str(token)
    if local_stubs is not None:
        env["MEMHUB_MEMO_PY"] = str(local_stubs["memo"])
    return subprocess.run(
        [sys.executable, str(script), *argv],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def went_local(result: subprocess.CompletedProcess) -> bool:
    return "LOCAL" in result.stdout


def went_shared(result: subprocess.CompletedProcess) -> bool:
    # With MEMHUB_URL pointing at a dead port, "shared" is unmistakable.
    return "memhub unreachable" in result.stderr


# ---------------------------------------------------- the scope matrix


@pytest.mark.parametrize(("tool", "script", "verb"), BOTH, ids=BOTH_IDS)
class TestScopeResolution:
    def test_a_machine_with_no_local_store_routes_shared(
        self, tool, script, verb, tmp_path, local_stubs
    ):
        """`none` is the declaration that this machine keeps no local memory."""
        r = run_client(
            script, [verb], cwd=tmp_path, roots="none", local_stubs=local_stubs
        )
        assert went_shared(r) and not went_local(r)

    def test_the_sentinel_is_case_insensitive(
        self, tool, script, verb, tmp_path, local_stubs
    ):
        r = run_client(
            script, [verb], cwd=tmp_path, roots="None", local_stubs=local_stubs
        )
        assert went_shared(r)

    def test_a_path_literally_named_none_is_still_the_sentinel(
        self, tool, script, verb, tmp_path, local_stubs
    ):
        """Documented consequence of the sentinel: a directory called `none`
        cannot be a local root. Nothing here has one, and the alternative — an
        unset variable meaning "share everything" — is what we are removing."""
        (tmp_path / "none").mkdir()
        r = run_client(
            script, [verb], cwd=tmp_path / "none", roots="none", local_stubs=local_stubs
        )
        assert went_shared(r)

    def test_cwd_under_a_root_routes_local(
        self, tool, script, verb, tmp_path, local_stubs
    ):
        work = tmp_path / "work"
        (work / "project").mkdir(parents=True)
        r = run_client(
            script,
            [verb],
            cwd=work / "project",
            roots=str(work),
            local_stubs=local_stubs,
        )
        assert went_local(r)
        assert r.returncode == 7  # the exec passed the tool's own code through

    def test_the_root_itself_is_local(self, tool, script, verb, tmp_path, local_stubs):
        work = tmp_path / "work"
        work.mkdir()
        r = run_client(
            script, [verb], cwd=work, roots=str(work), local_stubs=local_stubs
        )
        assert went_local(r)

    def test_cwd_outside_every_root_routes_shared(
        self, tool, script, verb, tmp_path, local_stubs
    ):
        work, other = tmp_path / "work", tmp_path / "personal"
        work.mkdir()
        other.mkdir()
        r = run_client(
            script, [verb], cwd=other, roots=str(work), local_stubs=local_stubs
        )
        assert went_shared(r)

    def test_a_sibling_sharing_the_prefix_is_not_under_the_root(
        self, tool, script, verb, tmp_path, local_stubs
    ):
        """`work-notes` is not inside `work`. A startswith() without the
        separator would leak it to the local store — or, with the roots reversed,
        leak work text to the server."""
        work, sibling = tmp_path / "work", tmp_path / "work-notes"
        work.mkdir()
        sibling.mkdir()
        r = run_client(
            script, [verb], cwd=sibling, roots=str(work), local_stubs=local_stubs
        )
        assert went_shared(r)

    def test_a_trailing_slash_on_the_root_still_matches(
        self, tool, script, verb, tmp_path, local_stubs
    ):
        work = tmp_path / "work"
        work.mkdir()
        r = run_client(
            script, [verb], cwd=work, roots=str(work) + "/", local_stubs=local_stubs
        )
        assert went_local(r)

    def test_a_symlinked_root_matches_its_real_path(
        self, tool, script, verb, tmp_path, local_stubs
    ):
        real = tmp_path / "real-work"
        real.mkdir()
        link = tmp_path / "work"
        link.symlink_to(real)
        # The root is given as the link, the cwd resolves to the target.
        r = run_client(
            script, [verb], cwd=real, roots=str(link), local_stubs=local_stubs
        )
        assert went_local(r)

    def test_a_stray_colon_does_not_route_everything_local(
        self, tool, script, verb, tmp_path, local_stubs
    ):
        """An empty entry is a prefix of every path. Treating it as a root would
        silently strand the whole machine on its local store."""
        work, other = tmp_path / "work", tmp_path / "personal"
        work.mkdir()
        other.mkdir()
        r = run_client(
            script, [verb], cwd=other, roots=f"::{work}:", local_stubs=local_stubs
        )
        assert went_shared(r)

    def test_the_second_of_several_roots_matches(
        self, tool, script, verb, tmp_path, local_stubs
    ):
        first, second = tmp_path / "clients", tmp_path / "work"
        first.mkdir()
        second.mkdir()
        r = run_client(
            script,
            [verb],
            cwd=second,
            roots=f"{first}:{second}",
            local_stubs=local_stubs,
        )
        assert went_local(r)

    @pytest.mark.parametrize("flag", ["-g", "--shared"])
    def test_the_shared_flag_overrides_a_local_cwd(
        self, tool, script, verb, flag, tmp_path, local_stubs
    ):
        work = tmp_path / "work"
        work.mkdir()
        r = run_client(
            script, [flag, verb], cwd=work, roots=str(work), local_stubs=local_stubs
        )
        assert went_shared(r)

    @pytest.mark.parametrize("flag", ["-l", "--local"])
    def test_the_local_flag_overrides_a_shared_cwd(
        self, tool, script, verb, flag, tmp_path, local_stubs
    ):
        other = tmp_path / "personal"
        other.mkdir()
        r = run_client(script, [flag, verb], cwd=other, local_stubs=local_stubs)
        assert went_local(r)

    def test_a_flag_after_the_verb_is_not_a_scope_flag(
        self, tool, script, verb, tmp_path, local_stubs
    ):
        """The flag comes first, matching what the tools print (`memo -g wake 2
        296`). Anywhere else it is the tool's own argument, not ours."""
        work = tmp_path / "work"
        work.mkdir()
        r = run_client(
            script, [verb, "-g"], cwd=work, roots=str(work), local_stubs=local_stubs
        )
        assert went_local(r)
        assert "-g" in r.stdout  # passed through to the tool untouched

    def test_help_and_no_arguments_never_touch_the_network(
        self, tool, script, verb, tmp_path, local_stubs
    ):
        for argv in ([], ["-h"], ["--help"]):
            r = run_client(script, argv, cwd=tmp_path, local_stubs=local_stubs)
            assert r.returncode == 0
            assert tool in r.stdout
            assert "unreachable" not in r.stderr


# ------------------------------------------------------- the local route


def test_local_memo_gets_the_default_store_and_argv(tmp_path, local_stubs):
    r = run_client(
        MEMO, ["-l", "note", "a fact"], cwd=tmp_path, local_stubs=local_stubs
    )
    assert r.returncode == 7
    assert ".agents/memory/optmem" in r.stdout
    assert "['note', 'a fact']" in r.stdout


def test_a_missing_local_tool_says_which_variable_to_set(tmp_path):
    r = run_client(MEMO, ["-l", "wake"], cwd=tmp_path)
    assert r.returncode == 1
    assert "MEMHUB_MEMO_PY" in r.stderr


# ------------------------------------------------------ the shared route


def test_shared_run_sends_tool_and_argv_with_the_token(tmp_path, stub, token_file):
    stub.reply({"exit_code": 0, "stdout": "Saved as #0.\n", "stderr": ""})

    r = run_client(
        MEMO, ["-g", "note", "a fact"], cwd=tmp_path, url=stub.url, token=token_file
    )

    assert r.returncode == 0
    assert r.stdout == "Saved as #0.\n"
    assert stub.requests[0]["path"] == "/run"
    body = stub.requests[0]["body"]
    assert (body["tool"], body["argv"]) == ("memo", ["note", "a fact"])
    assert stub.requests[0]["auth"] == "Bearer sekret-token"


def test_the_tools_exit_code_and_stderr_come_through(tmp_path, stub, token_file):
    stub.reply({"exit_code": 1, "stdout": "", "stderr": "memo: too long.\n"})

    r = run_client(
        MEMO, ["-g", "note", "x"], cwd=tmp_path, url=stub.url, token=token_file
    )

    assert r.returncode == 1
    assert r.stderr == "memo: too long.\n"


def test_a_rejected_token_names_the_fix(tmp_path, stub, token_file):
    stub.reply({"detail": "memhub: bad or missing bearer token"}, status=401)

    r = run_client(MEMO, ["-g", "wake"], cwd=tmp_path, url=stub.url, token=token_file)

    assert r.returncode == 1
    assert "rejected the token" in r.stderr
    assert "MEMHUB_TOKEN_FILE" in r.stderr


def test_a_server_error_shows_the_servers_own_detail(tmp_path, stub, token_file):
    stub.reply({"detail": "the store is locked by another writer; retry"}, status=409)

    r = run_client(
        MEMO, ["-g", "note", "x"], cwd=tmp_path, url=stub.url, token=token_file
    )

    assert r.returncode == 1
    assert "locked by another writer" in r.stderr


def test_a_missing_token_file_says_so_before_anything_else(tmp_path, stub):
    r = run_client(
        MEMO, ["-g", "wake"], cwd=tmp_path, url=stub.url, token=tmp_path / "absent"
    )
    assert r.returncode == 1
    assert "cannot read the memhub token" in r.stderr
    assert stub.requests == []


# --------------------------------------------------------------- fail soft


@pytest.mark.parametrize(("tool", "script", "verb"), BOTH, ids=BOTH_IDS)
def test_an_unreachable_server_fails_a_read_softly(
    tool, script, verb, tmp_path, token_file
):
    r = run_client(script, ["-g", verb], cwd=tmp_path, token=token_file)

    assert r.returncode == 1
    assert f"{tool}: memhub unreachable at {DEAD_URL}" in r.stderr
    assert "shared memory unavailable" in r.stderr
    # A read has nothing to lose, so it must not talk about lost data.
    assert "NOT recorded" not in r.stderr


@pytest.mark.parametrize(("tool", "script"), [("memo", MEMO)], ids=BOTH_IDS)
def test_an_unreachable_server_tells_a_writer_what_to_do(
    tool, script, tmp_path, token_file
):
    r = run_client(script, ["-g", "note", "a fact"], cwd=tmp_path, token=token_file)

    assert r.returncode == 1
    assert "NOT recorded" in r.stderr
    assert f"{tool} -l note" in r.stderr


def test_an_unroutable_host_fails_inside_the_connect_budget(tmp_path, token_file):
    """The 2s connect timeout, which is why the SessionStart hook can call
    `memo -g wake` on a machine whose server is switched off."""
    start = time.monotonic()
    r = run_client(
        MEMO, ["-g", "wake"], cwd=tmp_path, url=BLACKHOLE_URL, token=token_file
    )
    elapsed = time.monotonic() - start

    assert r.returncode == 1
    assert "memhub unreachable" in r.stderr
    assert elapsed < 5.0, f"took {elapsed:.1f}s — the connect timeout is not bounded"


# ---------------------------------------------------------------- scope


def as_repo(path: Path) -> Path:
    """A directory the client will read as a project: the walk looks for .git,
    it does not run git."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    return path


def sent_scope(stub: Stub) -> dict | None:
    return stub.requests[0]["body"].get("scope")


def test_the_client_reports_what_it_observed_and_decides_nothing(
    tmp_path, stub, token_file
):
    """Whether a project is machine-bound is a property of the project, the same
    on every machine, so the server holds that list. A client carrying it would
    be one of three copies free to disagree about one repo."""
    repo = as_repo(tmp_path / "memhub")

    run_client(
        MEMO,
        ["-g", "note", "a fact"],
        cwd=repo,
        url=stub.url,
        token=token_file,
        env_extra={"MEMHUB_MACHINE": "omarchy", "MEMHUB_ACTOR": "claude-code"},
    )

    scope = sent_scope(stub)
    assert scope["machine"] == "omarchy"
    assert scope["project"] == "memhub"
    assert scope["actor"] == "claude-code"
    # It reports; it does not decide.
    assert "applies" not in scope and "asserted" not in scope


def test_a_deep_subdirectory_still_finds_the_project(tmp_path, stub, token_file):
    repo = as_repo(tmp_path / "memhub")
    deep = repo / "server" / "memhub"
    deep.mkdir(parents=True)

    run_client(
        MEMO,
        ["-g", "note", "x"],
        cwd=deep,
        url=stub.url,
        token=token_file,
        env_extra={"MEMHUB_MACHINE": "omarchy"},
    )

    assert sent_scope(stub)["project"] == "memhub"


def test_no_project_is_reported_as_none(tmp_path, stub, token_file):
    """The client says "no project" rather than guessing one. What that implies
    for applicability is the server's call, and it means `unknown`."""
    run_client(
        MEMO,
        ["-g", "note", "display scale is 1.6"],
        cwd=tmp_path,
        url=stub.url,
        token=token_file,
        env_extra={"MEMHUB_MACHINE": "omarchy"},
    )

    scope = sent_scope(stub)
    assert scope["project"] is None
    assert "applies" not in scope


@pytest.mark.parametrize(
    ("flag", "applies"), [("--here", "host"), ("--anywhere", "portable")]
)
def test_a_declared_flag_overrides_the_derived_value(
    tmp_path, stub, token_file, flag, applies
):
    repo = as_repo(tmp_path / "memhub")

    run_client(
        MEMO,
        ["-g", "note", flag, "a fact"],
        cwd=repo,
        url=stub.url,
        token=token_file,
        env_extra={"MEMHUB_MACHINE": "omarchy"},
    )

    scope = sent_scope(stub)
    assert (scope["applies"], scope["asserted"]) == (applies, "declared")
    # The flag is the client's, so the tool never sees it.
    assert stub.requests[0]["body"]["argv"] == ["note", "a fact"]


def test_the_flag_is_only_taken_immediately_after_the_verb(tmp_path, stub, token_file):
    """A memory whose own text mentions the flag keeps it."""
    run_client(
        MEMO,
        ["-g", "note", "use --here to force a scope"],
        cwd=tmp_path,
        url=stub.url,
        token=token_file,
        env_extra={"MEMHUB_MACHINE": "omarchy"},
    )

    assert stub.requests[0]["body"]["argv"] == ["note", "use --here to force a scope"]
    assert "applies" not in sent_scope(stub)


# --------------------------------------------------- the configuration gate
#
# A missing *label* is a runtime condition and must never cost a memory. A
# missing *boundary* is a broken install, and letting it through would route
# confidential work to the shared store — which cannot be undone. So the client
# refuses to run at all, at `wake`, the first command of every session.


@pytest.mark.parametrize(("tool", "script", "verb"), BOTH, ids=BOTH_IDS)
class TestConfigurationGate:
    @pytest.mark.parametrize("machine", ["", "laptop", "desktop", "WORK"])
    def test_a_missing_or_unknown_role_refuses(
        self, tool, script, verb, machine, tmp_path, stub, token_file
    ):
        r = run_client(
            script,
            [verb],
            cwd=tmp_path,
            url=stub.url,
            token=token_file,
            env_extra={"MEMHUB_MACHINE": machine},
        )

        assert r.returncode == 1
        assert "refusing to run" in r.stderr
        assert "MEMHUB_MACHINE" in r.stderr
        assert stub.requests == []  # nothing left the machine

    @pytest.mark.parametrize("value", ["", "   "])
    def test_missing_local_roots_refuses(
        self, tool, script, verb, value, tmp_path, stub, token_file
    ):
        """The failure this gate exists for: an unset boundary must never read as
        "share everything"."""
        r = run_client(
            script, [verb], cwd=tmp_path, url=stub.url, token=token_file, roots=value
        )

        assert r.returncode == 1
        assert "MEMHUB_LOCAL_ROOTS is not set" in r.stderr
        assert "'none'" in r.stderr  # it says how to fix it
        assert stub.requests == []

    def test_both_missing_are_reported_together(
        self, tool, script, verb, tmp_path, stub, token_file
    ):
        """One run, both problems: a session start should not have to be fixed
        twice."""
        r = run_client(
            script,
            [verb],
            cwd=tmp_path,
            url=stub.url,
            token=token_file,
            roots="",
            env_extra={"MEMHUB_MACHINE": ""},
        )

        assert "MEMHUB_MACHINE" in r.stderr and "MEMHUB_LOCAL_ROOTS" in r.stderr

    def test_a_local_command_is_refused_too(
        self, tool, script, verb, tmp_path, local_stubs
    ):
        """The gate is about configuration, not about where a command routes."""
        r = run_client(
            script,
            ["-l", verb],
            cwd=tmp_path,
            local_stubs=local_stubs,
            env_extra={"MEMHUB_MACHINE": ""},
        )

        assert r.returncode == 1
        assert not went_local(r)

    def test_help_works_without_any_configuration(self, tool, script, verb, tmp_path):
        """Help has to work on a machine that is not set up yet — it is where
        someone reads what to set."""
        r = run_client(
            script,
            ["--help"],
            cwd=tmp_path,
            roots="",
            env_extra={"MEMHUB_MACHINE": ""},
        )

        assert r.returncode == 0
        assert tool in r.stdout
        assert "refusing" not in r.stderr


def test_the_local_route_sends_no_metadata_at_all(
    tmp_path, stub, token_file, local_stubs
):
    """The local store is single-machine by construction, so scope carries no
    information there — and the client stays smaller for it."""
    r = run_client(
        MEMO,
        ["-l", "note", "a local fact"],
        cwd=tmp_path,
        url=stub.url,
        token=token_file,
        local_stubs=local_stubs,
        env_extra={"MEMHUB_MACHINE": "omarchy"},
    )

    assert r.returncode == 7
    assert stub.requests == []


# ------------------------------------------------------------- the footer

WAKE = "#0 2026-08-19 a memory\n#1 2026-08-19 another\nYou are awake.\n"


def footer_case(tmp_path, stub, token_file, scopes, reader="omarchy"):
    stub.reply({"exit_code": 0, "stdout": WAKE, "stderr": "", "scopes": scopes})
    return run_client(
        MEMO,
        ["-g", "wake"],
        cwd=tmp_path,
        url=stub.url,
        token=token_file,
        env_extra={"MEMHUB_MACHINE": reader},
    )


def test_the_footer_names_only_foreign_memories(tmp_path, stub, token_file):
    r = footer_case(
        tmp_path,
        stub,
        token_file,
        [
            {
                "ref": "#0",
                "machine": "work",
                "applies": "host",
                "label": "work / memhub / host",
            },
            {
                "ref": "#1",
                "machine": "omarchy",
                "applies": "host",
                "label": "omarchy / x / host",
            },
        ],
    )

    assert WAKE in r.stdout  # the tool's own output is never rewritten
    assert "1 of the memories above was written elsewhere" in r.stdout
    assert "#0 [work / memhub / host]" in r.stdout
    assert "#1" not in r.stdout.split("-- scope:")[1]


def test_a_portable_memory_is_never_foreign(tmp_path, stub, token_file):
    """It is true everywhere, so where it was written does not matter."""
    r = footer_case(
        tmp_path,
        stub,
        token_file,
        [
            {
                "ref": "#0",
                "machine": "work",
                "applies": "portable",
                "label": "work / memhub / portable",
            }
        ],
    )

    assert "-- scope:" not in r.stdout


def test_an_unknown_scope_from_elsewhere_is_flagged(tmp_path, stub, token_file):
    r = footer_case(
        tmp_path,
        stub,
        token_file,
        [
            {
                "ref": "#0",
                "machine": "omarchy",
                "applies": "unknown",
                "label": "omarchy / no project / unknown",
            }
        ],
        reader="personal",
    )

    assert "#0 [omarchy / no project / unknown]" in r.stdout


def test_no_footer_when_nothing_is_foreign(tmp_path, stub, token_file):
    r = footer_case(
        tmp_path,
        stub,
        token_file,
        [
            {
                "ref": "#0",
                "machine": "omarchy",
                "applies": "host",
                "label": "omarchy / x / host",
            }
        ],
    )

    assert "-- scope:" not in r.stdout


def test_no_footer_when_the_server_sent_no_scopes(tmp_path, stub, token_file):
    r = footer_case(tmp_path, stub, token_file, None)
    assert "-- scope:" not in r.stdout
    assert WAKE in r.stdout


# ------------------------------------------------- the deny list

# Routing decides by directory; this decides by text. The two are independent
# lines, so these tests never set roots to anything but `none` — the point is
# that a *correctly* shared route is still refused when the text is work.

BOTH_WRITE = [("memo", MEMO)]


def deny_file(tmp_path: Path, body: str) -> dict[str, str]:
    p = tmp_path / "deny-shared"
    p.write_text(body, encoding="utf-8")
    return {"MEMHUB_DENY_SHARED_FILE": str(p)}


@pytest.mark.parametrize(("tool", "script"), BOTH_WRITE, ids=BOTH_IDS)
class TestDenyList:
    def test_a_matching_shared_note_is_refused_and_never_sent(
        self, tool, script, tmp_path, stub, token_file, local_stubs
    ):
        """The whole point: nothing leaves the machine."""
        r = run_client(
            script,
            ["note", "reviewed metastore MR !1101 today"],
            cwd=tmp_path,
            roots="none",
            url=stub.url,
            token=token_file,
            local_stubs=local_stubs,
            env_extra=deny_file(tmp_path, "metastore\n"),
        )
        assert r.returncode != 0
        assert stub.requests == []
        assert "metastore" in r.stderr

    def test_the_refusal_names_the_command_that_would_work(
        self, tool, script, tmp_path, stub, token_file, local_stubs
    ):
        r = run_client(
            script,
            ["note", "metastore again"],
            cwd=tmp_path,
            roots="none",
            url=stub.url,
            token=token_file,
            local_stubs=local_stubs,
            env_extra=deny_file(tmp_path, "metastore\n"),
        )
        assert f"{tool} -l note" in r.stderr

    def test_the_same_text_still_goes_local(
        self, tool, script, tmp_path, stub, token_file, local_stubs
    ):
        """The gate guards one direction. -l is the way out, so it must work."""
        r = run_client(
            script,
            ["-l", "note", "metastore MR !1101"],
            cwd=tmp_path,
            roots="none",
            url=stub.url,
            token=token_file,
            local_stubs=local_stubs,
            env_extra=deny_file(tmp_path, "metastore\n"),
        )
        assert went_local(r)
        assert stub.requests == []

    def test_no_deny_file_means_no_gate(
        self, tool, script, tmp_path, stub, token_file, local_stubs
    ):
        r = run_client(
            script,
            ["note", "metastore MR !1101"],
            cwd=tmp_path,
            roots="none",
            url=stub.url,
            token=token_file,
            local_stubs=local_stubs,
            env_extra={"MEMHUB_DENY_SHARED_FILE": str(tmp_path / "absent")},
        )
        assert r.returncode == 0
        assert len(stub.requests) == 1

    def test_an_unmatched_note_is_untouched(
        self, tool, script, tmp_path, stub, token_file, local_stubs
    ):
        r = run_client(
            script,
            ["note", "cordis-pi grew a deny list"],
            cwd=tmp_path,
            roots="none",
            url=stub.url,
            token=token_file,
            local_stubs=local_stubs,
            env_extra=deny_file(tmp_path, "metastore\n"),
        )
        assert r.returncode == 0
        assert len(stub.requests) == 1

    def test_matching_ignores_case_comments_and_blank_lines(
        self, tool, script, tmp_path, stub, token_file, local_stubs
    ):
        r = run_client(
            script,
            ["note", "the Metastore admin form"],
            cwd=tmp_path,
            roots="none",
            url=stub.url,
            token=token_file,
            local_stubs=local_stubs,
            env_extra=deny_file(tmp_path, "# terms\n\n   \nmetastore   # the one\n"),
        )
        assert r.returncode != 0
        assert stub.requests == []

    def test_a_regex_matches_as_a_regex(
        self, tool, script, tmp_path, stub, token_file, local_stubs
    ):
        r = run_client(
            script,
            ["note", "closed APP-3373 this morning"],
            cwd=tmp_path,
            roots="none",
            url=stub.url,
            token=token_file,
            local_stubs=local_stubs,
            env_extra=deny_file(tmp_path, "APP-[0-9]{3,}\n"),
        )
        assert r.returncode != 0
        assert stub.requests == []

    def test_a_broken_pattern_fails_the_write_rather_than_being_skipped(
        self, tool, script, tmp_path, stub, token_file, local_stubs
    ):
        """A gate that silently stops gating is worse than no gate."""
        r = run_client(
            script,
            ["note", "harmless"],
            cwd=tmp_path,
            roots="none",
            url=stub.url,
            token=token_file,
            local_stubs=local_stubs,
            env_extra=deny_file(tmp_path, "metastore\n[unclosed\n"),
        )
        assert r.returncode != 0
        assert stub.requests == []
        assert "[unclosed" in r.stderr

    def test_the_override_lets_the_boundary_record_itself_through(
        self, tool, script, tmp_path, stub, token_file, local_stubs
    ):
        r = run_client(
            script,
            ["note", "boundary verified: metastore must stay local"],
            cwd=tmp_path,
            roots="none",
            url=stub.url,
            token=token_file,
            local_stubs=local_stubs,
            env_extra={
                **deny_file(tmp_path, "metastore\n"),
                "MEMHUB_ALLOW_SHARED": "1",
            },
        )
        assert r.returncode == 0
        assert len(stub.requests) == 1

    def test_a_read_is_never_gated(
        self, tool, script, tmp_path, stub, token_file, local_stubs
    ):
        """`recall metastore` is how you find out whether the leak is there."""
        verb = "recall" if tool == "memo" else "grep"
        r = run_client(
            script,
            [verb, "metastore"],
            cwd=tmp_path,
            roots="none",
            url=stub.url,
            token=token_file,
            local_stubs=local_stubs,
            env_extra=deny_file(tmp_path, "metastore\n"),
        )
        assert r.returncode == 0
        assert len(stub.requests) == 1
