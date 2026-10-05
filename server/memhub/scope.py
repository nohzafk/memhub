"""Scope metadata: who wrote a memory, and where it is true.

Held in `optmem/META.jsonl`, one JSON object per memory, never in the memory's
own text — a text tag is paid twice, cannot be elided when it already matches the
reader, and cannot survive `nap`, because a summary of 16 memories from 3
machines must itself fit the same 280 bytes.

Two facts of different kinds live here. **Provenance** — machine, host, project,
actor — is objective and derived for free by the client. **Applicability** —
whether a fact is true anywhere or only on the machine that wrote it — is a
judgement. They come apart constantly, so they are separate fields.

Refs are byte offsets, so a wiped and re-imported store shifts every id and would
leave this file describing the wrong memories. Two defences make that detectable
rather than silent: every row carries a fingerprint of the memory it describes,
and every row carries the store's epoch. A mismatch on either **suppresses the
annotation**; it never prints a guess.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

# A role, not a hostname. Hostnames rot, and "the laptop" is already ambiguous
# with two Macs in one store.
MACHINES = ("work", "personal", "omarchy", "nuc", "cloud", "phone")
APPLIES = ("portable", "host", "unknown")
ASSERTED = ("declared", "inferred")

META_NAME = "META.jsonl"
EPOCH_NAME = "EPOCH"


@dataclass(frozen=True)
class Scope:
    """One memory's scope. `machine` is the only required field: a client that
    knows nothing else still says where it was standing."""

    machine: str
    host: str | None = None
    project: str | None = None
    actor: str | None = None
    applies: str = "unknown"
    asserted: str = "inferred"

    @classmethod
    def parse(cls, raw: dict, host_projects: tuple[str, ...] = ()) -> Scope | None:
        """Build a Scope from client-supplied data, dropping anything invalid.

        The client reports only what it observed. Applicability is settled here:
        an author's declaration wins, and otherwise it is derived from the
        project, which predicts it far better than the machine does. With no
        project there is nothing to derive from, so the answer is `unknown` —
        recorded and rendered visibly, never guessed.

        Odd values are normalised rather than rejected: scope is an annotation,
        and a write must never fail because its label was strange.
        """
        machine = str(raw.get("machine") or "").strip()
        if machine not in MACHINES:
            return None
        project = _text(raw.get("project"))
        declared = raw.get("applies")
        if declared in APPLIES and raw.get("asserted") == "declared":
            applies, asserted = declared, "declared"
        elif project is None:
            applies, asserted = "unknown", "inferred"
        elif project in host_projects:
            applies, asserted = "host", "inferred"
        else:
            applies, asserted = "portable", "inferred"
        return cls(
            machine=machine,
            host=_text(raw.get("host")),
            project=project,
            actor=_text(raw.get("actor")),
            applies=applies,
            asserted=asserted,
        )

    @classmethod
    def stored(cls, row: dict) -> Scope | None:
        """Read a row back **as recorded**, deriving nothing.

        A stored row is a record of a decision already made, not a claim to
        re-evaluate. Re-deriving here would mean the same memory reads as
        `host` or `portable` depending on the policy of whoever is looking at
        it, which is exactly the inconsistency the single server-side list
        exists to prevent.
        """
        machine = str(row.get("machine") or "").strip()
        if machine not in MACHINES:
            return None
        return cls(
            machine=machine,
            host=_text(row.get("host")),
            project=_text(row.get("project")),
            actor=_text(row.get("actor")),
            applies=row.get("applies") if row.get("applies") in APPLIES else "unknown",
            asserted=row.get("asserted")
            if row.get("asserted") in ASSERTED
            else "inferred",
        )

    def label(self) -> str:
        """How a scope reads in a footer: machine / project / applies."""
        return f"{self.machine} / {self.project or 'no project'} / {self.applies}"


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:120] or None


def fingerprint(date: str, text: str) -> str:
    """Identifies a memory by content, so a shifted id is detectable."""
    return hashlib.sha256(f"{date}\0{text}".encode()).hexdigest()[:16]


class ScopeStore:
    """`optmem/META.jsonl` plus the store's epoch.

    Append-only, like the log it describes. It lives inside the store directory
    on purpose: seeding and restoring both re-import whole store directories, so
    metadata kept anywhere else is orphaned by the plan's own rollback steps.
    """

    def __init__(self, optmem_dir: Path):
        self.dir = optmem_dir
        self.path = optmem_dir / META_NAME
        self.epoch_path = optmem_dir / EPOCH_NAME

    def epoch(self) -> str:
        """The store's identity. Minted on first use and destroyed with the
        store, so a wipe-and-reimport cannot silently inherit stale rows."""
        try:
            value = self.epoch_path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            pass
        value = uuid.uuid4().hex
        self.epoch_path.parent.mkdir(parents=True, exist_ok=True)
        self.epoch_path.write_text(value + "\n", encoding="utf-8")
        return value

    def record(self, rows: list[tuple[str, str, str, Scope]]) -> int:
        """Append rows of (ref, date, text, scope). Returns rows written.

        The caller holds the write lock: this runs in the same critical section
        as the write it describes, so the two cannot interleave.
        """
        if not rows:
            return 0
        epoch = self.epoch()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            for ref, date, text, scope in rows:
                # `date` is redundant against `fp` for verification, and kept
                # anyway: it makes the file readable on its own, which matters
                # for a file a human reviews during the cutover.
                record = {
                    "ref": ref,
                    "epoch": epoch,
                    "fp": fingerprint(date, text),
                    "date": date,
                }
                record.update(asdict(scope))
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return len(rows)

    def rows(self) -> dict[str, dict]:
        """Every row by ref, last write winning. A malformed line is skipped
        rather than fatal: this file annotates, and annotation must never be able
        to take the store down."""
        out: dict[str, dict] = {}
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return out
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            ref = row.get("ref")
            if isinstance(ref, str):
                out[ref] = row
        return out

    def lookup(self, wanted: dict[str, tuple[str, str]]) -> dict[str, Scope]:
        """Scopes for `{ref: (date, text)}`, verified against the memory itself.

        A row whose fingerprint or epoch does not match the memory at that ref is
        dropped, not corrected. That is the whole drift defence: after a
        wipe-and-reimport every id names a different memory, and a stale
        annotation would be worse than none.
        """
        rows, epoch, out = self.rows(), self.epoch(), {}
        for ref, (date, text) in wanted.items():
            row = rows.get(ref)
            if row is None:
                continue
            # A row with no epoch is accepted on its fingerprint alone. That is
            # not a loophole: the fingerprint pins the content at that ref, so a
            # match means the row really does describe this memory. Epoch is the
            # cheap bulk signal — it catches a whole stale generation at once —
            # and it lets a seed file be written before the store exists.
            if row.get("epoch") is not None and row["epoch"] != epoch:
                continue
            if row.get("fp") != fingerprint(date, text):
                continue
            scope = Scope.stored(row)
            if scope is not None:
                out[ref] = scope
        return out

    def audit(self, known: dict[str, tuple[str, str]]) -> dict[str, list[str]]:
        """What `memhub verify` reports: rows that match no memory, and memories
        that have no row."""
        rows, epoch = self.rows(), self.epoch()
        matched = self.lookup(known)
        # Only `#N` refs name memories. Rows with any other ref describe entries
        # outside OptMem and are neither stale nor auditable here.
        stale = [
            ref
            for ref, row in rows.items()
            if ref.startswith("#")
            and ref not in matched
            and (row.get("epoch") == epoch or ref in known)
        ]
        return {
            "unmatched_rows": sorted(stale),
            "unlabelled": sorted(ref for ref in known if ref not in matched),
        }


class ScopeRecorder:
    """Records scope for whatever a write creates, inside the write's own lock.

    New refs are found by comparing the store before and after — never by
    parsing ids out of the tool's prose, which would make the record depend on
    wording that belongs to `memo.py`.
    """

    def __init__(
        self,
        store: ScopeStore,
        optmem_dir: Path,
        scope: Scope,
    ):
        self.store = store
        self.optmem_dir = optmem_dir
        self.scope = scope

    def snapshot(self) -> int:
        from .store import memo_count

        return memo_count(self.optmem_dir)

    def commit(self, snapshot: int) -> int:
        from .store import read_memos

        new = read_memos(self.optmem_dir, snapshot)
        return self.store.record([(d.ref, d.date, d.text, self.scope) for d in new])
