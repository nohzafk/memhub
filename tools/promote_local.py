#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Promote memories written to the local store into the shared store.

    uv run tools/promote_local.py --dry-run
    uv run tools/promote_local.py

The local store exists for one purpose: a fact that must not wait while the server
is unreachable (`memo -l note "..."`). Those memories then live in a second,
separate store — and OptMem is position-is-identity, so the two can never be
merged in place. This is the way back: dump the local memories to the import
format, hand them to the server, and archive the local store so nothing can
promote it twice.

Why this works when a merge would not: `memo import` appends, and it refuses a
line dated before the store's newest memory. The local tail was written *after*
everything on the server, so it appends cleanly and the server assigns the
canonical ids. Nothing is renumbered and no summary is invalidated.

What is lost, deliberately: any `nap` done locally. Those summaries were built
over local positions, which mean nothing on the server. The server simply carries
the compression debt instead, which is a prompt rather than a problem.

Scope: these memories are recorded with this machine's role and no project.
The local store keeps no record of the directory a note was written in, so
inventing one would be a guess.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

LOG_REC = 320
DEFAULT_STORE = "~/.agents/memory/optmem"
DEFAULT_URL = "http://127.0.0.1:8900"
DEFAULT_TOKEN_FILE = "~/.config/memhub/token"
MACHINE_ROLES = ("work", "personal", "omarchy", "nuc", "cloud", "phone")


def die(msg: str) -> None:
    print(f"promote_local: {msg}", file=sys.stderr)
    raise SystemExit(1)


def read_log(store: Path) -> list[tuple[str, str]]:
    """The local store as (date, text) pairs. Records are sliced as BYTES and
    decoded one at a time; slicing decoded text would shift every boundary after
    the first multi-byte character."""
    log = store / "LOG.txt"
    if not log.is_file():
        die(f"no store at {store} (looked for {log})")
    buf = log.read_bytes()
    out = []
    for i in range(len(buf) // LOG_REC):
        rec = buf[i * LOG_REC : (i + 1) * LOG_REC].decode("utf-8").rstrip()
        head, _, rest = rec.partition(" ")
        stamp, _, text = rest.partition(" ")
        if head != f"#{i}":
            die(f"record #{i} of {log} says {head!r}; the log is misaligned")
        out.append((stamp, text.strip()))
    return out


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
    rows = read_log(store)
    if not rows:
        print("promote_local: the local store is empty; nothing to promote.")
        return 0

    dump = "".join(f"{stamp} {text}\n" for stamp, text in rows)
    print(
        f"promote_local: {len(rows)} local memories, {rows[0][0]}..{rows[-1][0]}",
        file=sys.stderr,
    )
    if args.dry_run:
        sys.stdout.write(dump)
        return 0

    if args.machine and args.machine not in MACHINE_ROLES:
        die(f"--machine {args.machine!r} is not a role: {', '.join(MACHINE_ROLES)}")

    token_path = Path(args.token_file).expanduser()
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as e:
        die(f"cannot read the token at {token_path} ({e.strerror or e})")

    payload: dict = {"text": dump}
    if args.machine:
        # No project: the local store keeps no record of where a note was
        # written, and guessing one would be worse than saying nothing.
        payload["scope"] = {
            "machine": args.machine,
            "host": socket.gethostname(),
            "project": None,
        }

    body = post(args.url, token, "/import", payload)
    sys.stdout.write(body.get("stdout", ""))
    sys.stderr.write(body.get("stderr", ""))
    if body.get("exit_code") != 0:
        die(
            "the import was refused, so the local store is untouched.\n"
            "       A date older than the shared store's newest memory is the"
            " usual cause."
        )

    if args.keep:
        print(
            "promote_local: the local store was NOT archived. Promote it again and"
            " these memories land twice.",
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
            "promote_local: recreate the local store with: memo -l init",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
