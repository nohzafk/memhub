# client

The routed `memo`: the command a machine actually installs.

One file, stdlib only. That is a vendoring requirement, not a style choice: the
Nix config repo wraps it on its own in Nix and the second config repo copies it
into place, so it may not depend on a sibling. `server/tests/test_client.py`
runs the whole scope matrix against it.

## Routing

1. `-g`/`--shared` or `-l`/`--local`, **first argument only** — the position the
   tools themselves print (`memo -g drop 12`).
2. Otherwise a cwd under any entry of `MEMHUB_LOCAL_ROOTS` → local.
3. Otherwise → shared.

Rule 2 is the confidentiality boundary. The server never learns the cwd, so
this decision cannot be made anywhere else, and it cannot be revisited after the
fact. Hence the deliberate asymmetry: if the cwd cannot be determined at all and
local roots are configured, the client routes **local** — a shared memory in the
local store is recoverable, work text on the server is not.

## Verbs

| verb | effect | writes |
|---|---|---|
| `wake` | print every rule, grouped by tag, with a size footer | no |
| `recall <regex>` | print every rule whose text matches, case-insensitive | no |
| `note [--here\|--anywhere] "<text>"` | add a rule; prints `Saved as #<id>.` | yes |
| `edit <id> "<text>"` | replace a rule's text in place | yes |
| `drop <id> ["<why>"]` | retire a rule; the reason goes in the commit message | yes |
| `merge <id>,<id>[,…] "<text>"` | replace the listed rules with one new rule | yes |

Any other verb is a usage error (exit 2) and goes nowhere. The tool behind both
scopes is the rules store (`rules.py`); its contract is
[the design](../docs/rules-store.md).

## The deny list

Rule 2 protects a directory, not a topic. Work done from a cwd outside
`MEMHUB_LOCAL_ROOTS` routes shared even when the text is plainly work — that is
not hypothetical, it happened, and a `drop` on the server still leaves the text
in the store's history.

So the text of a `note`, `edit` or `merge`, and the reason of a `drop`
(it lands in the server's git history), bound for the shared store is matched
against a list of regexes first, one per line, `#` starting a comment. Only the
text is read, not an id or an id list:

```
# ~/.config/memhub/deny-shared — never let these reach the shared store
metastore
APP-[0-9]{3,}
talpa
```

A hit refuses the write (exit 2), names the term, and prints the command that
would have been right (`memo -l note …`; for an `edit` or `merge`, also
`memo -g drop <id>` to retire the shared rule; for a `drop`, the same drop
without a reason). Matching is case-insensitive. An unreadable
pattern is a hard error rather than a skipped line: a gate that silently stops
gating is worse than none.

**No file means no gate.** A machine with nothing to keep local says so by not
having one, which is why this does not need a per-machine default. And because
the terms are the confidential part, the file lives outside this repo — this one
is public.

`MEMHUB_ALLOW_SHARED=1` overrides it for one command. The boundary record itself
needs it: to write down which words are work-only, a memory has to name them.

## Environment

| Variable | Default | Notes |
|---|---|---|
| `MEMHUB_URL` | `http://127.0.0.1:8900` | override it; the default is a placeholder |
| `MEMHUB_DENY_SHARED_FILE` | `~/.config/memhub/deny-shared` | regexes that must never reach the shared store; absent = no gate |
| `MEMHUB_ALLOW_SHARED` | unset | `1` waives the deny list for one command |
| `MEMHUB_TOKEN_FILE` | `/run/secrets/memhub-token` | sops-nix on the workstations; a hand-installed `0600` file on the Linux box |
| `MEMHUB_LOCAL_ROOTS` | **required** | colon-separated paths that stay on this machine, or the literal `none`. Empty entries within the list are ignored |
| `MEMHUB_MACHINE` | **required** | this machine's **role**: `work`, `personal`, `omarchy`, `nuc`, `cloud`, `phone` |
| `MEMHUB_ACTOR` | unset | which agent is writing: `claude-code`, `codex`, … |
| `MEMHUB_MEMO_PY` | `rules.py` beside the client | the rules store tool for local scope |
| `MEMORY_DIR` | `~/.agents/memory/rules` | the local store, for local scope only |
| `MEMHUB_OUTBOX` | `~/.agents/memory/outbox` | shared writes waiting for the server |

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
and swaps it into `~/.agents/memory/mirror`, read-only. It is a byte copy of
`rules/` — `RULES.txt`, `NEXT`, the git history, `META.jsonl` and `EPOCH` — so
it is promotable: copy `<mirror>/rules` into a new server's data dir, give it
to the service user, start the service, and it *is* the store, every id, the
next id and every scope row intact.

```sh
memhub-mirror              # refresh
memhub-mirror --verify     # check it and say what it holds
MEMORY_DIR=~/.agents/memory/mirror/rules memo -l wake    # read it offline
```

A copy passes verification when `rules/RULES.txt`, `rules/NEXT` and the
`rules/.git` directory are there, every line of `RULES.txt` is blank, a `//`
comment or a rule (`#<id> <YYYY-MM-DD> <text>`), and `META.jsonl`, if present,
is valid JSONL. A download that fails verification is discarded rather than
swapped in, so a truncated fetch cannot destroy a good copy.

**Never write to the mirror.** The hub is the one writer; a second store that
takes writes would hand out the hub's ids to other rules.

## The outbox

A shared write (`note`, `edit`, `drop`, `merge`) that cannot reach the server
is not lost. It is appended to `~/.agents/memory/outbox` (`MEMHUB_OUTBOX`), one
JSON line per call:

```json
{"ts": "2026-05-01T09:30:00+00:00", "argv": ["note", "..."], "scope": {...}}
```

`argv` and `scope` are exactly what the request would have carried (`scope`
is `null` when there is none). The client prints
`memhub unreachable at <url>; queued as outbox entry <n>, sent on the next shared call.`
on stdout and exits 0. A read (`wake`, `recall`) has nothing to keep and fails
as before.

The next shared call of any verb whose connect probe succeeds replays the outbox
first, in file order, each entry as its own `/run` request with its stored
scope, and prints one line per entry on stderr before its own output:

- an entry the server answers 200 is removed, whatever the tool's exit code:
  `outbox: sent entry <n>`, or `outbox entry <n> refused: <stderr>` when the tool
  rejected it (a text check; it would be rejected again forever);
- a transport failure stops the replay and keeps that entry and the rest. If
  the probe now fails too, the server is gone and the call itself queues (a
  write) or fails (a read); otherwise it proceeds;
- an HTTP error (a 401, a 409) stops the replay the same way, and the call
  proceeds;
- an unreadable line is kept and skipped.

The deny list is not read again at replay: it was read before the entry was
queued. Only text that routing already sent to the shared store, and the deny
list let through, ever enters the outbox, so the confidentiality boundary is
where it was. The file is rewritten in one rename after each replay, and a lock
beside it (`outbox.lock`) keeps two sessions from sending one entry twice.
There is no daemon: a machine that makes no shared call sends nothing.

An entry whose request reached the server but whose answer was lost is sent
again, so it can land twice; drop the copy.

## Writing back

The local store is a separate store with ids of its own. To move its rules to
the shared store:

```sh
uv run tools/promote_local.py --dry-run   # see what would be sent
uv run tools/promote_local.py             # note each one, then archive the local store
```

Each local rule is sent as one `memo note`, in id order, and the server assigns
it a new id: position was never identity across stores, so this is an append.
It stops at the first refusal and archives nothing, naming what was already sent.

## Behaviour worth knowing

- **Local scope execs the tool**, replacing the process, so exit codes, stdout,
  stderr and paging behave exactly as they do today.
- **Unreachable server**: a read prints
  `memhub unreachable at <url>; shared memory unavailable` on stderr and exits 1;
  a write is queued in the outbox and exits 0. The connect probe is bounded at 2s
  so a SessionStart hook on a machine whose server is off costs two seconds, not
  thirty.
- **A server that accepts and then stalls** past 30s fails the call with the
  unreachable message, and a write is *not* queued: it may have landed, and a
  second copy is worse than a message.
