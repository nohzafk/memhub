#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Turn an OptMem LOG.txt into the import format, one memory per line.

First cutover step. Run it on each workstation to get a reviewable text file:

    uv run tools/dump_log.py > personal-dump.txt
    uv run tools/dump_log.py ~/.agents/memory/optmem.pre-memhub -o old.txt

The output is exactly what `memo import` accepts — `YYYY-MM-DD <text>` — so a
dump can be split into a shared half and a work half with any editor, and each
half imported on its own.

This reads LOG.txt itself instead of asking `memo`: `memo wake` prints a
budgeted, partly summarised view of the memory, which is the opposite of what a
migration needs. The record layout is duplicated from server/memhub/store.py on
purpose — this script runs on a workstation that has no memhub checkout beside it.
"""

import argparse
import os
import re
import sys
from pathlib import Path

LOG_REC = 320
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# `memo import` refuses a longer memory, and so does the fixed-width record.
ENTRY_CHARS = 280

DEFAULT_STORE = "~/.agents/memory/optmem"


def die(msg: str) -> None:
    print(f"dump_log: {msg}", file=sys.stderr)
    raise SystemExit(1)


def read_log(path: Path) -> list[tuple[int, str, str]]:
    """Every complete record as (index, date, text).

    Records are sliced as BYTES and decoded one at a time: slicing decoded text
    would shift every boundary after the first multi-byte character, and these
    logs are full of arrows and umlauts.
    """
    buf = path.read_bytes()
    out = []
    for i in range(len(buf) // LOG_REC):
        raw = buf[i * LOG_REC : (i + 1) * LOG_REC]
        try:
            rec = raw.decode("utf-8").rstrip()
        except UnicodeDecodeError as e:
            die(f"record #{i} of {path} is not UTF-8: {e}")
        head, _, rest = rec.partition(" ")
        date, _, text = rest.partition(" ")
        if head != f"#{i}":
            die(
                f"record #{i} of {path} says {head!r}. The log is misaligned;"
                " run `memo wake` against this store first — it repairs a"
                " partial record left by a crash."
            )
        out.append((i, date, text.strip()))
    if buf and not out:
        # Zero complete records in a file that is not empty: this is not an
        # OptMem log. Reporting "0 memories" here would read as "this machine
        # had nothing to migrate", and the originals are archived the same day.
        die(
            f"{path} is {len(buf)} bytes and holds no complete record."
            f" An OptMem log is a multiple of {LOG_REC} bytes — is this the"
            " right store?"
        )
    if len(buf) % LOG_REC:
        # memo truncates this on its next write. It was never acknowledged, so
        # it is not a memory and must not reach the new store.
        print(
            f"dump_log: ignoring a partial trailing record in {path}"
            " (a crash left it; memo repairs it on the next write)",
            file=sys.stderr,
        )
    return out


def check(rows: list[tuple[int, str, str]], path: Path) -> None:
    """Everything `memo import` will reject, reported now instead of at import time."""
    last = "0000-00-00"
    for i, date, text in rows:
        if not DATE_RE.fullmatch(date):
            die(f"record #{i} of {path} has no date: {date!r}")
        if date < last:
            print(
                f"dump_log: warning: #{i} is dated {date}, before #{i - 1}"
                f" ({last}). `memo import` refuses an out-of-order dump —"
                " sort or edit those lines before importing.",
                file=sys.stderr,
            )
        if not text:
            die(f"record #{i} of {path} has no text.")
        if len(text.encode()) > ENTRY_CHARS:
            die(
                f"record #{i} of {path} is {len(text.encode())} bytes, limit {ENTRY_CHARS}."
            )
        last = date


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "store",
        nargs="?",
        default=os.environ.get("MEMORY_DIR") or DEFAULT_STORE,
        help=f"the OptMem store directory (default: $MEMORY_DIR, else {DEFAULT_STORE})",
    )
    ap.add_argument("-o", "--out", help="write here instead of stdout")
    args = ap.parse_args(argv)

    store = Path(args.store).expanduser()
    log = store / "LOG.txt"
    if not log.is_file():
        die(f"no memory at {store} (looked for {log}).")

    rows = read_log(log)
    check(rows, log)
    lines = "".join(f"{date} {text}\n" for _, date, text in rows)

    if args.out:
        Path(args.out).expanduser().write_text(lines, encoding="utf-8")
    else:
        sys.stdout.write(lines)

    span = f"{rows[0][1]}..{rows[-1][1]}" if rows else "empty"
    print(f"dump_log: {len(rows)} memories, {span}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
