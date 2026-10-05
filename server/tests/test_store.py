"""Decoding the OptMem store, including the cases that break naive parsers."""

from __future__ import annotations

from pathlib import Path

from memhub.store import LOG_REC, memo_count, read_memos


def write_log(path: Path, entries: list[tuple[str, str]]) -> None:
    """Write LOG.txt the way memo.py does: fixed-width records, padded in
    BYTES, so a multi-byte character shortens the visible text, not the
    record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        for i, (date, text) in enumerate(entries):
            rec = f"#{i} {date} {text}".encode()
            f.write(rec + b" " * (LOG_REC - 1 - len(rec)) + b"\n")


def test_reads_fixed_width_records_with_multibyte_text(tmp_path: Path):
    log = tmp_path / "optmem" / "LOG.txt"
    write_log(
        log,
        [
            ("2026-08-17", "plain ascii memory"),
            ("2026-08-18", "der Kühlschrank → 4 °C, ±0.5"),
            ("2026-08-19", "🧊 emoji and ünïcödé together"),
        ],
    )
    docs = read_memos(tmp_path / "optmem")

    assert memo_count(tmp_path / "optmem") == 3
    assert [d.ref for d in docs] == ["#0", "#1", "#2"]
    assert [d.date for d in docs] == ["2026-08-17", "2026-08-18", "2026-08-19"]
    assert docs[1].text == "der Kühlschrank → 4 °C, ±0.5"
    assert docs[2].text == "🧊 emoji and ünïcödé together"


def test_reads_from_an_offset(tmp_path: Path):
    log = tmp_path / "optmem" / "LOG.txt"
    write_log(log, [("2026-08-19", f"memory {i}") for i in range(5)])

    docs = read_memos(tmp_path / "optmem", start=3)

    assert [d.ref for d in docs] == ["#3", "#4"]


def test_partial_trailing_record_is_not_a_memory(tmp_path: Path):
    """A crash mid-append leaves a short record. memo.py truncates it on the
    next write; until then it was never acknowledged and is not a memory."""
    log = tmp_path / "optmem" / "LOG.txt"
    write_log(log, [("2026-08-19", "complete")])
    with log.open("ab") as f:
        f.write(b"#1 2026-08-19 half-writ")

    assert memo_count(tmp_path / "optmem") == 1
    assert [d.ref for d in read_memos(tmp_path / "optmem")] == ["#0"]


def test_missing_store_reads_as_empty(tmp_path: Path):
    assert memo_count(tmp_path / "nothing") == 0
    assert read_memos(tmp_path / "nothing") == []
