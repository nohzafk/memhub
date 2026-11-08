# Agent Instructions — memhub

One shared agent memory for several machines: a store of standing rules
(`memo`), one text file in a git repository, served over HTTP from a container,
with a thin routed client. The contract is `docs/rules-store.md`.

## Quick Start

```sh
cd server
uv run pytest -q                                    # the whole suite
uv run ruff check . ../tools ../client
uv run ruff format --check . ../tools ../client
```

Those three are gate G1, and `deploy/deploy.sh` runs them itself before it
pushes anything. Deploy and verify:

```sh
deploy/deploy.sh          # push, sync the venv, install units, health-check
deploy/smoke.sh           # gate G2, end to end over the network
```

`smoke.sh` **writes to the real store permanently** — it notes, edits and drops a
marker rule on every run, which uses up an id and leaves commits in the history.

## Project Structure

```text
server/          the FastAPI service. A uv project; its lockfile covers tools/ too
  memhub/        app.py (endpoints), runner.py (the passthrough), rules.py
                 (the store, and the local `memo` CLI), scope.py, settings.py
  vendor/        OptMem's memo.py, for the migration tools' tests only.
                 A COPY. See "Vendored tools" below
  tests/         drive the real rules store and git, never mock them
client/          what a machine installs: memo-client.py,
                 memhub-mirror.sh. Stdlib only
docs/            rules-store.md: the store's contract and its decisions
tools/           one-off operator scripts, PEP 723 standalone. Tested from server/
deploy/          provision-ct.sh, deploy.sh, smoke.sh, systemd units, pins.env
```

## Key Concepts

**The transport is argv, not REST.** `POST /run {tool, argv}` hands argv to the
rules store's own parser (`rules.run`), in process, so the text check, the id
assignment, the lock and the commit all come from the tool itself and cannot
drift from it. Nothing goes through a shell: argv is a list, so a quote or a
semicolon inside a rule is text and never syntax. Verbs are allowlisted in
`runner.py`.

**Routing lives in the client, and it is a confidentiality boundary.** `-g` forces
shared, `-l` forces local, and otherwise a cwd under `MEMHUB_LOCAL_ROOTS` routes
local. The server never learns the cwd, so by the time a request arrives the text
has either left the machine or it has not — nothing downstream can undo that. This
is why the clients refuse to run when `MEMHUB_LOCAL_ROOTS` is unset: an absent
boundary must not read as "nothing here is confidential".

**The deny list is the second line, and it reads the text.** Routing protects a
directory, not a topic: work done from a cwd outside `MEMHUB_LOCAL_ROOTS` routes
shared even when the text is plainly work, and that has happened. So before the
text of a `note`, `edit` or `merge`, or the reason of a `drop`, leaves for the
shared store, the client matches it
against the regexes in `MEMHUB_DENY_SHARED_FILE`
(default `~/.config/memhub/deny-shared`) and refuses on a hit, naming the flag
that would have been right. No file means no gate — a machine with nothing to
keep local says so by not having one. `MEMHUB_ALLOW_SHARED=1` is the deliberate
override, for the memory that has to name the thing to record the boundary
itself. The terms live in a file outside the repo because this repo is public.

**Writes are serialised at both layers.** `rules.py` flocks `<store>/.lock`
around each read-modify-write and its commit. `runner.py`'s `WRITE_VERBS` lock
still matters on its own: it brackets the scope recorder's row around the write,
so a row is credited to the write that made it, and `/export` takes it so a copy
is never torn.

**The store is one text file and git.** `RULES.txt` holds the rules in force,
one per line (`#<id> <YYYY-MM-DD> <text>`); every write is one commit, so what
was edited or dropped is in the history, not the file. Ids are monotonic and
never reused: `NEXT` holds one more than the largest id ever held, so dropping
the last rule does not free its number. Moving a machine's local rules to the
shared store (`tools/promote_local.py`) is an append that hands out new ids,
never a merge of identities.

**One writer, on the hub.** Two stores that both accepted shared writes would
hand out the same ids to different rules, which is why the mirror is read-only.
A shared write that cannot reach the hub waits in the client's outbox
(`~/.agents/memory/outbox`) and is sent, in order, by the next shared call that
reaches it; only text that routing and the deny list already let through enters
it.

## Vendored tools

`server/vendor/memo.py` is OptMem's tool, a byte-identical copy from the Nix
config repo. The server no longer runs it; only the migration tools' tests
(`tests/test_cutover.py`) do, and the release leaves it out. **Never edit it
here.** `vendor/` means a copy owned somewhere else.

## Conventions

- Python via uv. `server/` is the uv project; `tools/` scripts are standalone with
  inline script metadata but are tested from `server/`, on one lockfile.
- `client/*.py` stay **stdlib-only**. Each is vendored alone into a Nix wrapper
  or copied into place, with no dependency closure.
- Shell functions use snake_case.
- Every address and host is env-overridable and the committed defaults are
  examples: `MEMHUB_URL`, `MEMHUB_TOKEN_FILE`, `PVE_HOST`, `VMID`. Set them for a
  real deployment rather than editing the defaults.

## Do not

- Do not edit anything under `server/vendor/`.
- Do not touch the live store on any machine, and do not write agent memory from a
  subagent (see the global memory rules).
- Do not turn a known gap into a warning in `smoke.sh`. A warning is right only
  while a gap is expected; left in place it lets a broken deploy pass gate G2.

## Additional Documentation

- `README.md` — what this is and how it fits together, for a human
- `docs/rules-store.md` — the store's contract: format, verbs, ids, outbox
- `server/README.md` — endpoints, settings, scope metadata
- `client/README.md` — the routing rules and every environment variable
- `deploy/README.md` — provisioning, pins, token rotation, unit hardening
