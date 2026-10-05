"""Reader for the OptMem store on disk.

The store is never written here: `memo.py` owns its files (see runner.py).
This module only decodes them, for scope and /health.

OptMem is position-is-identity: memory `#i` is the fixed 320-byte record at
offset `i*320` of LOG.txt, `"#<id> <YYYY-MM-DD> <text>"` right-padded with
spaces. Records are sliced as BYTES and decoded one at a time — slicing decoded
text would shift every boundary after the first multi-byte character.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

LOG_REC = 320


@dataclass(frozen=True)
class Doc:
    """One memory."""

    source: str  # always 'memo'
    ref: str  # '#123'
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
