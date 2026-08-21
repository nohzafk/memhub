"""`/export` and `/import` — the mirror and the way back.

These two carry the whole-copy discipline. A mirror is safe *because* it is a
byte copy that nothing writes to: ids, `TREE/` and `META.jsonl` all stay valid,
so it can be promoted to be the store. `/import` is the only way memories written
to a second store can rejoin the first, and it works only because `memo import`
appends in date order — which is why the local tail, being newest, fits.
"""

from __future__ import annotations

import io
import json
import tarfile

from conftest import TOKEN
from fastapi.testclient import TestClient

from memhub.settings import Settings

OMARCHY = {"machine": "omarchy", "host": "host-b", "project": None}


def members(body: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        return {
            m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()
        }


# ------------------------------------------------------------------ export


def test_export_is_a_byte_copy_of_the_store(client: TestClient, settings: Settings):
    """The promise a mirror rests on: what comes out is what is on disk, so a
    promoted mirror keeps every id and every summary."""
    client.post(
        "/run", json={"tool": "memo", "argv": ["note", "a fact"], "scope": OMARCHY}
    )
    client.post("/run", json={"tool": "daylog", "argv": ["note", "a day"]})

    body = client.get("/export").content

    files = members(body)
    assert files["optmem/LOG.txt"] == (settings.optmem_dir / "LOG.txt").read_bytes()
    assert (
        files["optmem/META.jsonl"] == (settings.optmem_dir / "META.jsonl").read_bytes()
    )
    assert "optmem/config" in files
    assert any(name.startswith("daily/") and name.endswith(".md") for name in files)


def test_export_leaves_the_lock_file_out(client: TestClient, settings: Settings):
    """It is a lock, not data, and `memo` recreates it."""
    client.post("/run", json={"tool": "memo", "argv": ["note", "a fact"]})
    assert (settings.optmem_dir / ".lock").exists()

    assert not any(".lock" in name for name in members(client.get("/export").content))


def test_export_of_a_fresh_store_still_unpacks(client: TestClient):
    files = members(client.get("/export").content)
    assert files["optmem/LOG.txt"] == b""


def test_export_needs_the_token(anon: TestClient):
    assert anon.get("/export").status_code == 401
    assert (
        anon.get("/export", headers={"Authorization": f"Bearer {TOKEN}"}).status_code
        == 200
    )


# ------------------------------------------------------------------ import


def test_import_appends_and_records_scope(client: TestClient, settings: Settings):
    body = client.post(
        "/import",
        json={
            "text": "2026-08-01 promoted one\n2026-08-02 promoted two\n",
            "scope": OMARCHY,
        },
    ).json()

    assert body["exit_code"] == 0
    assert "Imported 2 memories, #0 to #1." in body["stdout"]
    rows = [
        json.loads(line)
        for line in (settings.optmem_dir / "META.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert [(r["ref"], r["machine"], r["applies"]) for r in rows] == [
        ("#0", "omarchy", "unknown"),
        ("#1", "omarchy", "unknown"),
    ]


def test_import_without_scope_still_appends(client: TestClient, settings: Settings):
    body = client.post("/import", json={"text": "2026-08-01 unlabelled\n"}).json()

    assert body["exit_code"] == 0
    assert not (settings.optmem_dir / "META.jsonl").exists()


def test_an_out_of_order_date_is_refused_and_writes_nothing(
    client: TestClient, settings: Settings
):
    """`memo import` refuses a line older than the store's newest memory. That is
    the rule the whole write-back path depends on, so the failure has to be clean
    and the tool's own message has to reach the caller."""
    client.post("/run", json={"tool": "memo", "argv": ["note", "today's memory"]})
    before = (settings.optmem_dir / "LOG.txt").read_bytes()

    body = client.post("/import", json={"text": "2020-01-01 far too old\n"}).json()

    assert body["exit_code"] == 1
    assert "precedes the previous memory" in body["stderr"]
    assert (settings.optmem_dir / "LOG.txt").read_bytes() == before


def test_an_empty_dump_is_a_400(client: TestClient):
    assert client.post("/import", json={"text": "   \n"}).status_code == 400


def test_the_staged_file_never_survives(client: TestClient, settings: Settings):
    """It is written into the data dir because the unit's PrivateTmp hides /tmp
    from the service. It must not accumulate there."""
    client.post("/import", json={"text": "2026-08-01 one\n"})
    client.post("/import", json={"text": "2020-01-01 refused\n"})

    assert list(settings.data_dir.glob(".import-*")) == []


def test_import_needs_the_token(anon: TestClient):
    assert anon.post("/import", json={"text": "2026-08-01 x\n"}).status_code == 401
