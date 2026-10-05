"""The HTTP surface.

Five endpoints. `/health` is unauthenticated so a probe can reach it; the others
need `Authorization: Bearer <token>`.

The endpoints are sync (`def`, not `async def`) on purpose: each one blocks on a
a subprocess or the filesystem, so FastAPI runs them in its threadpool instead
of stalling the event loop.
"""

from __future__ import annotations

import io
import logging
import os
import re
import secrets
import tarfile
import threading
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .runner import REF_CREATING, BadRequest, RunResult, check, write_lock
from .runner import run as run_tool
from .scope import Scope, ScopeRecorder, ScopeStore
from .settings import Settings
from .store import memo_count, read_memos

log = logging.getLogger("memhub")

# A memory as `memo` prints it, at the start of a line. Matching is read-only:
# stdout is never rewritten — the client appends a footer — so a pattern that
# finds nothing costs a missing annotation and never a mangled memory.
REF_RE = re.compile(r"^#(\d+) ", re.MULTILINE)


class ScopeModel(BaseModel):
    """Where the caller was standing. A local-scope client sends none:
    its reader's scope always matches, so every annotation would be elided."""

    machine: str
    host: str | None = None
    project: str | None = None
    actor: str | None = None
    # Sent only when the author declared it (`--here` / `--anywhere`). Left
    # unset, the server derives it from the project.
    applies: str | None = None
    asserted: str | None = None


class ScopeHit(BaseModel):
    ref: str
    machine: str
    project: str | None = None
    applies: str = "unknown"
    label: str


class RunRequest(BaseModel):
    tool: str
    argv: list[str] = Field(default_factory=list)
    scope: ScopeModel | None = None


class RunResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    # Scope for the memories named in `stdout`, for the client's footer. Only
    # rows whose fingerprint and epoch still match the memory appear here.
    scopes: list[ScopeHit] | None = None


class ImportRequest(BaseModel):
    """A dump in `memo import` format: `YYYY-MM-DD <text>` per line.

    The text travels in the body rather than as a path, because the unit runs
    with PrivateTmp and cannot see a file another process wrote to /tmp — and
    because a client on a workstation has no way to place a file on the server at all.
    """

    text: str
    scope: ScopeModel | None = None


class ImportResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


class VerifyResponse(BaseModel):
    epoch: str
    rows: int
    unmatched_rows: list[str]
    unlabelled: list[str]


class HealthResponse(BaseModel):
    ok: bool
    memo_count: int
    scope_rows: int


def require_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """One static bearer token for the single user.

    The file is read on every request, so rotation takes effect the moment
    deploy.sh writes the new value — no restart.
    """
    settings: Settings = request.app.state.settings
    try:
        expected = settings.token_file.read_text(encoding="utf-8").strip()
    except OSError:
        expected = ""
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=f"memhub: no token configured at {settings.token_file}",
        )
    given = ""
    if authorization and authorization.lower().startswith("bearer "):
        given = authorization[7:].strip()
    if not given or not secrets.compare_digest(given, expected):
        raise HTTPException(
            status_code=401, detail="memhub: bad or missing bearer token"
        )


def known_memories(settings: Settings) -> dict[str, tuple[str, str]]:
    """Every memory as `{ref: (date, text)}` — what a scope row is verified
    against."""
    return {doc.ref: (doc.date, doc.text) for doc in read_memos(settings.optmem_dir)}


def scopes_in(settings: Settings, stdout: str) -> list[ScopeHit] | None:
    """Scope for the memories `stdout` names, or None when it names none."""
    ids = {int(m.group(1)) for m in REF_RE.finditer(stdout)}
    if not ids:
        return None
    wanted = {
        doc.ref: (doc.date, doc.text)
        for doc in read_memos(settings.optmem_dir)
        if int(doc.ref[1:]) in ids
    }
    found = ScopeStore(settings.optmem_dir).lookup(wanted)
    if not found:
        return None
    return [
        ScopeHit(
            ref=ref,
            machine=scope.machine,
            project=scope.project,
            applies=scope.applies,
            label=scope.label(),
        )
        for ref, scope in sorted(found.items(), key=lambda kv: int(kv[0][1:]))
    ]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Nothing here touches the filesystem: uvicorn imports this
    module, and the tests build an app for a tmp dir that has no store yet."""
    settings = settings or Settings.from_env()
    app = FastAPI(title="memhub", version="0.1.0")
    app.state.settings = settings

    @app.get("/health", response_model=HealthResponse)
    def health(request: Request) -> HealthResponse:
        s: Settings = request.app.state.settings
        return HealthResponse(
            ok=s.optmem_dir.is_dir(),
            memo_count=memo_count(s.optmem_dir),
            scope_rows=len(ScopeStore(s.optmem_dir).rows()),
        )

    @app.post("/run", response_model=RunResponse, dependencies=[Depends(require_token)])
    def run(request: Request, body: RunRequest) -> RunResponse:
        s: Settings = request.app.state.settings
        try:
            verb = check(body.tool, body.argv)
        except BadRequest as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        # Scope is recorded only for the writes that create refs, inside the same
        # lock, so a concurrent write cannot be credited to this caller.
        recorder = None
        scope = (
            Scope.parse(body.scope.model_dump(), s.host_projects)
            if body.scope
            else None
        )
        if scope is not None and (body.tool, verb) in REF_CREATING:
            recorder = ScopeRecorder(ScopeStore(s.optmem_dir), s.optmem_dir, scope)
        result: RunResult = run_tool(s, body.tool, body.argv, recorder)

        scopes = None
        if result.exit_code == 0:
            try:
                scopes = scopes_in(s, result.stdout)
            except OSError as e:
                # Annotation is never load-bearing: a read must succeed whether
                # or not scope could be looked up.
                log.warning("scope lookup failed: %s", e)
        return RunResponse(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            scopes=scopes,
        )

    @app.get("/export", dependencies=[Depends(require_token)])
    def export_store(request: Request) -> Response:
        """The whole store as a gzipped tar, for a machine keeping a mirror.

        Taken under the write lock, so a note landing mid-read cannot produce a
        torn `LOG.txt`. The store is tens of kilobytes, so the lock is held for
        milliseconds and the whole thing is built in memory.

        `.lock` is left out: it is a lock file, not data, and `memo` recreates it.
        """
        s: Settings = request.app.state.settings
        buffer = io.BytesIO()
        with write_lock(s.lock_path), tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            if s.optmem_dir.is_dir():
                tar.add(
                    s.optmem_dir,
                    arcname=s.optmem_dir.name,
                    filter=lambda info: None if info.name.endswith(".lock") else info,
                )
        return Response(
            content=buffer.getvalue(),
            media_type="application/gzip",
            headers={
                "Content-Disposition": 'attachment; filename="memhub-store.tar.gz"'
            },
        )

    @app.post(
        "/import", response_model=ImportResponse, dependencies=[Depends(require_token)]
    )
    def import_dump(request: Request, body: ImportRequest) -> ImportResponse:
        """Append a dump to the shared store — the write-back path.

        `memo import` reads a file, so the text is staged inside the data
        directory: the only place the unit can both write and read. It is removed
        whether the import succeeds or fails.

        Scope is recorded for every memory the import creates, by the same
        recorder and inside the same lock as any other write.
        """
        s: Settings = request.app.state.settings
        if not body.text.strip():
            raise HTTPException(status_code=400, detail="memhub: the dump is empty")

        staged = s.data_dir / f".import-{os.getpid()}-{threading.get_ident()}.txt"
        try:
            staged.write_text(body.text, encoding="utf-8")
            scope = (
                Scope.parse(body.scope.model_dump(), s.host_projects)
                if body.scope
                else None
            )
            recorder = (
                ScopeRecorder(ScopeStore(s.optmem_dir), s.optmem_dir, scope)
                if scope is not None
                else None
            )
            result = run_tool(s, "memo", ["import", str(staged)], recorder)
        finally:
            staged.unlink(missing_ok=True)
        return ImportResponse(
            exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr
        )

    @app.get(
        "/verify", response_model=VerifyResponse, dependencies=[Depends(require_token)]
    )
    def verify(request: Request) -> VerifyResponse:
        """The third drift defence: which scope rows match no memory,
        and which memories carry no scope."""
        s: Settings = request.app.state.settings
        store = ScopeStore(s.optmem_dir)
        report = store.audit(known_memories(s))
        return VerifyResponse(
            epoch=store.epoch(),
            rows=len(store.rows()),
            unmatched_rows=report["unmatched_rows"],
            unlabelled=report["unlabelled"],
        )

    return app


app = create_app()
