# memhub server

The HTTP face of the shared rules store. See `docs/rules-store.md` for the
contract; this file is only how to run it.

```sh
uv sync
uv run pytest            # gate G1
uv run ruff format --check . && uv run ruff check .

MEMHUB_DATA=/tmp/memhub-dev \
MEMHUB_TOKEN_FILE=/tmp/memhub-dev/token \
uv run uvicorn memhub.app:app --host 127.0.0.1 --port 8900
```

The store is never created by the server: run `MEMORY_DIR=$MEMHUB_DATA/rules
uv run python memhub/rules.py init` once, as provisioning does. Until then every
`/run` answers the tool's exit code 3 and its message, and `/health` says
`"ok": false`.

## Endpoints

| Endpoint | Token | Does |
|---|---|---|
| `GET /health` | no | `ok` (the store is initialised), `rule_count`, `scope_rows` |
| `POST /run` | yes | `{tool: "memo", argv, scope?}`: one call to the rules store; answers `exit_code`, `stdout`, `stderr`, `scopes` |
| `GET /export` | yes | the store directory as a gzipped tar, history included, `.lock` left out |
| `GET /verify` | yes | scope rows that match no rule, and rules with no row |

`/run` serves `wake`, `recall`, `note`, `edit`, `drop` and `merge`; anything
else is a 400, `init` included. A refusal by the tool itself (a bad id, a text
over 280 bytes, a missing store) is a 200 whose body carries the tool's exit
code and stderr.

## Layout

| File | Holds |
|---|---|
| `memhub/settings.py` | every path, read from the environment, no filesystem access |
| `memhub/rules.py` | the rules store itself; also the `memo` CLI of a machine's local store |
| `memhub/runner.py` | `/run`: the verb allowlist, the write lock, the in-process call |
| `memhub/scope.py` | `rules/META.jsonl`: who wrote a rule, and where it is true |
| `memhub/app.py` | the endpoints and the bearer token |

## Scope metadata

`rules/META.jsonl` records **machine, project, actor** and an
**applicability** for every rule — out of band, never in the rule's text. It
sits beside `RULES.txt` but is not tracked by the store's git repository.

- Written on `/run` when the client sends `scope` with a `note` or `merge`,
  inside the write's own lock, for the id the tool printed (`Saved as #<id>.`).
  `edit` and `drop` write no row.
- Read back on `/run` as `scopes`, for the footer the client appends. The tool's
  own stdout is never rewritten.
- `GET /verify` reports rows that match no rule, and rules with no row. A
  dropped rule's row, and the row of a rule whose text was edited, are reported
  there.
- Every row carries a fingerprint of the rule's date and text, and the store's
  epoch. A mismatch on either **suppresses the annotation** rather than
  printing a stale one.

The commit of a write is authored by the writing machine's role and host, from
the request's scope; without scope, by `memhub`.

## Environment

| Variable | Default |
|---|---|
| `MEMHUB_DATA` | `/var/lib/memhub`; the store is `$MEMHUB_DATA/rules` |
| `MEMHUB_TOKEN_FILE` | `/etc/memhub/token` |
| `MEMHUB_HOST_PROJECTS` | empty — colon-separated projects whose rules are machine-bound |

## Vendored tools

`vendor/memo.py` is OptMem's tool, kept only for the migration tooling under
`tools/` and its tests; the server no longer runs it. It is a copy, never
edited here.
