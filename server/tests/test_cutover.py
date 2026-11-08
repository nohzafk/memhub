"""The cutover tools: dump and merge OptMem stores.

These two scripts run once, on the workstations, and decide what the shared memory is
made of. A silent mangling here is unrecoverable — the originals are archived
the same day — so the tests assert on exact bytes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# An OptMem LOG.txt: memory #i is the fixed 320-byte record at offset i*320,
# "#<id> <YYYY-MM-DD> <text>" right-padded with spaces, sliced as bytes.
LOG_REC = 320

TOOLS = Path(__file__).resolve().parents[2] / "tools"
DUMP = TOOLS / "dump_log.py"
MERGE = TOOLS / "merge_dumps.py"


def run(script: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *argv],
        capture_output=True,
        text=True,
        check=False,
    )


def write_log(path: Path, entries: list[tuple[str, str]]) -> None:
    """Write LOG.txt the way OptMem does: fixed-width records, padded in
    BYTES, so a multi-byte character shortens the visible text, not the
    record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        for i, (date, text) in enumerate(entries):
            rec = f"#{i} {date} {text}".encode()
            f.write(rec + b" " * (LOG_REC - 1 - len(rec)) + b"\n")


# ------------------------------------------------------------------ dump_log


def test_dump_round_trips_records_including_multibyte(tmp_path: Path):
    entries = [
        ("2026-08-17", "plain ascii memory"),
        ("2026-08-18", "der Kühlschrank → 4 °C, ±0.5"),
        ("2026-08-19", "🧊 emoji and ünïcödé together"),
    ]
    write_log(tmp_path / "LOG.txt", entries)

    r = run(DUMP, str(tmp_path))

    assert r.returncode == 0
    assert r.stdout == "".join(f"{d} {t}\n" for d, t in entries)
    assert "3 memories, 2026-08-17..2026-08-19" in r.stderr


def test_dump_writes_a_file_with_o(tmp_path: Path):
    write_log(tmp_path / "LOG.txt", [("2026-08-19", "one memory")])
    out = tmp_path / "dump.txt"

    assert run(DUMP, str(tmp_path), "-o", str(out)).returncode == 0
    assert out.read_text(encoding="utf-8") == "2026-08-19 one memory\n"


def test_dump_reads_records_laid_out_byte_by_byte(tmp_path: Path):
    """The other tests write LOG.txt through write_log. This one spells out
    OptMem's record layout by hand — 319 bytes of UTF-8 text padded with
    spaces, then a newline — so write_log and dump_log cannot drift together."""
    store = tmp_path / "optmem"
    store.mkdir()
    first = b"#0 2026-08-19 the build host reboots on Sundays"
    second = "#1 2026-08-19 backups run at 02:00 → off-box".encode()
    (store / "LOG.txt").write_bytes(
        first.ljust(319) + b"\n" + second.ljust(319) + b"\n"
    )
    assert (store / "LOG.txt").stat().st_size == 2 * LOG_REC

    r = run(DUMP, str(store))

    assert r.returncode == 0, r.stderr
    assert r.stdout == (
        "2026-08-19 the build host reboots on Sundays\n"
        "2026-08-19 backups run at 02:00 → off-box\n"
    )


def test_dump_ignores_a_partial_trailing_record(tmp_path: Path):
    write_log(tmp_path / "LOG.txt", [("2026-08-19", "complete")])
    with (tmp_path / "LOG.txt").open("ab") as f:
        f.write(b"#1 2026-08-19 half-writ")

    r = run(DUMP, str(tmp_path))

    assert r.stdout == "2026-08-19 complete\n"
    assert "partial trailing record" in r.stderr


def test_dump_refuses_a_misaligned_log(tmp_path: Path):
    """Position IS identity in OptMem: memory #i lives at offset i*320. A log
    whose ids do not match their offsets is not a log any more — dumping it
    would renumber memories silently, so it stops instead."""
    write_log(tmp_path / "LOG.txt", [("2026-08-19", "first"), ("2026-08-19", "second")])
    buf = bytearray((tmp_path / "LOG.txt").read_bytes())
    buf[LOG_REC : LOG_REC + 2] = b"#7"  # the second record now claims to be #7
    (tmp_path / "LOG.txt").write_bytes(bytes(buf))

    r = run(DUMP, str(tmp_path))

    assert r.returncode == 1
    assert "misaligned" in r.stderr


def test_dump_refuses_a_file_that_is_not_a_log(tmp_path: Path):
    """The dangerous shape: a file too short to hold one record would dump as
    "0 memories" and exit 0, which reads as "this machine had no memories"."""
    (tmp_path / "LOG.txt").write_bytes(b"#0 2026-08-19 a plain text line\n")

    r = run(DUMP, str(tmp_path))

    assert r.returncode == 1
    assert "holds no complete record" in r.stderr


def test_dump_of_a_fresh_empty_store_is_empty_and_fine(tmp_path: Path):
    """An empty log is a legitimate state — `memo init` creates one."""
    store = tmp_path / "optmem"
    store.mkdir()
    (store / "LOG.txt").write_bytes(b"")

    r = run(DUMP, str(store))

    assert r.returncode == 0
    assert r.stdout == ""
    assert "0 memories, empty" in r.stderr


def test_dump_names_a_missing_store(tmp_path: Path):
    r = run(DUMP, str(tmp_path / "nowhere"))
    assert r.returncode == 1
    assert "no memory at" in r.stderr


# --------------------------------------------------------------- merge_dumps


def test_merge_is_date_monotonic_and_stable(tmp_path: Path):
    personal = tmp_path / "personal-shared.txt"
    work = tmp_path / "work-shared.txt"
    personal.write_text(
        "2026-08-19 personal, later day\n2026-08-01 personal, first day\n",
        encoding="utf-8",
    )
    work.write_text(
        "2026-08-01 work, same first day\n2026-08-10 work, middle day\n",
        encoding="utf-8",
    )

    r = run(MERGE, str(personal), str(work))

    assert r.stdout.splitlines() == [
        "2026-08-01 personal, first day",
        "2026-08-01 work, same first day",
        "2026-08-10 work, middle day",
        "2026-08-19 personal, later day",
    ]
    dates = [line[:10] for line in r.stdout.splitlines()]
    assert dates == sorted(dates)


def test_merge_tie_break_follows_argument_order(tmp_path: Path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("2026-08-19 from a\n", encoding="utf-8")
    b.write_text("2026-08-19 from b\n", encoding="utf-8")

    assert run(MERGE, str(a), str(b)).stdout == "2026-08-19 from a\n2026-08-19 from b\n"
    assert run(MERGE, str(b), str(a)).stdout == "2026-08-19 from b\n2026-08-19 from a\n"


def test_merge_notes_an_input_that_lost_its_order(tmp_path: Path):
    f = tmp_path / "hand-edited.txt"
    f.write_text("2026-08-19 later\n2026-08-01 earlier\n", encoding="utf-8")

    r = run(MERGE, str(f))

    assert r.returncode == 0
    assert "not in date order" in r.stderr


@pytest.mark.parametrize(
    ("line", "complaint"),
    [
        ("19-08-2026 wrong order", "expected 'YYYY-MM-DD"),
        ("2026-02-30 not a real day", "not a real date"),
        ("2026-08-19", "no text after the date"),
        ("2026-08-19 " + "x" * 281, "limit 280"),
    ],
)
def test_merge_names_the_file_and_line_of_a_bad_entry(
    tmp_path: Path, line: str, complaint: str
):
    f = tmp_path / "classified.txt"
    f.write_text(f"2026-08-01 fine\n\n{line}\n", encoding="utf-8")

    r = run(MERGE, str(f))

    assert r.returncode == 1
    assert "line 3" in r.stderr and complaint in r.stderr


def test_merge_skips_blank_lines(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("\n2026-08-19 one\n   \n2026-08-19 two\n\n", encoding="utf-8")

    assert run(MERGE, str(f)).stdout == "2026-08-19 one\n2026-08-19 two\n"


# --------------------------------------------------------- scope at seed


def test_merge_writes_scope_for_every_line_from_its_file(tmp_path: Path):
    (tmp_path / "p.txt").write_text("2026-07-01 personal one\n", encoding="utf-8")
    (tmp_path / "o.txt").write_text(
        "2026-07-01 omarchy one\n2026-08-01 omarchy two\n", encoding="utf-8"
    )
    meta = tmp_path / "META.jsonl"

    r = run(
        MERGE,
        f"personal:{tmp_path / 'p.txt'}",
        f"omarchy:{tmp_path / 'o.txt'}",
        "-o",
        str(tmp_path / "seed.txt"),
        "--meta",
        str(meta),
    )

    assert r.returncode == 0, r.stderr
    rows = [json.loads(line) for line in meta.read_text(encoding="utf-8").splitlines()]
    # Ids follow the merged order: personal first on the shared date (argv order).
    assert [(x["ref"], x["machine"]) for x in rows] == [
        ("#0", "personal"),
        ("#1", "omarchy"),
        ("#2", "omarchy"),
    ]
    assert all(x["applies"] == "unknown" for x in rows)
    # The tally is sorted, so assert on content rather than on order.
    assert "personal 1" in r.stderr and "omarchy 2" in r.stderr


def test_meta_refuses_a_file_with_no_role(tmp_path: Path):
    (tmp_path / "a.txt").write_text("2026-08-19 one\n", encoding="utf-8")

    r = run(MERGE, str(tmp_path / "a.txt"), "--meta", str(tmp_path / "m.jsonl"))

    assert r.returncode == 1
    assert "needs a machine role" in r.stderr
    assert not (tmp_path / "m.jsonl").exists()


def test_a_role_prefix_is_not_confused_with_a_path(tmp_path: Path):
    """Only a known role counts as a prefix, so a path containing a colon is
    still a path."""
    odd = tmp_path / "weird:name.txt"
    odd.write_text("2026-08-19 one\n", encoding="utf-8")

    assert run(MERGE, str(odd)).returncode == 0
