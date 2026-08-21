# client

The routed `memo` and `daylog`: the two commands a machine actually installs.

Two files, stdlib only, no imports of each other. That is a vendoring
requirement, not a style choice: the Nix config repo wraps each file on its own in Nix
and the second config repo copies each into place, so neither may depend on a sibling.
The routing core is therefore duplicated, and
`server/tests/test_client.py` runs the whole scope matrix against **both** files
so the copies cannot drift.

## Routing

1. `-g`/`--shared` or `-l`/`--local`, **first argument only** — the position the
   tools themselves print (`memo -g wake 2 296`).
2. Otherwise a cwd under any entry of `MEMHUB_LOCAL_ROOTS` → local.
3. Otherwise → shared.

Rule 2 is the confidentiality boundary. The server never learns the cwd, so
this decision cannot be made anywhere else, and it cannot be revisited after the
fact. Hence the deliberate asymmetry: if the cwd cannot be determined at all and
local roots are configured, the client routes **local** — a shared memory in the
local store is recoverable, work text on the server is not.

## Environment

| Variable | Default | Notes |
|---|---|---|
| `MEMHUB_URL` | `http://127.0.0.1:8900` | override it; the default is a placeholder |
| `MEMHUB_TOKEN_FILE` | `/run/secrets/memhub-token` | sops-nix on the workstations; a hand-installed `0600` file on the Linux box |
| `MEMHUB_LOCAL_ROOTS` | **required** | colon-separated paths that stay on this machine, or the literal `none`. Empty entries within the list are ignored |
| `MEMHUB_MACHINE` | **required** | this machine's **role**: `work`, `personal`, `omarchy`, `nuc`, `cloud`, `phone` |
| `MEMHUB_ACTOR` | unset | which agent is writing: `claude-code`, `codex`, … |
| `MEMHUB_MEMO_PY` | `memo.py` beside the client | the vendored tool for local scope |
| `MEMHUB_DAYLOG_PY` | `daylog.py` beside the client | same |

Per machine:

| Machine | `MEMHUB_LOCAL_ROOTS` | Effect |
|---|---|---|
| work machine | `$HOME/work` | work stays local, everything else shared |
| personal machine | `none` | everything shared |
| Linux box | `none` | everything shared |

**The client refuses to run if either is missing**, naming what to set. It fails
at `wake`, the first command of a session; only `--help` works unconfigured.
That is stricter than the treatment of a missing label, on purpose: an
unlabelled memory is recoverable, but an unset `MEMHUB_LOCAL_ROOTS` read as
"nothing is local" would send confidential work to the shared store, and that
cannot be undone. Absence is therefore not a valid answer — `none` is how a
machine says it out loud.

## Scope

Every shared write carries where it came from: `machine` from
`MEMHUB_MACHINE`, `host` from the hostname, `project` from the nearest ancestor
directory holding a `.git`, `actor` from `MEMHUB_ACTOR`.

The client **reports and does not decide**. Applicability is settled by the
server from its own `MEMHUB_HOST_PROJECTS` list, because whether a project is
machine-bound is a property of the project — the same answer on every machine —
and three client copies of one list are three chances to disagree:

| Situation | `applies` |
|---|---|
| project is in the server's `MEMHUB_HOST_PROJECTS` | `host` |
| any other project | `portable` |
| no project detected | `unknown` |
| `note --here "…"` / `note --anywhere "…"` | `host` / `portable`, marked `declared`, and it wins |

The flag is taken only immediately after the verb, so a memory whose own text
mentions `--here` keeps it.

A write is **never refused** for want of a label: no `MEMHUB_MACHINE` means no
metadata, the memory still lands, and `GET /verify` reports it. Local scope
(`-l`) sends nothing at all — the local store is single-machine by construction.

After `wake` and `recall` the client appends a footer naming only the memories
written elsewhere. A `portable` memory is true everywhere, so it is never
foreign however far away it was written:

```
-- scope: 2 of the memories above were written elsewhere
   #12 [omarchy / the second config repo / host]
   #18 [omarchy / no project / unknown]
```

## The mirror

`memhub-mirror.sh` pulls the whole store from `GET /export` on an hourly timer
and swaps it into `~/.agents/memory/mirror`, read-only. It is a byte copy, so it
is promotable — move it into a server's data dir and it *is* the store, ids and
`TREE/` and `META.jsonl` intact.

```sh
memhub-mirror              # refresh
memhub-mirror --verify     # check it and say what it holds
MEMORY_DIR=~/.agents/memory/mirror/optmem memo -l wake    # read it offline
```

**Never write to the mirror.** Two OptMem stores that both take writes are two
identities that can never be merged. A download that fails verification is
discarded rather than swapped in, so a truncated fetch cannot destroy a good copy.

## Writing back

If the server is unreachable and a fact must not wait, `memo -l note "..."` puts it
in the local store. Later:

```sh
uv run tools/promote_local.py --dry-run   # see what would be sent
uv run tools/promote_local.py             # import it, then archive the local store
```

It appends rather than merges — the local tail is newer than everything on the
server, which is exactly what `memo import` accepts — and the server assigns the
canonical ids.

## Behaviour worth knowing

- **Local scope execs the tool**, replacing the process, so exit codes, stdout,
  stderr and paging behave exactly as they do today.
- **Unreachable server**: `memhub unreachable at <url>; shared memory unavailable`
  on stderr, exit 1. A write adds that it was *not* recorded and how to record it
  locally. The connect probe is bounded at 2s so a SessionStart hook on a machine
  whose server is off costs two seconds, not thirty.
- **`daylog path`** in shared scope prints a path on the server and exits 0, which
  the calling machine cannot open. The client cannot fix it either: the tool sees
  only `DAYLOG_DIR` and answers for the store it was given. Use `daylog -g read
  DATE`, which returns content and so crosses the boundary a path cannot.
