"""The HTTP surface.

Four endpoints: `/health`, `/run`, `/export` and `/verify`. `/health` is
unauthenticated so a probe can reach it; the others need
`Authorization: Bearer <token>`.

The endpoints are sync (`def`, not `async def`) on purpose: each one blocks on
the filesystem or a `git` subprocess, so FastAPI runs them in its threadpool
instead of stalling the event loop.
"""

from __future__ import annotations

import io
import logging
import re
import secrets
import tarfile
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import rules
from .rules import RulesError
from .runner import SCOPE_RECORDING, BadRequest, RunResult, check, write_lock
from .runner import run as run_tool
from .scope import Scope, ScopeRecorder, ScopeStore
from .settings import Settings

log = logging.getLogger("memhub")

# A rule as `memo` prints it, at the start of a line. Matching is read-only:
# stdout is never rewritten — the client appends a footer — so a pattern that
# finds nothing costs a missing annotation and never a mangled rule. A wake's
# `## <tag>` group lines do not match.
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
    # Scope for the rules named in `stdout`, for the client's footer. Only
    # rows whose fingerprint and epoch still match the rule appear here.
    scopes: list[ScopeHit] | None = None


class VerifyResponse(BaseModel):
    epoch: str
    rows: int
    unmatched_rows: list[str]
    unlabelled: list[str]


class HealthResponse(BaseModel):
    ok: bool
    rule_count: int
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


def known_rules(settings: Settings) -> dict[str, tuple[str, str]]:
    """Every rule as `{ref: (date, text)}` — what a scope row is verified
    against. An uninitialised store has none."""
    try:
        found = rules.read_rules(settings.rules_dir)
    except RulesError:
        return {}
    return {f"#{r.id}": (r.date, r.text) for r in found}


def scopes_in(settings: Settings, stdout: str) -> list[ScopeHit] | None:
    """Scope for the rules `stdout` names, or None when it names none."""
    ids = {int(m.group(1)) for m in REF_RE.finditer(stdout)}
    if not ids:
        return None
    wanted = {
        ref: dt for ref, dt in known_rules(settings).items() if int(ref[1:]) in ids
    }
    found = ScopeStore(settings.rules_dir).lookup(wanted)
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
            ok=(s.rules_dir / rules.RULES).is_file(),
            rule_count=rules.rule_count(s.rules_dir),
            scope_rows=len(ScopeStore(s.rules_dir).rows()),
        )

    @app.post("/run", response_model=RunResponse, dependencies=[Depends(require_token)])
    def run(request: Request, body: RunRequest) -> RunResponse:
        s: Settings = request.app.state.settings
        try:
            verb = check(body.tool, body.argv)
        except BadRequest as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        # Scope is recorded only for the writes that create or edit a rule,
        # inside the same lock, so a concurrent write cannot be credited to
        # this caller.
        recorder = None
        scope = (
            Scope.parse(body.scope.model_dump(), s.host_projects)
            if body.scope
            else None
        )
        if scope is not None and (body.tool, verb) in SCOPE_RECORDING:
            recorder = ScopeRecorder(ScopeStore(s.rules_dir), s.rules_dir, scope)
        # The commit's author is the writing machine's role and host.
        author = (
            " ".join(p for p in (scope.machine, scope.host) if p) if scope else None
        )
        result: RunResult = run_tool(s, body.tool, body.argv, recorder, author)

        scopes = None
        if result.exit_code == 0:
            try:
                scopes = scopes_in(s, result.stdout)
            except (OSError, RulesError) as e:
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
        """The rules store as a gzipped tar, for a machine keeping a mirror: the
        repository with its history (`.git`), `META.jsonl` and `EPOCH`.

        Taken under the write lock, so a write landing mid-read cannot produce a
        torn copy. The store is tens of kilobytes, so the lock is held for
        milliseconds and the whole thing is built in memory.

        `.lock` is left out: it is a lock file, not data, and the tool recreates
        it.
        """
        s: Settings = request.app.state.settings
        buffer = io.BytesIO()
        with write_lock(s.lock_path), tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            if s.rules_dir.is_dir():
                tar.add(
                    s.rules_dir,
                    arcname=s.rules_dir.name,
                    filter=lambda info: None if info.name.endswith(".lock") else info,
                )
        return Response(
            content=buffer.getvalue(),
            media_type="application/gzip",
            headers={
                "Content-Disposition": 'attachment; filename="memhub-store.tar.gz"'
            },
        )

    @app.get(
        "/verify", response_model=VerifyResponse, dependencies=[Depends(require_token)]
    )
    def verify(request: Request) -> VerifyResponse:
        """The third drift defence: which scope rows match no rule, and which
        rules carry no scope."""
        s: Settings = request.app.state.settings
        store = ScopeStore(s.rules_dir)
        report = store.audit(known_rules(s))
        return VerifyResponse(
            epoch=store.epoch(),
            rows=len(store.rows()),
            unmatched_rows=report["unmatched_rows"],
            unlabelled=report["unlabelled"],
        )

    return app


app = create_app()
