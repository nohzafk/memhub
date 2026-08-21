# memhub server

The HTTP face of the shared stores. See
the contract; this file is only how to run it.

```sh
uv sync
uv run pytest            # gate G1
uv run ruff format --check . && uv run ruff check .

MEMHUB_DATA=/tmp/memhub-dev \
MEMHUB_TOKEN_FILE=/tmp/memhub-dev/token \
uv run uvicorn memhub.app:app --host 127.0.0.1 --port 8900
```

The store is never created by the server: run `MEMORY_DIR=$MEMHUB_DATA/optmem
uv run python vendor/memo.py init` once, as provisioning does.

## Layout

| File | Holds |
|---|---|
| `memhub/settings.py` | every path, read from the environment, no filesystem access |
| `memhub/store.py` | decoding LOG.txt records and daylog entry blocks |
| `memhub/runner.py` | `/run`: the verb allowlist, the write lock, the subprocess |
| `memhub/scope.py` | `optmem/META.jsonl`: who wrote a memory, and where it is true |
| `memhub/app.py` | the endpoints and the bearer token |

## Scope metadata

`optmem/META.jsonl` records **machine, project, actor** and an
**applicability** for every memory — out of band, never in the memory's text.

- Written on `/run` when the client sends `scope`, inside the write's own lock.
  New refs come from the log length before and after, never from parsing ids out
  of `memo`'s prose.
- Read back on `/run` as `scopes`, for the footer the client appends. The tool's
  own stdout is never rewritten.
- `GET /verify` reports rows that match no memory, and memories with no row.
- Every row carries a fingerprint. A mismatch **suppresses the annotation**
  rather than printing a stale one, because refs are byte offsets and a
  re-imported store shifts all of them.

## Environment

| Variable | Default |
|---|---|
| `MEMHUB_DATA` | `/var/lib/memhub` |
| `MEMHUB_TOKEN_FILE` | `/etc/memhub/token` |
| `MEMHUB_HOST_PROJECTS` | empty — colon-separated projects whose memories are machine-bound |
| `MEMHUB_VENDOR` | `server/vendor` beside the package |

## Vendored tools

`vendor/memo.py` and `vendor/daylog.py` are copies, never edited here. The
machine-config repo that installs the clients is the canonical source for both:
`memo.py` originates upstream at `VictorTaelin/OptMem` and is vendored through
that repo, and `daylog.py` is written there outright. Verify both before
deploying:

```sh
sha256sum server/vendor/memo.py     # 3dc120d01be3115ef6267eab4103e7909fc830d6227b549f20991ba999ee9ffb
sha256sum server/vendor/daylog.py   # 573a345b77485ab7c1601e0ddb815dd9b23cffa1830972e5a2c6d47f15d8473b
```

`memo.py` here is the unpatched upstream file. `deploy.sh` patches `ME` to
`"memo -g"` at deploy time, so the continuation commands it prints
route back to the shared store from any machine.

`daylog.py` implements `grep` and `read`, which the server had allowlisted well
before the tool implemented them. It is Python: `runner.py` runs both vendored tools with
`sys.executable`, where daylog used to be `bash daylog.sh`. So a vendoring step
no longer has to preserve a mode bit or a shebang, and the container needs no
particular bash. See `tests/test_run.py::test_daylog_grep_and_read_return_content`.
