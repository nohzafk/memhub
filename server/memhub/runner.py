"""`POST /run` — command passthrough to the vendored tools.

The transport is argv, not a per-verb REST API: the server executes
`memo.py` and `daylog.py` against the server-side stores, so wake pagination,
nap prompts, flock, import validation and crash repair all come from the tool
itself and cannot drift from it.

Nothing here goes through a shell. argv is handed to the tool's own parser as a
list, so a quote or a semicolon in a memory is text and never syntax.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .settings import Settings

MEMO_VERBS = {"wake", "note", "nap", "recall", "zoom", "forget", "config", "import"}
# `memo init` is deliberately absent: the store is created once, at
# provisioning. Serving it would let a typo create a second identity.
DAYLOG_VERBS = {"note", "recent", "path", "grep", "read"}

VERBS = {"memo": MEMO_VERBS, "daylog": DAYLOG_VERBS}

# Verbs that change a store, and so take the write lock.
WRITE_VERBS = {
    ("memo", "note"),
    ("memo", "nap"),
    ("memo", "import"),
    ("memo", "forget"),
    ("memo", "config"),
    ("daylog", "note"),
}

# Verbs that create a new ref, and so a new scope row. `nap` writes summaries
# into TREE/, which is derived from LOG.txt and is not a memory of its own.
REF_CREATING = {("memo", "note"), ("memo", "import"), ("daylog", "note")}

TIMEOUT = 60.0

_thread_lock = threading.Lock()


class BadRequest(ValueError):
    """An unknown tool or a verb outside the allowlist — a 400, not a crash."""


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    stdout: str
    stderr: str


def check(tool: str, argv: list[str]) -> str:
    """Validate a call and return its verb."""
    if tool not in VERBS:
        raise BadRequest(f"unknown tool {tool!r}; expected one of: memo, daylog")
    if not argv:
        raise BadRequest(f"{tool}: no verb; expected one of: {_listed(tool)}")
    verb = argv[0]
    if verb not in VERBS[tool]:
        raise BadRequest(
            f"{tool}: verb {verb!r} is not allowed; expected one of: {_listed(tool)}"
        )
    return verb


def _listed(tool: str) -> str:
    return ", ".join(sorted(VERBS[tool]))


@contextmanager
def write_lock(path: Path):
    """One writer at a time, across threads and across processes.

    `memo.py` flocks its own store already. The lock exists for `daylog.py`,
    whose prepend is read-modify-write and would otherwise lose an entry when two
    sessions note into the same day at the same moment. Its rename is atomic, so
    no reader ever sees half a day — but two writers still race, and this is what
    serialises them.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock, path.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def run(settings: Settings, tool: str, argv: list[str], recorder=None) -> RunResult:
    """Execute one allowlisted command against the server-side stores.

    `recorder`, when given, has `snapshot()` and `commit(snapshot)`. Both run
    inside the write lock, so what this write created cannot be confused with
    what a concurrent write created.
    """
    verb = check(tool, argv)
    # Both tools are Python now, so the interpreter is the same and only the script
    # and the store variable differ. daylog was a bash script until it grew `grep`
    # and `read`, and this branch used to run `bash daylog.sh` — the asymmetry is
    # gone, along with any dependence on what bash the container happens to have.
    if tool == "memo":
        script = settings.memo_py
        env_extra = {"MEMORY_DIR": str(settings.optmem_dir)}
    else:
        script = settings.daylog_py
        env_extra = {"DAYLOG_DIR": str(settings.daily_dir)}
    cmd = [sys.executable, str(script), *argv]

    if (tool, verb) in WRITE_VERBS:
        with write_lock(settings.lock_path):
            before = recorder.snapshot() if recorder is not None else None
            result = _exec(cmd, env_extra, tool, verb)
            if recorder is not None and result.exit_code == 0:
                recorder.commit(before)
            return result
    return _exec(cmd, env_extra, tool, verb)


def _exec(cmd: list[str], env_extra: dict[str, str], tool: str, verb: str) -> RunResult:
    env = dict(os.environ)
    env.update(env_extra)
    try:
        p = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            timeout=TIMEOUT,
            env=env,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return RunResult(
            124, "", f"memhub: {tool} {verb} timed out after {TIMEOUT:.0f}s"
        )
    return RunResult(p.returncode, p.stdout, p.stderr)
