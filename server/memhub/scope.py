"""Scope metadata: who wrote a rule, and where it is true.

Held in `rules/META.jsonl`, one JSON object per rule, never in the rule's own
text — a text tag is paid twice, cannot be elided when it already matches the
reader, and costs bytes of the 280 a rule may hold.

Two facts of different kinds live here. **Provenance** — machine, host, project,
actor — is objective and derived for free by the client. **Applicability** —
whether a fact is true anywhere or only on the machine that wrote it — is a
judgement. They come apart constantly, so they are separate fields.

A ref is one rule id, `#<N>`. Ids are never reused within a store, but a store
that is re-created starts again at #1, and `memo edit` changes a rule's text
under its id. Two defences make either detectable rather than silent: every row
carries a fingerprint of the rule it describes, and every row carries the
store's epoch. A mismatch on either **suppresses the annotation**; it never
prints a guess.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

# A role, not a hostname. Hostnames rot, and "the laptop" is already ambiguous
# with two Macs in one store.
MACHINES = ("work", "personal", "omarchy", "nuc", "cloud", "phone")
APPLIES = ("portable", "host", "unknown")
ASSERTED = ("declared", "inferred")

# The line `note` and `merge` print for the id they created.
SAVED_RE = re.compile(r"^Saved as #(\d+)\.$", re.MULTILINE)

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
    """`rules/META.jsonl` plus the store's epoch.

    Append-only. It lives inside the store directory on purpose: `/export` and
    a restore both copy whole store directories, so metadata kept anywhere else
    is orphaned by them. It is not tracked by the store's git repository; the
    rules tool commits only RULES.txt and NEXT.

    `edit` and `drop` write no row. A dropped rule's row becomes an unmatched
    ref and an edited rule's row no longer matches its fingerprint; `/verify`
    reports both, and neither is ever printed as an annotation.
    """

    def __init__(self, store_dir: Path):
        self.dir = store_dir
        self.path = store_dir / META_NAME
        self.epoch_path = store_dir / EPOCH_NAME

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
        # Only `#N` refs name rules. Rows with any other ref describe entries
        # outside the store and are neither stale nor auditable here.
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
    """Records scope for the rule a write creates, inside the write's own lock.

    The new id is read from the line `note` and `merge` print,
    `Saved as #<id>.`, and the rule is then read back from the store, so the
    row's fingerprint is of what was stored, not of what was sent.
    """

    def __init__(self, store: ScopeStore, rules_dir: Path, scope: Scope):
        self.store = store
        self.rules_dir = rules_dir
        self.scope = scope

    def commit(self, stdout: str) -> int:
        from .rules import read_rules

        m = SAVED_RE.search(stdout)
        if m is None:
            return 0
        rule_id = int(m.group(1))
        new = [r for r in read_rules(self.rules_dir) if r.id == rule_id]
        return self.store.record(
            [(f"#{r.id}", r.date, r.text, self.scope) for r in new]
        )
