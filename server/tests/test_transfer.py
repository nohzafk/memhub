"""`/export`, the mirror's copy of the store, and the retired `/import`.

A mirror is safe *because* it is a byte copy that nothing writes to: ids,
history and `META.jsonl` all stay valid, so it can be promoted to be the store.
"""

from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path

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
    promoted mirror keeps every id, its history and its scope."""
    client.post(
        "/run", json={"tool": "memo", "argv": ["note", "a fact"], "scope": OMARCHY}
    )

    files = members(client.get("/export").content)

    d = settings.rules_dir
    for name in ("RULES.txt", "NEXT", "META.jsonl", "EPOCH"):
        assert files[f"rules/{name}"] == (d / name).read_bytes(), name
    assert "rules/.git/HEAD" in files
    assert not any(name.startswith(("daily/", "optmem/")) for name in files)


def test_export_carries_the_history(
    client: TestClient, settings: Settings, tmp_path: Path
):
    client.post("/run", json={"tool": "memo", "argv": ["note", "a fact"]})
    with tarfile.open(fileobj=io.BytesIO(client.get("/export").content)) as tar:
        tar.extractall(tmp_path / "mirror", filter="data")

    log = subprocess.run(
        ["git", "-C", str(tmp_path / "mirror" / "rules"), "log", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert log.splitlines() == ["note #1", "init"]


def test_export_leaves_the_lock_file_out(client: TestClient, settings: Settings):
    """It is a lock, not data, and the tool recreates it."""
    client.post("/run", json={"tool": "memo", "argv": ["note", "a fact"]})
    assert (settings.rules_dir / ".lock").exists()

    assert not any(
        name.endswith(".lock") for name in members(client.get("/export").content)
    )


def test_export_of_a_fresh_store_still_unpacks(client: TestClient):
    files = members(client.get("/export").content)
    assert files["rules/RULES.txt"] == b""
    assert files["rules/NEXT"] == b"1\n"


def test_export_needs_the_token(anon: TestClient):
    assert anon.get("/export").status_code == 401
    assert (
        anon.get("/export", headers={"Authorization": f"Bearer {TOKEN}"}).status_code
        == 200
    )


# ------------------------------------------------------------------ import


def test_import_is_gone(client: TestClient):
    """Its format was OptMem's. A local rule reaches the shared store as a
    `note` over `/run`."""
    r = client.post("/import", json={"text": "2026-08-01 x\n"})
    assert r.status_code == 404
