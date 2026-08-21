#!/usr/bin/env python3
"""daylog — the day's running narrative, one markdown file per day.

The second half of this machine's memory. `memo` (OptMem) holds atomic, durable
facts and decays the old ones into summaries. daylog holds the dated, readable
story of a working day, and never compresses it.

Modelled on the memory tool in OpenMinis, the iOS agent app the user already
runs this way on the phone (src/ios/Agent/Chat/AIChatViewModel+MemoryTools.swift):
  * one file per day, named YYYY-MM-DD.md
  * entries prepended, newest first, each opened by an HTML comment timestamp
  * the most recent few days injected into the session automatically

The store is ~/.agents/memory/daily, beside ~/.agents/memory/optmem and
~/.agents/skills, so Claude Code, Codex and Antigravity share one log.
Set DAYLOG_DIR to point somewhere else.

Why a command instead of a plain file edit: entries are prepended, and an agent's
Edit tool must read the whole file before it can write. That cost grows with every
day of work. This rewrites the file without the agent reading it — the same reason
papercut is a command.

The files are plain markdown. Edit them by hand whenever you want; nothing here
owns their contents the way memo owns LOG.txt.

Why Python, when this was a bash script through the `note`/`recent`/`path` era:
adding `grep` and `read` moved the balance. Every line of the shell version's new
code was shell tax rather than daylog logic — `$?` read off a `[[ =~ ]]` to tell
an invalid regex from a non-match, `break 2` out of nested loops for the match
cap, per-writer SIGPIPE suppression, `wc`+`head` to count and preview in two
passes. All of it is stdlib here, and the closure gets *smaller*: the shell
version needed coreutils (`date -d`, `mktemp`, `head`, `wc`, `basename`),
findutils (`find`) and a modern bash, while this needs only the python3 that
modules/agents/pkg-daylog.nix already carried for the router. Losing GNU `date -d`
as a dependency also removes a real landmine, since BSD `date` does not accept it.

Stdlib only, and deliberately so: modules/agents/pkg-daylog.nix declares the whole
runtime, and the memhub server runs this same file against the shared store with
no wider closure of its own.
"""

import fcntl
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

STORE = Path(os.environ.get("DAYLOG_DIR") or "~/.agents/memory/daily").expanduser()

# How far back `recent` looks. An older file stays on disk and stays searchable; it
# only stops being injected as "recent".
LOOKBACK_DAYS = int(os.environ.get("DAYLOG_LOOKBACK_DAYS") or 30)

# Injection budget. Every session start pays for this output in tokens, so the caps
# are deliberately tighter than the OpenMinis 200 lines: entries written from a
# terminal agent run longer than entries typed on a phone. Raise DAYLOG_PREVIEW_LINES
# if a day's log reads as truncated too often.
PREVIEW_LINES = int(os.environ.get("DAYLOG_PREVIEW_LINES") or 60)
DEFAULT_RECENT = int(os.environ.get("DAYLOG_RECENT_FILES") or 3)

# Retrieval budgets, deliberately looser than the injection budget above: `recent`
# is paid for by every session start, while `read` and `grep` are asked for.
#
# `read` exists to return a day untruncated, so a cap at PREVIEW_LINES would defeat
# it. 400 sits above the longest day ever written here (224 lines) — it bounds a
# pathological file without ever truncating a real one.
#
# `grep` caps both the number of hits and the width of each, because one entry is
# often a single paragraph on one very long line.
READ_LINES = int(os.environ.get("DAYLOG_READ_LINES") or 400)
GREP_MATCHES = int(os.environ.get("DAYLOG_GREP_MATCHES") or 100)
GREP_WIDTH = int(os.environ.get("DAYLOG_GREP_WIDTH") or 200)

USAGE = """daylog — the day's running narrative, one file per day.

  daylog note "<text>"    prepend a timestamped entry to today's log
  daylog recent [N]       print the N most recent non-empty logs (default 3)
  daylog grep <regex>     search every day ever written, newest first
  daylog read [DATE]      print one day in full (default today)
  daylog path [DATE]      print the log path for DATE (default today)

The store is ~/.agents/memory/daily, or $DAYLOG_DIR if set.

`path` names a file in the store this command was routed to. Routing is the
caller's business, not this script's, so a path from a store held on another
machine is not openable here — use `read` to get that day's text instead."""

# Only YYYY-MM-DD counts as a day of the log, so a hand-added README, an editor
# backup or a typo'd `read` argument can never be mistaken for one. `recent`,
# `grep` and `read` all test through here, because three copies of the shape would
# be three chances to disagree about it.
LOG_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def fail(message):
    print(message, file=sys.stderr)
    return 1


def clock():
    """The local wall clock, which is the only clock this tool has.

    Every date here comes through this one function, so the choice is made once.
    Local and naive on purpose, which is what the DTZ noqa covers: a day of work
    is a local day. Aware UTC timestamps would move every late-night entry into
    the next day's file, and the reader is a person recalling what they did on a
    date, not a system correlating events across zones.
    """
    return datetime.now()  # noqa: DTZ005


def tilde(path):
    """The path as a human wrote it, with $HOME collapsed to ~."""
    home = Path.home()
    try:
        return "~/" + str(Path(path).relative_to(home))
    except ValueError:
        return str(path)


def log_files():
    """Every dated log in the store, newest first.

    Newest first is the order both `recent` and `grep` want: the most relevant
    answer arrives before any cap can truncate it. ISO dates sort correctly as
    strings, so the filename is the sort key.
    """
    if not STORE.is_dir():
        return []
    days = [p for p in STORE.iterdir() if p.is_file() and LOG_DATE.match(p.stem)]
    return sorted(days, key=lambda p: p.stem, reverse=True)


def head_and_count(path, limit):
    """The first `limit` lines, and how many lines follow them.

    One pass, and never the whole file in memory: a day is 38k today and grows
    forever. The shell version paid for `wc -l` and `head` separately.
    """
    head, extra = [], 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            if index < limit:
                head.append(line.rstrip("\n"))
            else:
                extra += 1
    return head, extra


def cmd_note(argv):
    """Prepend one timestamped entry to today's file."""
    content = " ".join(argv)
    if not content.strip():
        return fail("daylog note: refusing to record an empty entry")

    STORE.mkdir(parents=True, exist_ok=True)
    # One clock reading for both, so an entry written as the date rolls over cannot
    # be stamped with one day and filed under the other.
    now = clock()
    target = STORE / f"{now.date().isoformat()}.md"
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")

    # Write the new entry, then stream the old file after it. The old file is never
    # read into memory, and the temporary lands in the store rather than /tmp so
    # the rename is atomic instead of a cross-device copy.
    #
    # The rename only protects readers; the prepend is still read-modify-write, so
    # two concurrent notes would lose one. The flock serializes writers, the same
    # way memo.py's locked() does. The server's write lock (memhub runner.py
    # WRITE_VERBS) covers shared scope only — a note routed local, from a session
    # under MEMHUB_LOCAL_ROOTS, has only this. Readers take no lock, because
    # os.replace guarantees they always see a whole file.
    tmp = None
    with (STORE / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=STORE, delete=False
            ) as handle:
                tmp = handle.name
                handle.write(f"<!-- {stamp} -->\n{content}\n\n")
                if target.is_file():
                    with target.open(encoding="utf-8", errors="replace") as old:
                        for line in old:
                            handle.write(line)
            os.replace(tmp, target)
        except BaseException:
            if tmp:
                Path(tmp).unlink(missing_ok=True)
            raise

    print(f"daylog: recorded to {tilde(target)} ({len(content)} chars)")
    return 0


def cmd_recent(argv):
    """Print the most recent non-empty logs, newest first.

    Prints nothing at all when there is no store yet, so a fresh machine injects
    an empty block rather than an error.
    """
    try:
        want = int(argv[0]) if argv else DEFAULT_RECENT
    except ValueError:
        return fail(f"daylog recent: '{argv[0]}' is not a number of logs")
    today = clock().date()
    cutoff = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    yesterday = (today - timedelta(days=1)).isoformat()

    shown = 0
    for path in log_files():
        if shown >= want:
            break
        if path.stem < cutoff or path.stat().st_size == 0:
            continue

        if path.stem == today.isoformat():
            label = "Today's log"
        elif path.stem == yesterday:
            label = "Yesterday's log"
        else:
            label = "Log"

        head, extra = head_and_count(path, PREVIEW_LINES)
        if shown:
            print()
        print(f"[{label} — {path.name}]")
        print("\n".join(head))
        if extra:
            print(
                f"... ({extra} more lines in {path.name} — read the file for the rest)"
            )
        shown += 1
    return 0


def cmd_grep(argv):
    """Print every matching line, newest day first, as `<date>.md:<line>: text`.

    The date leads each hit because it is the argument `read` takes: a search names
    the day, and reading that day in full is the next command.

    Two deliberate differences from `recent`:
      * No lookback cutoff. That cutoff is recent's injection budget, and "when did
        I decide X" is a question about the whole history.
      * The pattern is a case-insensitive Python regex, which is exactly what
        `memo recall` compiles (memo.py:735). The two halves of one memory are
        searched by one dialect; the shell version's POSIX ERE was a second one.
    """
    if not argv or not argv[0]:
        return fail("daylog grep: no pattern given")
    if not STORE.is_dir():
        return fail(f"daylog grep: no store at {tilde(STORE)}")

    try:
        pattern = re.compile(argv[0], re.IGNORECASE)
    except re.error as error:
        return fail(
            f"daylog grep: '{argv[0]}' is not a valid regular expression ({error})"
        )

    hits = 0
    for path in log_files():
        with path.open(encoding="utf-8", errors="replace") as handle:
            for lineno, line in enumerate(handle, 1):
                line = line.rstrip("\n")
                if not pattern.search(line):
                    continue
                # The cap is tested only against a real match, so the footer means
                # "there is another hit", never "there was another line".
                if hits >= GREP_MATCHES:
                    print(
                        f"... (stopped at {GREP_MATCHES} matches — narrow the"
                        " pattern, or raise DAYLOG_GREP_MATCHES)"
                    )
                    return 0
                if len(line) > GREP_WIDTH:
                    line = line[:GREP_WIDTH] + "..."
                print(f"{path.name}:{lineno}: {line}")
                hits += 1

    if not hits:
        return fail(f"daylog grep: no line matches '{argv[0]}'")
    return 0


def cmd_read(argv):
    """Print one day in full.

    This is the verb that makes a log readable from another machine: it returns
    content, so it crosses the memhub client/server boundary, which a path cannot.
    """
    stem = argv[0] if argv else clock().date().isoformat()
    if not LOG_DATE.match(stem):
        return fail(f"daylog read: '{stem}' is not a date of the form YYYY-MM-DD")

    path = STORE / f"{stem}.md"
    if not path.is_file():
        return fail(f"daylog read: no log for {stem}")

    head, extra = head_and_count(path, READ_LINES)
    print(f"[Log — {path.name}]")
    print("\n".join(head))
    if extra:
        print(f"... ({extra} more lines — raise DAYLOG_READ_LINES to see them)")
    return 0


def cmd_path(argv):
    stem = argv[0] if argv else clock().date().isoformat()
    print(STORE / f"{stem}.md")
    return 0


COMMANDS = {
    "note": cmd_note,
    "recent": cmd_recent,
    "grep": cmd_grep,
    "read": cmd_read,
    "path": cmd_path,
}


def main(argv):
    verb = argv[0] if argv else ""
    if verb in ("", "-h", "--help", "help"):
        print(USAGE)
        return 0
    if verb not in COMMANDS:
        print(f"daylog: unknown command '{verb}'", file=sys.stderr)
        print(file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 1
    return COMMANDS[verb](argv[1:])


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except BrokenPipeError:
        # `daylog grep x | head` is an ordinary thing to write. CPython sets
        # SIGPIPE to SIG_IGN at startup, so a closed pipe arrives as an exception
        # here rather than killing the process the way grep(1) dies. Retarget
        # stdout at devnull before exiting, or the interpreter reports the same
        # broken pipe again while flushing on the way out. 128+SIGPIPE, as a
        # process killed by the signal would have reported.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(141) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
