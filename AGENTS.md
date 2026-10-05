# Agent Instructions — memhub

One shared agent memory for several machines: OptMem (`memo`), served over HTTP
from a container, with a thin routed client.

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

`smoke.sh` **writes to the real store permanently** — it appends a marker memory
on every run.

## Project Structure

```text
server/          the FastAPI service. A uv project; its lockfile covers tools/ too
  memhub/        app.py (endpoints), runner.py (the passthrough), store.py,
                 scope.py, settings.py
  vendor/        memo.py. A COPY. See "Vendored tools" below
  tests/         drives the real vendored tools, never mocks them
client/          what a machine installs: memo-client.py,
                 memhub-mirror.sh. Stdlib only
tools/           one-off operator scripts, PEP 723 standalone. Tested from server/
deploy/          provision-ct.sh, deploy.sh, smoke.sh, systemd units, pins.env
```

## Key Concepts

**The transport is argv, not REST.** `POST /run {tool, argv}` executes the
vendored tool against the server-side store, so pagination, nap prompts, locking
and crash repair all come from the tool itself and cannot drift from it. Nothing
goes through a shell: argv is a list, so a quote or a semicolon inside a memory
is text and never syntax. Verbs are allowlisted in `runner.py`.

**Routing lives in the client, and it is a confidentiality boundary.** `-g` forces
shared, `-l` forces local, and otherwise a cwd under `MEMHUB_LOCAL_ROOTS` routes
local. The server never learns the cwd, so by the time a request arrives the text
has either left the machine or it has not — nothing downstream can undo that. This
is why the clients refuse to run when `MEMHUB_LOCAL_ROOTS` is unset: an absent
boundary must not read as "nothing here is confidential".

**The deny list is the second line, and it reads the text.** Routing protects a
directory, not a topic: work done from a cwd outside `MEMHUB_LOCAL_ROOTS` routes
shared even when the text is plainly work, and that has happened. So before a
`note` (or a `nap` line) leaves for the shared store, the client matches it
against the regexes in `MEMHUB_DENY_SHARED_FILE`
(default `~/.config/memhub/deny-shared`) and refuses on a hit, naming the flag
that would have been right. No file means no gate — a machine with nothing to
keep local says so by not having one. `MEMHUB_ALLOW_SHARED=1` is the deliberate
override, for the memory that has to name the thing to record the boundary
itself. The terms live in a file outside the repo because this repo is public.

**Writes are serialised at both layers.** `memo.py` flocks its own store.
`runner.py`'s `WRITE_VERBS` lock still matters on its own: it brackets the
recorder's snapshot/commit around the write, and it holds whatever a vendored copy
does.

**Position is identity.** OptMem numbers memories by position, so two stores that
both accept writes are two identities that can never be merged. That single fact
is why the mirror is read-only and why write-back is a promotion, never a merge.

## Vendored tools

`server/vendor/memo.py` is a byte-identical copy from the Nix config repo
(`modules/memory/vendor/memo.py`). **Never edit it here; re-copy to upgrade.**
`vendor/` means the same thing in both repositories: a copy owned somewhere else.
Two copies that differ is the hardest bug here to see, because `-g` and `-l` then
answer the same question differently.

The `ME = "memo -g"` patch is applied by `deploy.sh` at deploy time, not committed
into the vendored file, so the file stays byte-identical to its source.

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
- `server/README.md` — endpoints, settings, vendored-tool checksums
- `client/README.md` — the routing rules and every environment variable
- `deploy/README.md` — provisioning, pins, token rotation, unit hardening
