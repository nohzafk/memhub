"""`POST /run` — command passthrough to the rules store.

The transport is argv, not a per-verb REST API: the server hands argv to the
rules store's own parser (`rules.run`), so the text check, the id assignment,
the flock and the commit all come from the tool itself and cannot drift from it.

Writes and `wake` run in process. `recall` runs the same tool as a child
process under a timeout: its argument is a caller's regex, and a pathological
one (`(a+)+$`) would otherwise hold a server thread for good.

Nothing here goes through a shell. argv is handed to the tool's parser as a
list, so a quote or a semicolon in a rule is text and never syntax.
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

from . import rules
from .settings import Settings

MEMO_VERBS = {"wake", "recall", "note", "edit", "drop", "merge"}
# `memo init` is deliberately absent: the store is created once, at
# provisioning. Serving it would let a typo create a second identity.

VERBS = {"memo": MEMO_VERBS}

# Verbs that change a store, and so take the write lock.
WRITE_VERBS = {
    ("memo", "note"),
    ("memo", "edit"),
    ("memo", "drop"),
    ("memo", "merge"),
}

# Verbs that write a scope row: `note` and `merge` for the id they create,
# `edit` for the id whose text it replaced (the row's fingerprint is of the
# text). `drop` retires an id and writes none.
SCOPE_RECORDING = {("memo", "note"), ("memo", "merge"), ("memo", "edit")}

# Seconds a `recall` may run before it is killed and answered with exit 124.
RECALL_TIMEOUT = 10.0
EXIT_TIMEOUT = 124

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
        raise BadRequest(f"unknown tool {tool!r}; expected: memo")
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

    The rules store flocks its own `.lock` already. This lock brackets the
    scope recorder's commit around the write, and `/export` takes it so a
    write landing mid-read cannot produce a torn copy of the repository.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock, path.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def run(
    settings: Settings,
    tool: str,
    argv: list[str],
    recorder=None,
    author: str | None = None,
) -> RunResult:
    """Execute one allowlisted command against the server-side store.

    `recorder`, when given, has `commit(stdout)`. It runs inside the write
    lock, so what this write created cannot be confused with what a concurrent
    write created. `author` becomes the commit's author (`"<role> <host>"`);
    without one the store's default applies.

    The call is in process, so there is no timeout: nothing here waits on
    anything but the local filesystem and `git`.
    """
    verb = check(tool, argv)
    if (tool, verb) in WRITE_VERBS:
        with write_lock(settings.lock_path):
            result = _exec(argv, settings, author)
            if recorder is not None and result.exit_code == 0:
                recorder.commit(result.stdout)
            return result
    if verb == "recall":
        return _recall(argv, settings)
    return _exec(argv, settings, author)


def _recall(argv: list[str], settings: Settings) -> RunResult:
    """`rules.py recall` in a child process, killed after `RECALL_TIMEOUT`."""
    env = dict(os.environ, MEMORY_DIR=str(settings.rules_dir), PYTHONUTF8="1")
    try:
        p = subprocess.run(
            [sys.executable, rules.__file__, *argv],
            env=env,
            capture_output=True,
            encoding="utf-8",
            timeout=RECALL_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return RunResult(EXIT_TIMEOUT, "", "recall timed out\n")
    return RunResult(p.returncode, p.stdout, p.stderr)


def _exec(argv: list[str], settings: Settings, author: str | None) -> RunResult:
    # `author=""` would read as a named author; None falls back to the default.
    code, out, err = rules.run(argv, settings.rules_dir, author or None)
    return RunResult(code, out, err)
