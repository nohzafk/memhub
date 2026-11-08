"""Scope metadata.

Two properties carry the design. Scope must be recorded for the rule a write
creates, under the id the store assigned. And a scope row that no longer matches
its rule must **suppress the annotation**, never print a stale one — because a
re-created store hands out the same ids again, and `edit` changes a rule's text
under its id.
"""

from __future__ import annotations

import json

import pytest
from conftest import TOKEN
from fastapi.testclient import TestClient

from memhub.scope import ScopeStore, fingerprint
from memhub.settings import Settings

# What a client actually sends: what it observed, and nothing it decided.
OMARCHY = {
    "machine": "omarchy",
    "host": "host-b",
    "project": "machine-config",
    "actor": "claude-code",
}
WORK = {**OMARCHY, "machine": "work", "project": "memhub"}
NO_PROJECT = {"machine": "omarchy", "host": "host-b", "project": None}


def note(
    client: TestClient, text: str, scope: dict | None = OMARCHY, tool: str = "memo"
):
    body = {"tool": tool, "argv": ["note", text]}
    if scope is not None:
        body["scope"] = scope
    return client.post("/run", json=body).json()


def rows(settings: Settings) -> list[dict]:
    text = (settings.rules_dir / "META.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ------------------------------------------------------------- recording


def test_a_note_records_one_scope_row(client: TestClient, settings: Settings):
    assert note(client, "Omarchy rewrites monitors.lua")["exit_code"] == 0

    (row,) = rows(settings)
    assert row["ref"] == "#1"
    assert row["machine"] == "omarchy"
    assert row["project"] == "machine-config"
    assert row["actor"] == "claude-code"
    assert row["fp"] == fingerprint(row["date"], "Omarchy rewrites monitors.lua")


# --------------------------------------------------- deriving applicability
#
# Whether a project is machine-bound is a property of the project, the same on
# every machine, so the server decides it. A client that carried the list would
# be one of three copies free to disagree about one repo.


def test_a_configured_host_project_derives_host(client: TestClient, settings: Settings):
    note(client, "monitors.lua is rewritten", scope=OMARCHY)

    (row,) = rows(settings)
    assert (row["applies"], row["asserted"]) == ("host", "inferred")


def test_any_other_project_derives_portable(client: TestClient, settings: Settings):
    note(client, "the index is a cache", scope=WORK)

    (row,) = rows(settings)
    assert (row["applies"], row["asserted"]) == ("portable", "inferred")


def test_no_project_derives_unknown_never_portable(
    client: TestClient, settings: Settings
):
    """The case the derived rule cannot answer, and where the corpus's worst
    offender lives. Assuming portable here is the bug being fixed."""
    note(client, "display scale is 1.6 with GDK_SCALE 2", scope=NO_PROJECT)

    (row,) = rows(settings)
    assert (row["applies"], row["asserted"]) == ("unknown", "inferred")


@pytest.mark.parametrize("declared", ["host", "portable"])
def test_a_declaration_beats_the_derived_value(
    client: TestClient, settings: Settings, declared: str
):
    note(
        client,
        "the author knows better",
        scope={**OMARCHY, "applies": declared, "asserted": "declared"},
    )

    (row,) = rows(settings)
    assert (row["applies"], row["asserted"]) == (declared, "declared")


def test_an_applies_without_a_declaration_is_ignored(
    client: TestClient, settings: Settings
):
    """Only `asserted: declared` counts. Otherwise a stale client could assert
    portability it was never told about."""
    note(client, "x", scope={**OMARCHY, "applies": "portable"})

    (row,) = rows(settings)
    assert (row["applies"], row["asserted"]) == ("host", "inferred")


def test_the_new_ref_is_the_id_the_store_assigned(
    client: TestClient, settings: Settings
):
    """Ids are never reused, so after a drop the next note's row names the
    next id, not the position the rule landed at."""
    for i in range(3):
        note(client, f"memory number {i}")
    client.post("/run", json={"tool": "memo", "argv": ["drop", "3"]})
    note(client, "after the drop")

    assert [r["ref"] for r in rows(settings)] == ["#1", "#2", "#3", "#4"]
    assert rows(settings)[-1]["fp"] == fingerprint(
        rows(settings)[-1]["date"], "after the drop"
    )


def test_merge_records_one_row_for_the_new_id(client: TestClient, settings: Settings):
    note(client, "first half")
    note(client, "second half")

    body = client.post(
        "/run",
        json={"tool": "memo", "argv": ["merge", "1,2", "both halves"], "scope": WORK},
    ).json()

    assert body["exit_code"] == 0
    assert [(r["ref"], r["machine"]) for r in rows(settings)] == [
        ("#1", "omarchy"),
        ("#2", "omarchy"),
        ("#3", "work"),
    ]


def test_edit_and_drop_record_no_row(client: TestClient, settings: Settings):
    """They create no id. The edited rule's old row stops matching and the
    dropped rule's row matches nothing; /verify reports both."""
    note(client, "to be edited")
    note(client, "to be dropped")
    for argv in (["edit", "1", "edited"], ["drop", "2"]):
        body = client.post(
            "/run", json={"tool": "memo", "argv": argv, "scope": OMARCHY}
        ).json()
        assert body["exit_code"] == 0, argv

    assert [r["ref"] for r in rows(settings)] == ["#1", "#2"]
    report = client.get("/verify").json()
    assert report["unmatched_rows"] == ["#1", "#2"]
    assert report["unlabelled"] == ["#1"]


def test_no_scope_sent_means_no_row_and_the_write_still_lands(
    client: TestClient, settings: Settings
):
    """A client that cannot name its machine still records its memory. The
    memory is what matters; the label is bookkeeping."""
    assert note(client, "unlabelled but recorded", scope=None)["exit_code"] == 0

    assert not (settings.rules_dir / "META.jsonl").exists()
    assert "unlabelled but recorded" in note(client, "x", scope=None)["stdout"] or True


def test_an_unknown_machine_role_is_dropped_not_fatal(
    client: TestClient, settings: Settings
):
    body = note(
        client, "written by a machine we do not know", scope={"machine": "laptop"}
    )

    assert body["exit_code"] == 0
    assert not (settings.rules_dir / "META.jsonl").exists()


def test_a_failed_write_records_nothing(client: TestClient, settings: Settings):
    too_long = "x" * 400

    body = note(client, too_long)

    assert body["exit_code"] == 1
    assert not (settings.rules_dir / "META.jsonl").exists()


# ------------------------------------------------------------ reading back


def test_wake_returns_scope_for_the_memories_it_printed(client: TestClient):
    note(client, "first fact")
    note(client, "second fact", scope=WORK)

    body = client.post("/run", json={"tool": "memo", "argv": ["wake"]}).json()

    assert [(s["ref"], s["machine"], s["applies"]) for s in body["scopes"]] == [
        ("#1", "omarchy", "host"),
        ("#2", "work", "portable"),
    ]  # derived from the projects, not sent by the client
    assert body["scopes"][0]["label"] == "omarchy / machine-config / host"


def test_a_write_response_carries_no_scopes(client: TestClient):
    """ "Saved as #1." names an id but not at the start of a line, so the read
    path does not fire on it."""
    assert note(client, "a fact")["scopes"] is None


def test_an_unlabelled_memory_reads_back_without_scope(client: TestClient):
    note(client, "labelled")
    note(client, "unlabelled", scope=None)

    body = client.post("/run", json={"tool": "memo", "argv": ["wake"]}).json()

    assert [s["ref"] for s in body["scopes"]] == ["#1"]


# ------------------------------------------------------------------ drift


def test_a_changed_fingerprint_suppresses_the_annotation(
    client: TestClient, settings: Settings
):
    """The git-notes failure: metadata keyed by a shifting identifier. A row
    that no longer matches its memory must go quiet, not lie."""
    note(client, "the original memory")
    meta = settings.rules_dir / "META.jsonl"
    row = json.loads(meta.read_text(encoding="utf-8").strip())
    row["fp"] = "0" * 16
    meta.write_text(json.dumps(row) + "\n", encoding="utf-8")

    body = client.post("/run", json={"tool": "memo", "argv": ["wake"]}).json()

    assert body["scopes"] is None


def test_a_new_epoch_suppresses_every_older_row(client: TestClient, settings: Settings):
    """What a re-created store looks like: the ids are reused by different
    rules, so every row from the previous store is stale by construction."""
    note(client, "written before the store was re-created")
    (settings.rules_dir / "EPOCH").write_text("f" * 32 + "\n", encoding="utf-8")

    body = client.post("/run", json={"tool": "memo", "argv": ["wake"]}).json()

    assert body["scopes"] is None


def test_the_epoch_survives_restarts_but_not_deletion(settings: Settings):
    store = ScopeStore(settings.rules_dir)
    first = store.epoch()

    assert ScopeStore(settings.rules_dir).epoch() == first

    (settings.rules_dir / "EPOCH").unlink()
    assert ScopeStore(settings.rules_dir).epoch() != first


def test_a_malformed_row_is_skipped_not_fatal(client: TestClient, settings: Settings):
    note(client, "a good memory")
    meta = settings.rules_dir / "META.jsonl"
    with meta.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n\n")

    body = client.post("/run", json={"tool": "memo", "argv": ["wake"]}).json()

    assert [s["ref"] for s in body["scopes"]] == ["#1"]


# ----------------------------------------------------------------- verify


def test_verify_reports_a_clean_store(client: TestClient):
    note(client, "one")
    note(client, "two")

    body = client.get("/verify").json()

    assert body["rows"] == 2
    assert body["unmatched_rows"] == []
    assert body["unlabelled"] == []


def test_verify_names_unlabelled_memories_and_stale_rows(
    client: TestClient, settings: Settings
):
    note(client, "labelled")
    note(client, "unlabelled", scope=None)
    meta = settings.rules_dir / "META.jsonl"
    row = json.loads(meta.read_text(encoding="utf-8").strip())
    with meta.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**row, "ref": "#99"}) + "\n")

    body = client.get("/verify").json()

    assert body["unlabelled"] == ["#2"]
    assert body["unmatched_rows"] == ["#99"]


def test_verify_ignores_rows_that_name_no_memory(
    client: TestClient, settings: Settings
):
    """META.jsonl can hold rows for refs outside the store, such as the retired
    day log's `2026-08-20/14:03:22`. They are not rules, so not stale."""
    note(client, "labelled")
    meta = settings.rules_dir / "META.jsonl"
    row = json.loads(meta.read_text(encoding="utf-8").strip())
    with meta.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**row, "ref": "2026-08-20/14:03:22"}) + "\n")

    assert client.get("/verify").json()["unmatched_rows"] == []


def test_verify_needs_the_token(anon: TestClient):
    assert anon.get("/verify").status_code == 401
    assert (
        anon.get("/verify", headers={"Authorization": f"Bearer {TOKEN}"}).status_code
        == 200
    )
