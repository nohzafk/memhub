# memhub

One memory, shared by every machine and every coding agent you use.

Coding agents forget everything between sessions. The usual fix is a file in the
repository, which works until you have three machines and three different agents,
and then you have three memories that disagree. memhub is the other approach: the
memory lives on one small always-on box, and every machine reaches the same copy.

Two stores, because remembering *that* something was decided and remembering *why*
are different jobs:

| Store | Command | Holds | Ages by |
|---|---|---|---|
| `optmem` | `memo` | one atomic fact per line — the clue, not the account of it | compression: old memories become summaries |
| `daily` | `daylog` | the running narrative of one working day | nothing; every word is kept forever |

A decision goes in `memo`. The reasoning, the dead ends and the state you left the
work in go in `daylog`. That split is the point: a `memo` note stays short enough
to survive compression, and carries enough of a handle to find the long version.

```console
$ memo note "dcg: an auto-discovered project .dcg.toml grants no allow (v0.6.9+)."
Saved as #216.

$ memo recall 'dcg.*allow'
#216 2026-08-21 dcg: an auto-discovered project .dcg.toml grants no allow (v0.6.9+).

$ daylog grep 'config regression'
2026-08-21.md:44: why that read as a config regression, the 6-of-13 false alarm...
```

`memo` is [OptMem](https://github.com/VictorTaelin/OptMem) by Victor Taelin — a
binary tree of memories where the old ones compress into summaries, so recall stays
a fixed cost as the store grows. memhub adds the sharing.

## How it works

```text
  Mac, laptop, box              the always-on host
  ┌────────────────┐            ┌──────────────────────────┐
  │ memo / daylog  │  argv over │  FastAPI                 │
  │  (thin client) │───HTTP────▶│   └─ runs memo.py or     │
  │                │            │      daylog.py on the    │
  │  routes first  │            │      real store          │
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

The server does not reimplement either tool. `POST /run {tool, argv}` executes the
real `memo.py` or `daylog.py` against the store, so pagination, compression prompts,
locking and crash repair come from the tool itself. Five endpoints and no more:
`/health`, `/run` and `/verify`, plus `/export` and `/import` for the mirror and
the way back from it.

## Why not simpler

- **Why not a synced folder?** OptMem numbers memories by position, so two stores
  that both accept writes are two identities that can never be merged. One writer,
  many readers, is not a limitation here — it is the only correct topology.
- **Why not one store and no local option?** Some work should not leave the machine
  it was done on. A single shared store forces a choice between recording nothing
  and recording it somewhere it does not belong.
- **Why not a REST API per verb?** Then the server owns behaviour the tool already
  has, and the two drift. Passing argv means there is only one implementation.

## Layout

```text
server/    the service. A uv project; vendor/ holds the two real tools
client/    what a machine installs — the two routers plus the mirror puller
tools/     operator scripts: dump, merge, promote a local store upstream
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

Then, on each machine, install the two clients from `client/` and set
`MEMHUB_MACHINE` and `MEMHUB_LOCAL_ROOTS`. Both are required — the clients refuse to
start without them, because an unset boundary must never be read as "nothing here is
confidential". `client/README.md` has the full list.

## Status

Built and in daily use across three machines and three agents. Search
(`memo recall`, `daylog grep`) is plain text matching.

## Documentation

- `server/README.md` — endpoints, settings, the vendored-tool checksums
- `client/README.md` — routing, every environment variable, failure messages
- `deploy/README.md` — pins, token rotation, why the units are hardened as they are
- `AGENTS.md` — conventions and constraints, for a coding agent

## Credits

[OptMem](https://github.com/VictorTaelin/OptMem) by Victor Taelin is the durable
half and the better idea. The daily log follows the same shape as the memory tool in
OpenMinis. memhub is the part that lets several machines share both.
