# The rules store: a text file replaces OptMem

State: built on branch rules-store, not deployed. This document is the contract the server, the client,
the Nix module that installs `memo`, and the bendcord `memory` plugin build to.

## Why

OptMem is a diary. Its three bets — append-only positional ids, old entries
compressed into period summaries by a model, a wake of fixed length that shows
the newest verbatim and the oldest coarsest — are right for an agent whose only
context management is compression, and they are what the store had to be when
memo started. Since then the sessions themselves keep their own history (folds,
searchable transcripts), and the rules for what goes into memo narrowed to
standing facts: what no file owns, what another tool would hit unasked, what
still holds in a year, what cost something. A store of standing facts wants the
opposite of a diary at every point:

- A rule does not age. The August fact about which launchd domain sees the
  Keychain is as true as yesterday's; a diary coarsens it first.
- A rule is corrected, merged, retired. In the live store 6 of the 77 uncompressed
  memories are corrections of earlier ones, and the wrong one stays in the wake
  until compression happens to swallow it. There is no delete.
- Summarising rules is editing, not narration. The model that compresses a block
  turns "pgrep does not list launchd-started GUI apps" and "push with --no-verify"
  into one sentence and the grep keyword is gone.
- Recall over the raw log returns the newest matches and drops the oldest, which
  for rules is the wrong end.

The result, measured on the live store: 541 memories written, 74 wake lines of
about 20 KB pushed into every session, a third of them period summaries of work
that no present rule would admit, and they cannot be removed.

What stays is everything around the store: one writer on the always-on host,
the argv transport, the client's routing as the confidentiality boundary, the
deny list, the read-only mirror, the scope metadata, the four gates and the
bendcord plugin that scores and reminds.

## Decisions

### D1 — One text file, one rule per line, in a git repository

The shared store is `<MEMHUB_DATA>/rules/RULES.txt`, and `<MEMHUB_DATA>/rules`
is a git repository. A line is

```text
#<id> <YYYY-MM-DD> <text>
```

`id` is a positive integer; `text` is one line of at most 280 UTF-8 bytes, which
carries its own `#tag` words as today. Blank lines and lines starting with `//`
are not rules and are not written by the tool. The file holds only the rules in
force; what was dropped or edited is in the repository's history.

Every write (D2) is one commit. The commit message is the verb and the ids it
touched (`note #542`, `drop #534: superseded by #535`, `merge #507,#537 -> #543`);
the author is the writing machine's role and host when the request carried scope,
else `memhub`. `git` is run by the server under the write lock, never by a shell.

The local store of a machine that keeps one (D7) has the same layout under
`MEMORY_DIR`.

Rejected: SQLite (history and diff would have to be built; git has them), a
directory of one-file-per-rule (one file is what `wake` prints and what a reader
greps).

### D2 — The verbs

`memo` keeps its name and its three everyday verbs, so hooks, instructions and
the plugin keep their contract, and gains the three that a store of rules needs:

| verb | effect | writes |
|---|---|---|
| `wake` | print the whole file, grouped (D5), with the footer (D4) | no |
| `recall <regex>` | print every rule whose text matches, case-insensitive, in id order; no truncation | no |
| `note "<text>"` | append a rule with the next id and today's date; print `Saved as #<id>.` | yes |
| `edit <id> "<text>"` | replace the text of a rule in place; date unchanged | yes |
| `drop <id> ["<why>"]` | remove a rule; the reason goes in the commit message only | yes |
| `merge <id>,<id>[,…] "<text>"` | remove the listed rules and append one with a new id, in one commit | yes |

Gone: `nap`, `zoom`, `forget`, `config`, `import`. `init` stays absent from the
API as before; the repository is created at provisioning.

`note`, `edit` and `merge` apply the same text check: non-empty, one line, at
most 280 bytes. The server's `/run` allowlist is exactly the table above; the
write verbs take the write lock; `note` and `merge` create a scope row for the
id they create, and `edit` re-records the row of the id it kept.

### D3 — Ids are monotonic and never reused

The next id is one more than the largest id the file has ever held, kept in
`<repo>/NEXT` and committed with the write, so a dropped last rule does not free
its number. Ids are single numbers: `#542`, never a range. A rule that corrects
another names it in its text (`corrects #534`) and the author drops or edits the
old one in the same session; the store does not link them.

### D4 — No compression; a size footer and a curation reminder

`wake` ends with one line: `<N> rules, <M> bytes.` When the file exceeds 120
rules or 32 KB the footer gains a second line:
`curate: the wake is over budget; merge or drop with memo merge / memo drop.`
Nothing is refused. At the present rate (about five rules a month after the
gates) the threshold is more than a year away.

Rejected: a hard cap that refuses `note` (it would refuse the one rule that
mattered on the day the file was full); silent growth (the fixed cost of the
wake is the one thing worth keeping from OptMem).

### D5 — The wake is grouped by tag

`wake` prints the rules grouped by their first `#tag` word, groups in order of
their earliest id, rules within a group in id order, then the rules with no tag.
A group is introduced by a line `## <tag>`. The whole file is printed: the wake
stays a push, not an index to pull from. Grouping is a view; the file itself is
in id order.

### D6 — Migration of the OptMem store

1. `tools/dump_log.py` turns the live OptMem log into `YYYY-MM-DD <text>` lines,
   all 541, from an `/export` of the shared store.
2. One model pass over the whole dump, with the four gates and the "never" list,
   marks each line `keep`, `drop`, or `merge-into <line>`; it sees neighbours, so
   a correction retires what it corrects and a restated fact merges with its
   first statement. It writes the candidate `RULES.txt` (original dates kept,
   ids assigned in dump order) and a report of what was dropped and why, in
   groups.
3. The operator reads the keep list and the drop report before the file is
   committed as the repository's first commit. The expected outcome is 60–80
   rules.
4. The OptMem directory stays on the server, unserved and still in the backup,
   as `daily/` already does; `/export` no longer includes it. The scope rows of
   the migrated rules are re-pointed from OptMem positions to the new ids.

### D7 — Local stores, routing and the deny list are unchanged

A machine with `MEMHUB_LOCAL_ROOTS` keeps a local store in the same format
(D1), and `-g`, `-l` and the cwd route exactly as before; the server still never
learns the cwd. The local store is created with `memo -l init`. The deny list still reads every `note`, `edit` and `merge` text
bound for the shared store, and now a `drop`'s reason too. `memhub-promote` appends local rules to the shared
store with new ids (position was never identity here, so this is an append, not
a merge of identities) and archives the local store as before.

### D8 — A write that cannot reach the hub waits in an outbox

When routing has decided a write is shared and the hub is unreachable, the
client appends the argv to `~/.agents/memory/outbox` (one JSON line per call)
and says so: `memhub unreachable at <url>; queued as outbox entry <n>, sent on
the next shared call.` The next shared call of any verb that reaches the hub first
replays the outbox in order, each as its own request, then runs itself; an entry
that the server refuses (a deny-list hit cannot happen here, since the client
checked before queueing; a text check can) is dropped with its error printed.
Only text that routing already judged shared enters the outbox, so the
confidentiality boundary is where it was. No daemon; a machine that makes no
shared call sends nothing.

### D9 — What the bendcord plugin and the Nix module change

- The plugin's wake parser reads `#<id>` lines and `## <tag>` group lines; the
  code that recognised a compression prompt, a pending `nap`, or a `#lo-hi`
  range goes. The hint's footer says `<N> rules`. The tool description no longer
  tells the model to run `memo nap`.
- The Nix module installs the new tool in place of the vendored `memo.py`,
  points `MEMHUB_MEMO_PY` at it, and creates the local store as a repository at
  activation. `instructions.md` and the skill lose the OptMem vocabulary
  (`nap`, `zoom`, summaries, "compressed to fit") and gain `edit`, `drop`,
  `merge`, and the outbox.
- The Claude Code session hook and the plugin stop following `wake`
  continuation pages: the wake is one page.

## Decisions taken while building

- A rewrite keeps the `//` and blank lines where they are; the tool writes none.
- A malformed line in `RULES.txt` fails every read and write with exit 3,
  naming the line, rather than being skipped.
- `init` writes `.lock`, `META.jsonl` and `EPOCH` to the repository's
  `.git/info/exclude`: the scope metadata lives beside the rules and travels with
  `/export`, but it is not the store's history.
- `edit` re-records its scope row: the same id, the fingerprint of the stored new
  text, the scope of the editing request; the last row of a ref wins. An `edit`
  without scope writes none, and `/verify` reports the old row.
- Outbox entry numbers are positions in the file, so they start again at 1 once
  the outbox has been emptied.
- During a replay, the line for each entry goes to stderr; the queued message of
  a write goes to stdout, where the write's own answer would have been.
- An HTTP error during a replay (a 401, a 409) stops it and keeps that entry and
  the rest; the call itself proceeds.
- Delivery is at least once: an entry whose request reached the hub but whose
  answer was lost is sent again, and the copy is dropped by hand.
- A `drop` reason passes the deny list, because it lands in the hub's git
  history.
- The container needs `git`; `deploy.sh` installs it where provisioning did not.

## What this does not do

- It does not decide what a rule is: the four gates and the fold screen are as
  they were.
- It does not search better than a regex; that was true before.
- It does not merge two writable stores. One writer, many readers, as before.
