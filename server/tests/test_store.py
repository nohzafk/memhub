"""Decoding both stores, including the cases that break naive parsers."""

from __future__ import annotations

from pathlib import Path

from memhub.store import (
    LOG_REC,
    daylog_days,
    memo_count,
    parse_daylog,
    read_daylog,
    read_memos,
)


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
    assert daylog_days(tmp_path / "nothing") == 0


def test_parses_daylog_entry_blocks(tmp_path: Path):
    day = tmp_path / "2026-08-19.md"
    day.write_text(
        "<!-- 2026-08-19 14:03:22 -->\n"
        "Second entry, written later — daylog prepends.\n"
        "It runs over two lines.\n"
        "\n"
        "<!-- 2026-08-19 09:12:01 -->\n"
        "Erste Notiz: Kühlschrank läuft 🧊\n"
        "\n",
        encoding="utf-8",
    )

    docs = parse_daylog(day)

    assert [d.ref for d in docs] == ["2026-08-19/14:03:22", "2026-08-19/09:12:01"]
    assert all(d.date == "2026-08-19" and d.source == "daylog" for d in docs)
    assert docs[0].text == (
        "Second entry, written later — daylog prepends.\nIt runs over two lines."
    )
    assert docs[1].text == "Erste Notiz: Kühlschrank läuft 🧊"


def test_two_entries_in_one_second_get_distinct_refs(tmp_path: Path):
    day = tmp_path / "2026-08-19.md"
    day.write_text(
        "<!-- 2026-08-19 09:00:00 -->\nsecond\n\n<!-- 2026-08-19 09:00:00 -->\nfirst\n",
        encoding="utf-8",
    )

    docs = parse_daylog(day)

    assert [d.ref for d in docs] == ["2026-08-19/09:00:00", "2026-08-19/09:00:00.2"]


def test_empty_entries_and_preamble_are_skipped(tmp_path: Path):
    day = tmp_path / "2026-08-19.md"
    day.write_text(
        "a hand-written heading above every stamp\n"
        "<!-- 2026-08-19 10:00:00 -->\n\n\n"
        "<!-- 2026-08-19 09:00:00 -->\nreal content\n",
        encoding="utf-8",
    )

    assert [d.text for d in parse_daylog(day)] == ["real content"]


def test_only_dated_markdown_files_are_logs(tmp_path: Path):
    (tmp_path / "2026-08-19.md").write_text(
        "<!-- 2026-08-19 09:00:00 -->\nreal\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text(
        "<!-- 2026-08-19 09:00:00 -->\nnot a log\n", encoding="utf-8"
    )
    (tmp_path / "2026-08-19.md.bak").write_text("backup", encoding="utf-8")

    assert daylog_days(tmp_path) == 1
    assert [d.text for d in read_daylog(tmp_path)] == ["real"]
