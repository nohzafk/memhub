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
| `memhub/store.py` | decoding LOG.txt records |
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

`vendor/memo.py` is a copy, never edited here. The machine-config repo that
installs the client is its canonical source: `memo.py` originates upstream at
`VictorTaelin/OptMem` and is vendored through that repo. Verify it before
deploying:

```sh
sha256sum server/vendor/memo.py     # 3dc120d01be3115ef6267eab4103e7909fc830d6227b549f20991ba999ee9ffb
```

`memo.py` here is the unpatched upstream file. `deploy.sh` patches `ME` to
`"memo -g"` at deploy time, so the continuation commands it prints
route back to the shared store from any machine.
