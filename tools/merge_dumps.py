#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Merge dump files by date into one importable dump.

Second cutover step. The shared halves of both workstations' histories become one store:

    uv run tools/merge_dumps.py personal-shared.txt work-shared.txt -o seed.txt

Give a file a machine role and it also writes the scope metadata for every line — the machine axis of the whole history, for free, because each
input file already says which machine it came from:

    uv run tools/merge_dumps.py \
        personal:personal-shared.txt work:work-shared.txt omarchy:omarchy-shared.txt \
        -o seed.txt --meta META.jsonl

`memo import` appends densely into a freshly reset store, so output line k
becomes memory #k — which is why the metadata can be written before the import
and why it must happen in that one import. Each row carries a
fingerprint of its memory, so a wrong alignment is detected instead of believed.

`project` and `applies` are left unrecorded here. They ride the dump step's shared/work
classification, which reviews every line by hand anyway; until then a memory
reads as "scope not recorded", which is honest and visible.

The merge is stable: memories from the same day keep the order of the files on
the command line, then their order inside each file. Naming personal first
therefore reads "personal, then work" for every shared day — the order the
seeding session expects.

Why the sort matters: `memo import` refuses a line dated before the previous
one, because OptMem's log is chronological by construction. An unsorted
concatenation of two machines' histories would be rejected at the first line
that steps back in time.
"""

import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

# The roles the server accepts. A role, not a hostname: hostnames rot.
MACHINES = ("work", "personal", "omarchy", "nuc", "cloud", "phone")

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
ENTRY_CHARS = 280


def die(msg: str) -> None:
    print(f"merge_dumps: {msg}", file=sys.stderr)
    raise SystemExit(1)


def read_dump(path: Path) -> list[tuple[str, str]]:
    """One dump file as [(date, text)], validated the way `memo import` will.

    A bad line is named by file and line number: after a hand review of three
    classified files, "line 412 of work-shared.txt" is the only message that
    points at the edit that broke it.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        die(f"{path} is not UTF-8 text. Convert it, then merge again.")
    except OSError as e:
        die(f"{path}: {e.strerror or e}")

    rows = []
    for n, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        date, _, text = line.partition(" ")
        text = text.strip()
        if not DATE_RE.fullmatch(date):
            die(f"{path} line {n}: expected 'YYYY-MM-DD <text>', got: {line}")
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            die(f"{path} line {n}: {date} is not a real date.")
        if not text:
            die(f"{path} line {n}: no text after the date.")
        if len(text.encode()) > ENTRY_CHARS:
            die(f"{path} line {n}: {len(text.encode())} bytes, limit {ENTRY_CHARS}.")
        rows.append((date, text))

    out_of_order = [n for n in range(1, len(rows)) if rows[n][0] < rows[n - 1][0]]
    if out_of_order:
        # Not fatal: the merge sorts anyway. It is still worth saying, because
        # a dump straight out of dump_log.py is monotonic, so a step backwards
        # means the classification pass moved a line to the wrong place.
        print(
            f"merge_dumps: note: {path} is not in date order"
            f" ({len(out_of_order)} steps back); the merge sorts it.",
            file=sys.stderr,
        )
    return rows


def split_role(spec: str) -> tuple[str | None, Path]:
    """`omarchy:dump.txt` → ("omarchy", dump.txt); `dump.txt` → (None, …).

    Only a known role counts as a prefix, so a path that happens to contain a
    colon is still just a path."""
    role, _, rest = spec.partition(":")
    if rest and role in MACHINES:
        return role, Path(rest).expanduser()
    return None, Path(spec).expanduser()


def merge(files: list[tuple[str | None, Path]]) -> list[tuple[str, str, str | None]]:
    """Every row as (date, text, role), sorted by date. Python's sort is stable,
    so rows of the same date keep file order first and in-file order second —
    exactly the ordering guarantee wanted here, without a custom key."""
    rows: list[tuple[str, str, str | None]] = []
    for role, path in files:
        rows.extend((date, text, role) for date, text in read_dump(path))
    return sorted(rows, key=lambda r: r[0])


def write_meta(path: Path, rows: list[tuple[str, str, str | None]]) -> None:
    """One scope row per merged line, aligned to the ids `memo import` will
    assign. Verified by fingerprint on read, never by trusting this offset."""
    with path.open("w", encoding="utf-8") as fh:
        for i, (date, text, role) in enumerate(rows):
            fh.write(
                json.dumps(
                    {
                        "ref": f"#{i}",
                        "fp": hashlib.sha256(f"{date}\0{text}".encode()).hexdigest()[
                            :16
                        ],
                        "date": date,
                        "machine": role,
                        "host": None,
                        "project": None,
                        "actor": None,
                        "applies": "unknown",
                        "asserted": "inferred",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "files",
        nargs="+",
        help="dump files in tie-break order, each optionally prefixed with a"
        f" machine role ({', '.join(MACHINES)}), as in omarchy:dump.txt",
    )
    ap.add_argument("-o", "--out", help="write here instead of stdout")
    ap.add_argument("--meta", help="also write scope metadata here (needs roles)")
    args = ap.parse_args(argv)

    files = [split_role(f) for f in args.files]
    if args.meta and any(role is None for role, _ in files):
        unroled = ", ".join(str(p) for role, p in files if role is None)
        die(
            f"--meta needs a machine role on every file; these have none: {unroled}\n"
            f"       Prefix each one, as in omarchy:{files[0][1].name}."
        )

    rows = merge(files)
    if not rows:
        die("the inputs hold no memories.")

    lines = "".join(f"{date} {text}\n" for date, text, _ in rows)
    if args.out:
        Path(args.out).expanduser().write_text(lines, encoding="utf-8")
    else:
        sys.stdout.write(lines)

    if args.meta:
        write_meta(Path(args.meta).expanduser(), rows)
        by_role: dict[str, int] = {}
        for _, _, role in rows:
            by_role[role] = by_role.get(role, 0) + 1
        tally = ", ".join(f"{role} {n}" for role, n in sorted(by_role.items()))
        print(
            f"merge_dumps: wrote scope for {len(rows)} memories ({tally})",
            file=sys.stderr,
        )

    print(
        f"merge_dumps: {len(rows)} memories from {len(files)} files,"
        f" {rows[0][0]}..{rows[-1][0]}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
