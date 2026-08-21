"""Readers for the two stores on disk.

Neither store is ever written here: `memo.py` and `daylog.py` own their files
(see runner.py). This module only decodes them, for scope and /health.

OptMem is position-is-identity: memory `#i` is the fixed 320-byte record at
offset `i*320` of LOG.txt, `"#<id> <YYYY-MM-DD> <text>"` right-padded with
spaces. Records are sliced as BYTES and decoded one at a time — slicing decoded
text would shift every boundary after the first multi-byte character.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

LOG_REC = 320

# `<!-- 2026-08-19 14:03:22 -->` opens every daylog entry; the body runs to the
# next such line, or to the end of the file.
STAMP_RE = re.compile(r"^<!-- (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) -->$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Doc:
    """One recorded thing: a memory, or one daylog entry."""

    source: str  # 'memo' | 'daylog'
    ref: str  # memo: '#123'; daylog: '2026-08-19/14:03:22'
    date: str  # YYYY-MM-DD
    text: str


def memo_count(optmem_dir: Path) -> int:
    """How many complete records LOG.txt holds. A partial trailing record left
    by a crash is not a memory until `memo` repairs it, so it is not counted."""
    try:
        return (optmem_dir / "LOG.txt").stat().st_size // LOG_REC
    except FileNotFoundError:
        return 0


def read_memos(optmem_dir: Path, start: int = 0) -> list[Doc]:
    """Memories [start, end) as Docs, read by seeking straight to `start`."""
    path = optmem_dir / "LOG.txt"
    try:
        fh = path.open("rb")
    except FileNotFoundError:
        return []
    with fh:
        fh.seek(start * LOG_REC)
        buf = fh.read()
    out = []
    for k in range(len(buf) // LOG_REC):
        rec = buf[k * LOG_REC : (k + 1) * LOG_REC].decode("utf-8").rstrip()
        head, _, rest = rec.partition(" ")
        date, _, text = rest.partition(" ")
        out.append(Doc("memo", head, date, text))
    return out


def daylog_files(daily_dir: Path) -> list[Path]:
    """The dated logs, oldest first. Anything else in the directory — a
    hand-added README, an editor backup — is not a log and is skipped."""
    try:
        names = list(daily_dir.iterdir())
    except FileNotFoundError:
        return []
    return sorted(
        p for p in names if p.is_file() and DAY_RE.match(p.stem) and p.suffix == ".md"
    )


def daylog_days(daily_dir: Path) -> int:
    return len(daylog_files(daily_dir))


def parse_daylog(path: Path) -> list[Doc]:
    """The entries of one day file, in file order (newest first — daylog
    prepends). The date comes from the filename, so an entry never lands under
    a different day than the file it lives in.

    Two entries can share a timestamp when a session notes twice inside one
    second; the second one gets a `.2` suffix so refs stay unique.
    """
    day = path.stem
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[Doc] = []
    seen: dict[str, int] = {}
    stamp: str | None = None
    body: list[str] = []

    def flush() -> None:
        if stamp is None:
            return
        text = "\n".join(body).strip()
        if not text:
            return
        seen[stamp] = seen.get(stamp, 0) + 1
        ref = f"{day}/{stamp}" if seen[stamp] == 1 else f"{day}/{stamp}.{seen[stamp]}"
        out.append(Doc("daylog", ref, day, text))

    for line in lines:
        m = STAMP_RE.match(line.strip())
        if m:
            flush()
            stamp, body = m.group(2), []
        elif stamp is not None:
            body.append(line)
    flush()
    return out


def read_daylog(daily_dir: Path) -> list[Doc]:
    """Every daylog entry in the store, oldest day first."""
    out: list[Doc] = []
    for path in daylog_files(daily_dir):
        out.extend(parse_daylog(path))
    return out
