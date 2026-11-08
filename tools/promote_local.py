#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Promote rules written to the local store into the shared store.

    uv run tools/promote_local.py --dry-run
    uv run tools/promote_local.py

The local store holds what a machine kept local (`memo -l note "..."`) and
what had to be written while the server was out of reach before the outbox
existed. It is a second store with ids of its own. This is the way back: each
local rule, in id order, is sent as one `memo note`, the server assigns it a
new id, and the local store is archived so nothing can be promoted twice.

Ids are not carried over: a rule's position was never its identity across
stores, so promotion is an append, not a merge. A local rule that names
another local rule by id (`corrects #3`) names the wrong one afterwards.

The text check is the same on both sides, so a refusal means the local file
was edited by hand. Promotion stops at the first failure and archives
nothing; the rules already sent are listed, and must be dropped locally
(`memo -l drop <id>`) before a second run, or they land twice.

Scope: these rules are recorded with this machine's role and no project.
The local store keeps no record of the directory a note was written in, so
inventing one would be a guess.
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_STORE = "~/.agents/memory/rules"
DEFAULT_URL = "http://127.0.0.1:8900"
DEFAULT_TOKEN_FILE = "~/.config/memhub/token"
MACHINE_ROLES = ("work", "personal", "omarchy", "nuc", "cloud", "phone")
RULE = re.compile(r"#(\d+) (\d{4}-\d{2}-\d{2}) (.*)")


def die(msg: str) -> None:
    print(f"promote_local: {msg}", file=sys.stderr)
    raise SystemExit(1)


def read_rules(store: Path) -> list[tuple[int, str, str]]:
    """The local store's rules as (id, date, text), in id order. Blank lines
    and `//` lines are not rules (the store's own format)."""
    path = store / "RULES.txt"
    if not path.is_file():
        die(f"no store at {store} (looked for {path})")
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("//"):
            continue
        m = RULE.fullmatch(line)
        if m is None:
            die(f"{path} line {n} is not a rule: {line!r}")
        out.append((int(m.group(1)), m.group(2), m.group(3)))
    return sorted(out)


def post(url: str, token: str, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.load(e).get("detail", "")
        except (ValueError, OSError):
            pass
        die(f"{url} answered {e.code}{': ' + detail if detail else ''}")
    except OSError as e:
        die(f"cannot reach {url}: {e}")
    return {}


def stranded(sent: list[int]) -> None:
    """Name what already reached the server when promotion stops part-way."""
    if sent:
        ids = " ".join(f"#{i}" for i in sent)
        print(
            f"promote_local: already sent: {ids}. Drop them locally"
            " (memo -l drop <id>) before promoting again, or they land twice.",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", default=os.environ.get("MEMORY_DIR") or DEFAULT_STORE)
    ap.add_argument("--url", default=os.environ.get("MEMHUB_URL") or DEFAULT_URL)
    ap.add_argument(
        "--token-file",
        default=os.environ.get("MEMHUB_TOKEN_FILE") or DEFAULT_TOKEN_FILE,
    )
    ap.add_argument(
        "--machine",
        default=(os.environ.get("MEMHUB_MACHINE") or "").strip(),
        help=f"this machine's role ({', '.join(MACHINE_ROLES)})",
    )
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent")
    ap.add_argument(
        "--keep",
        action="store_true",
        help="do not archive the local store afterwards (it can then be promoted twice)",
    )
    args = ap.parse_args(argv)

    store = Path(args.store).expanduser()
    rows = read_rules(store)
    if not rows:
        print("promote_local: the local store is empty; nothing to promote.")
        return 0

    print(
        f"promote_local: {len(rows)} local rules, #{rows[0][0]}..#{rows[-1][0]}",
        file=sys.stderr,
    )
    if args.dry_run:
        sys.stdout.write("".join(f"#{i} {date} {text}\n" for i, date, text in rows))
        return 0

    if args.machine and args.machine not in MACHINE_ROLES:
        die(f"--machine {args.machine!r} is not a role: {', '.join(MACHINE_ROLES)}")

    token_path = Path(args.token_file).expanduser()
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as e:
        die(f"cannot read the token at {token_path} ({e.strerror or e})")

    scope = None
    if args.machine:
        # No project: the local store keeps no record of where a note was
        # written, and guessing one would be worse than saying nothing.
        scope = {
            "machine": args.machine,
            "host": socket.gethostname(),
            "project": None,
        }

    sent: list[int] = []
    for rule_id, _date, text in rows:
        payload: dict = {"tool": "memo", "argv": ["note", text]}
        if scope is not None:
            payload["scope"] = scope
        try:
            body = post(args.url, token, "/run", payload)
        except SystemExit:
            stranded(sent)
            raise
        sys.stdout.write(f"#{rule_id} -> {body.get('stdout', '')}")
        if body.get("exit_code") != 0:
            sys.stderr.write(body.get("stderr", ""))
            stranded(sent)
            die(f"local rule #{rule_id} was refused; nothing was archived.")
        sent.append(rule_id)

    if args.keep:
        print(
            "promote_local: the local store was NOT archived. Promote it again and"
            " these rules land twice.",
            file=sys.stderr,
        )
        return 0

    archive = store.with_name(
        f"{store.name}.promoted-{datetime.now().astimezone().date().isoformat()}"
    )
    if archive.exists():
        die(f"{archive} already exists; move it aside first")
    shutil.move(str(store), str(archive))
    print(f"promote_local: local store archived as {archive}", file=sys.stderr)

    # Recreate an empty one, so `memo -l` still has somewhere to go next time.
    memo_py = os.environ.get("MEMHUB_MEMO_PY")
    if memo_py and Path(memo_py).is_file():
        subprocess.run(
            [sys.executable, memo_py, "init"],
            env={**os.environ, "MEMORY_DIR": str(store)},
            check=False,
            capture_output=True,
        )
        print(f"promote_local: fresh local store at {store}", file=sys.stderr)
    else:
        print(
            "promote_local: recreate the local store with:"
            f" MEMORY_DIR={store} python3 rules.py init",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
