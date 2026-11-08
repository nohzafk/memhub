# memhub

One memory, shared by every machine and every coding agent you use.

Coding agents forget everything between sessions. The usual fix is a file in the
repository, which works until you have three machines and three different agents,
and then you have three memories that disagree. memhub is the other approach: the
memory lives on one small always-on box, and every machine reaches the same copy.

One store, served as `memo`: one standing rule per line — the clue, not the
account of it. The whole store is what an agent reads at the start of a session,
so it is curated, not compressed: a rule is edited when it was wrong, merged when
it repeats another, and dropped when it stops being true.

It holds what no single project owns: facts about machines, tools and services,
and anything an agent working in another tool needs to know unasked. The running
narrative of a project's work lives with that project's own session history, not
here.

```console
$ memo note "dcg: an auto-discovered project .dcg.toml grants no allow (v0.6.9+)."
Saved as #216.

$ memo recall 'dcg.*allow'
#216 2026-08-21 dcg: an auto-discovered project .dcg.toml grants no allow (v0.6.9+).
```

The store is a plain text file in a git repository: `#<id> <date> <text>`, one
rule per line, and every write is one commit, so nothing edited or dropped is
lost. memhub adds the sharing.

## How it works

```text
  Mac, laptop, box              the always-on host
  ┌────────────────┐            ┌──────────────────────────┐
  │ memo           │  argv over │  FastAPI                 │
  │  (thin client) │───HTTP────▶│   └─ runs rules.py on    │
  │                │            │      RULES.txt + git     │
  │  routes first  │            │                          │
  └────────────────┘            └──────────────────────────┘
```

The client is a **router**, not an implementation. Every invocation resolves to the
shared store or to a store that never leaves the machine, by three rules in order:

1. `-g` forces shared, `-l` forces local. The flag comes first.
2. Otherwise, a working directory under `MEMHUB_LOCAL_ROOTS` stays local.
3. Otherwise, shared.

**Rule 2 can only live in the client, and that is the whole design.** The server
never learns which directory a command was typed in, so by the time a request
arrives the text has either left the machine or it has not — and nothing further
along can undo it. Work under a confidential path is therefore never sent anywhere,
rather than being sent and then filtered.

The server does not reimplement the tool. `POST /run {tool, argv}` hands argv to
the store's own parser, so the text check, the ids, the locking and the commit
come from the tool itself — the same `rules.py` that is the `memo` of a
machine's local store. Four endpoints and no more: `/health`, `/run`, `/verify`,
and `/export` for the mirror.

The store used to be OptMem, a diary whose old entries compress into summaries.
Standing rules want the opposite at every point — they do not age, they get
corrected — so it became a file of rules in git; [the design](docs/rules-store.md)
has the reasons and the measurements.

## Why not simpler

- **Why not a synced folder?** Ids are handed out by the store and never reused,
  so two stores that both accept writes would give one id to two different rules.
  One writer, many readers, is not a limitation here — it is the only correct
  topology. A write made while the hub is unreachable waits in an outbox on the
  machine and is sent by the next call that reaches it.
- **Why not one store and no local option?** Some work should not leave the machine
  it was done on. A single shared store forces a choice between recording nothing
  and recording it somewhere it does not belong.
- **Why not a REST API per verb?** Then the server owns behaviour the tool already
  has, and the two drift. Passing argv means there is only one implementation.

## Layout

```text
server/    the service. A uv project; memhub/rules.py is the store itself
client/    what a machine installs — the router plus the mirror puller
tools/     operator scripts: dump, merge, promote a local store upstream
docs/      the rules store's design
deploy/    provisioning, deploy, smoke test, systemd units, pinned versions
```

## Getting it running

You need a host that is always on and reachable from your machines — a NUC, a small
VM, a Raspberry Pi. `deploy/` targets an LXC container on Proxmox; the service is a
plain FastAPI app and does not require that.

```sh
deploy/provision-ct.sh    # create the container and the store. Once, ever.
deploy/deploy.sh          # push code, install units, health-check
deploy/smoke.sh           # verify end to end (this writes to the store)
```

Every address, host and path is set by environment variable — `MEMHUB_URL`,
`PVE_HOST`, `VMID`, `MEMHUB_TOKEN_FILE`. The committed defaults are examples from
one deployment, not configuration you are meant to inherit.

Access is a single bearer token, read from a file at both ends. That is deliberately
modest: this is designed for a private network, not the internet.

Then, on each machine, install the client from `client/` and set
`MEMHUB_MACHINE` and `MEMHUB_LOCAL_ROOTS`. Both are required — the client refuses to
start without them, because an unset boundary must never be read as "nothing here is
confidential". `client/README.md` has the full list.

## Status

The rules store is built and not yet deployed; the hub in daily use across three
machines and three agents still serves OptMem until the migration the design
describes. Search (`memo recall`) is a regex over the rules.

memhub used to serve a second store, a per-day log (`daylog`). It is no longer
served. The day files stay in the server's data directory, untouched, and
`/export` no longer includes them.

## Documentation

- `docs/rules-store.md` — the store's contract and why it replaced OptMem
- `server/README.md` — endpoints, settings, scope metadata
- `client/README.md` — routing, every environment variable, failure messages
- `deploy/README.md` — pins, token rotation, why the units are hardened as they are
- `AGENTS.md` — conventions and constraints, for a coding agent

## Credits

`memo` started as [OptMem](https://github.com/VictorTaelin/OptMem) by Victor
Taelin: one fact per line, read whole at the start of a session, kept to a fixed
cost. That idea is still the core of it. memhub is the part that lets several
machines share it.
