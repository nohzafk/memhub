#!/usr/bin/env python3
"""memo — one memory, two stores: the shared one on the server, the local one here.

Every invocation is routed before it runs:

  1. `-g`/`--shared` forces the shared store; `-l`/`--local` forces the local one.
     The flag comes first, which is the shape the tools themselves print:
     `memo -g drop 12`, `memo -l note "..."`.
  2. Otherwise, a cwd under any entry of MEMHUB_LOCAL_ROOTS routes local.
  3. Otherwise, shared.

Rule 2 IS the confidentiality boundary. The server never learns the directory a
command was typed in, so nothing downstream can make this decision — by the time
a request reaches /run, work text has already left the machine or it has not.

Shared scope POSTs /run and exits with the tool's own exit code. A shared
write that cannot reach the server waits in the outbox and is sent before the
next shared call that can. Local scope execs the rules store tool (rules.py),
replacing this process, so output and exit codes pass through untouched.

Stdlib only, on purpose: the Nix config repo vendors this file into a Nix wrapper and
the second config repo installs it directly, neither with a dependency closure.
server/tests/test_client.py runs the scope matrix against it.
"""

import fcntl
import json
import os
import re
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime
from urllib.parse import urlsplit

TOOL = "memo"
DEFAULT_URL = "http://127.0.0.1:8900"
DEFAULT_TOKEN_FILE = "/run/secrets/memhub-token"
DEFAULT_STORE = "~/.agents/memory/rules"
DEFAULT_OUTBOX = "~/.agents/memory/outbox"

# Fail fast on an unreachable server, but give a live one time to answer: a session
# must never hang at startup because the box is off.
CONNECT_TIMEOUT = 2.0
READ_TIMEOUT = 30.0

# Verbs that change the store. An unreachable server queues them in the outbox;
# a read has nothing to keep and simply fails.
READ_VERBS = {"wake", "recall"}
WRITE_VERBS = {"note", "edit", "drop", "merge"}
VERBS = READ_VERBS | WRITE_VERBS

USAGE = """memo — durable rules, routed by path.

  memo wake                          every rule, grouped by tag
  memo recall <regex>                the rules whose text matches
  memo note [--here|--anywhere] "..."  add a rule
  memo edit <id> "..."               replace a rule's text
  memo drop <id> ["why"]             retire a rule
  memo merge <id>,<id>[,...] "..."   replace several rules with one

  memo -g <cmd>    the shared memory on the server
  memo -l <cmd>    this machine's local memory

With neither flag: a cwd under MEMHUB_LOCAL_ROOTS is local, everything else is
shared. MEMHUB_URL is %s.""" % (os.environ.get("MEMHUB_URL") or DEFAULT_URL)


def die(msg, code=1):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def note_to_stderr(msg):
    print(msg, file=sys.stderr)


# ----------------------------------------------------------------- routing


def local_roots():
    """MEMHUB_LOCAL_ROOTS as absolute, symlink-resolved paths.

    An empty entry is dropped rather than treated as a root: "" is a prefix of
    every path, so one stray colon would route the whole machine local."""
    # The literal `none` is how a machine says it keeps no local memory, and is
    # the only way to say so: unset or empty is a misconfiguration and never
    # reaches here (see require_config).
    if (os.environ.get("MEMHUB_LOCAL_ROOTS") or "").strip().lower() == NO_LOCAL_ROOTS:
        return []
    out = []
    for part in (os.environ.get("MEMHUB_LOCAL_ROOTS") or "").split(":"):
        part = part.strip()
        if part:
            out.append(os.path.realpath(os.path.expanduser(part)))
    return out


def current_dir():
    """The cwd, resolved. None when it cannot be determined at all — a deleted
    directory, most often, where getcwd() fails but the shell's PWD survives."""
    try:
        return os.path.realpath(os.getcwd())
    except OSError:
        pwd = os.environ.get("PWD")
        return os.path.realpath(pwd) if pwd else None


def under(path, roots):
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def resolve_scope(argv):
    """(scope, argv-without-the-flag)."""
    if argv and argv[0] in ("-g", "--shared"):
        return "shared", argv[1:]
    if argv and argv[0] in ("-l", "--local"):
        return "local", argv[1:]

    roots = local_roots()
    if not roots:
        # A machine that keeps a local store must say which paths feed it. If it
        # does not, the safe reading is that every path does: an unset variable
        # would otherwise route confidential work to the shared store silently,
        # and unlike a missing label that cannot be undone. Routing local instead
        # loses nothing and shows up immediately.
        if (os.environ.get("MEMHUB_MACHINE") or "").strip() in LOCAL_STORE_ROLES:
            note_to_stderr(
                f"{TOOL}: MEMHUB_LOCAL_ROOTS is empty on a machine that keeps a"
                " local store; routing local. Set it, or pass -g to force shared."
            )
            return "local", argv
        return "shared", argv
    cwd = current_dir()
    if cwd is None:
        # Cannot prove this is not a work directory. Writing a shared memory to
        # the local store is recoverable; sending work text to the server is the
        # one thing that cannot be undone. Choose the recoverable mistake.
        return "local", argv
    return ("local" if under(cwd, roots) else "shared"), argv


# ------------------------------------------------------- the deny list

DEFAULT_DENY_FILE = "~/.config/memhub/deny-shared"

# Verbs that carry text of their own, and how many arguments precede it:
# `note "<text>"`, `edit <id> "<text>"`, `merge <ids> "<text>"`, and
# `drop <id> "<why>"`, whose reason lands in the server's git history. A read
# is never gated: `recall <term>` is how you find out whether the leak is
# already in the store.
GATED_VERBS = {"note": 0, "edit": 1, "merge": 1, "drop": 1}


def deny_patterns():
    """(compiled, source) for every regex in MEMHUB_DENY_SHARED_FILE.

    No file means no gate: a machine with nothing to keep local says so by not
    having one. A file that cannot be parsed is a hard error, though -- a gate
    that silently stops gating is worse than no gate at all."""
    path = os.path.expanduser(
        os.environ.get("MEMHUB_DENY_SHARED_FILE") or DEFAULT_DENY_FILE
    )
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out = []
    for n, line in enumerate(lines, 1):
        src = line.split("#", 1)[0].strip()
        if not src:
            continue
        try:
            out.append((re.compile(src, re.IGNORECASE), src))
        except re.error as exc:
            die(
                f"{TOOL}: {path} line {n}: {src!r} is not a valid regex ({exc})."
                " Nothing was written."
            )
    return out


def guard_shared(argv):
    """Refuse a shared write whose text names something this machine keeps local.

    resolve_scope decides by directory, and a directory is not a topic: work
    done from a cwd outside MEMHUB_LOCAL_ROOTS routes shared even when the text
    is plainly work, which is how it went wrong before. A drop on the server
    still leaves the text in the store's history, so the text is read here,
    before the request exists."""
    verb = argv[0]
    if verb not in GATED_VERBS or os.environ.get("MEMHUB_ALLOW_SHARED") == "1":
        return
    _, args = take_applies(argv)
    text = " ".join(args[1 + GATED_VERBS[verb] :])
    for pattern, src in deny_patterns():
        hit = pattern.search(text)
        if hit:
            # An id names a shared rule, so `-l edit <id>` would edit some other
            # rule: the local way out is always a new note. A drop's reason has
            # no local home; the drop itself needs none.
            if verb == "drop":
                way_out = f"  {TOOL} -g drop <id>  drops it without a reason"
            else:
                retire = (
                    ""
                    if verb == "note"
                    else f"\n  {TOOL} -g drop <id>  retires the shared rule instead"
                )
                way_out = f'  {TOOL} -l note "..."  keeps it local{retire}'
            die(
                f'{TOOL}: this text names "{hit.group(0)}" (pattern {src!r}),'
                " which stays on this machine. Nothing was written.\n"
                f"{way_out}\n"
                "  MEMHUB_ALLOW_SHARED=1  overrides this, deliberately",
                2,
            )


# ------------------------------------------------------------ local scope


def local_tool():
    path = os.environ.get("MEMHUB_MEMO_PY")
    if not path:
        path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "rules.py")
    if not os.path.isfile(path):
        die(
            f"memo: no local rules.py at {path}.\n"
            "Set MEMHUB_MEMO_PY, or use -g to reach the shared memory."
        )
    return path


def run_local(argv):
    """Hand the process over to the rules store tool. Never returns."""
    env = dict(os.environ)
    # setdefault, not assignment: a caller who has already pointed MEMORY_DIR
    # somewhere means it.
    env.setdefault("MEMORY_DIR", os.path.expanduser(DEFAULT_STORE))
    os.execve(sys.executable, [sys.executable, local_tool()] + argv, env)


# ----------------------------------------------------------- shared scope


def base_url():
    return (os.environ.get("MEMHUB_URL") or DEFAULT_URL).rstrip("/")


def token():
    path = os.environ.get("MEMHUB_TOKEN_FILE") or DEFAULT_TOKEN_FILE
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError as e:
        die(
            f"memo: cannot read the memhub token at {path} ({e.strerror or e}).\n"
            "Set MEMHUB_TOKEN_FILE, or use -l to record locally."
        )
    if not value:
        die(f"memo: the token file {path} is empty.")
    return value


def unreachable(url, is_write):
    """The one message that is fixed verbatim, because the SessionStart hook
    matches on a failed call rather than on its text."""
    msg = f"memo: memhub unreachable at {url}; shared memory unavailable"
    if is_write:
        msg += (
            "\nThe rule was NOT recorded. Retry when the server is back, or run"
            '\n  memo -l note "..."\nif the fact must not wait.'
        )
    die(msg)


class Unreachable(Exception):
    """The transport failed: no connection, or no answer within READ_TIMEOUT."""


def probe(url):
    """A 2s TCP probe before any request.

    urlopen takes one timeout for the whole exchange, and 30s of it is right for
    a live server. But the common failure is a server that is simply off, and the
    SessionStart hook runs `memo -g wake` before the session begins — so the
    unreachable case has to be known in about two seconds, not thirty.
    """
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        socket.create_connection(
            (parts.hostname, port), timeout=CONNECT_TIMEOUT
        ).close()
    except OSError:
        return False
    return True


def send(path, payload):
    """POST once and return the decoded body of a 2xx answer.

    Raises Unreachable on a transport failure and lets urllib's HTTPError
    through, so each caller decides what a failure means for it."""
    url = base_url() + path
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + token(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=READ_TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError:
        raise
    except OSError as e:
        # URLError, socket.timeout and a dropped connection are all OSError.
        raise Unreachable(str(e)) from e
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        die(f"memo: {url} answered with something that is not JSON.")


def http_detail(error):
    try:
        return json.loads(error.read().decode("utf-8")).get("detail", "")
    except (ValueError, OSError, AttributeError):
        return ""


def handle_http_error(error, url):
    detail = http_detail(error)
    if error.code == 401:
        die(
            f"memo: {url} rejected the token (401).\n"
            "Check MEMHUB_TOKEN_FILE, or re-run deploy/deploy.sh to push the"
            " current value."
        )
    why = f": {detail}" if detail else ""
    die(f"memo: {url} answered {error.code}{why}")


def run_payload(argv, scope):
    payload = {"tool": TOOL, "argv": argv}
    if scope is not None:
        payload["scope"] = scope
    return payload


def run_shared(argv):
    verb = argv[0]
    declared, argv = take_applies(argv)
    scope = scope_payload(declared)
    url = base_url()
    if not probe(url):
        return offline(verb, argv, scope)
    # The hub may go away while the outbox drains. Only a failed probe says so:
    # one request that timed out is not reason enough to queue this call too.
    if not replay_outbox() and not probe(url):
        return offline(verb, argv, scope)
    try:
        body = send("/run", run_payload(argv, scope))
    except urllib.error.HTTPError as e:
        handle_http_error(e, url)
    except Unreachable:
        # The host accepted the connection and then stalled past READ_TIMEOUT.
        # A write may or may not have landed, so it is not queued: a second
        # copy of a rule is a worse outcome than a message.
        unreachable(url, verb in WRITE_VERBS)
    sys.stdout.write(body.get("stdout", ""))
    sys.stderr.write(body.get("stderr", ""))
    print_scope_footer(body.get("scopes"))
    return int(body.get("exit_code", 1))


def offline(verb, argv, scope):
    """The hub is unreachable: a write waits in the outbox, a read fails."""
    url = base_url()
    if verb not in WRITE_VERBS:
        unreachable(url, False)
    try:
        n = enqueue(argv, scope)
    except OSError as e:
        note_to_stderr(f"memo: cannot write the outbox at {outbox_path()} ({e}).")
        unreachable(url, True)
    print(
        f"memhub unreachable at {url}; queued as outbox entry {n},"
        " sent on the next shared call."
    )
    return 0


# ----------------------------------------------------------------- outbox
#
# A shared write that could not reach the hub, one JSON object per line:
# {"ts": <ISO UTC>, "argv": [...], "scope": {...} | null}. Only text that
# routing already sent to the shared store (and the deny list let through)
# is ever written here, so the confidentiality boundary is where it was.


def outbox_path():
    return os.path.expanduser(os.environ.get("MEMHUB_OUTBOX") or DEFAULT_OUTBOX)


@contextmanager
def outbox_lock(path):
    """One process at a time appends to or drains the outbox, so an entry is
    never sent twice by two sessions that start together. The lock is a
    sibling file: the outbox itself is replaced on every rewrite."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def read_outbox(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return [line for line in fh.read().splitlines() if line.strip()]
    except FileNotFoundError:
        return []


def write_outbox(path, lines):
    """Replace the outbox in one rename, so a crash leaves the old file or the
    new one and never half of either."""
    fd, tmp = tempfile.mkstemp(prefix=".outbox.", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in lines))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def enqueue(argv, scope):
    """Append one call to the outbox; returns its entry number (1-based)."""
    path = outbox_path()
    entry = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "argv": argv,
        "scope": scope,
    }
    with outbox_lock(path):
        lines = read_outbox(path) + [json.dumps(entry)]
        write_outbox(path, lines)
    return len(lines)


def parse_entry(line):
    entry = json.loads(line)
    argv = entry["argv"]
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        raise ValueError("argv is not a list of strings")
    scope = entry.get("scope")
    if scope is not None and not isinstance(scope, dict):
        raise ValueError("scope is not an object")
    return argv, scope


def replay_outbox():
    """Send every queued write, in file order, each as its own request.

    An entry the server answered 200 is done, whatever the tool's exit code: a
    refusal (a text the tool rejects) would be refused again forever. A
    transport failure stops the replay and keeps that entry and the rest; an
    HTTP error does the same, but this call still proceeds. The deny list is
    not read again: it was read before the entry was queued.

    Returns False when the transport failed."""
    path = outbox_path()
    if not os.path.exists(path):
        return True
    with outbox_lock(path):
        lines = read_outbox(path)
        kept = []
        reached = True
        for n, line in enumerate(lines, 1):
            try:
                argv, scope = parse_entry(line)
            except (ValueError, KeyError, TypeError) as e:
                note_to_stderr(f"outbox entry {n} is unreadable ({e}); kept in {path}")
                kept.append(line)
                continue
            try:
                body = send("/run", run_payload(argv, scope))
            except urllib.error.HTTPError as e:
                why = http_detail(e)
                note_to_stderr(
                    f"outbox entry {n} not sent ({e.code}{': ' + why if why else ''});"
                    " kept with the rest"
                )
                kept.extend(lines[n - 1 :])
                break
            except Unreachable:
                kept.extend(lines[n - 1 :])
                reached = False
                break
            if int(body.get("exit_code", 1)) == 0:
                note_to_stderr(f"outbox: sent entry {n}")
            else:
                err = (body.get("stderr") or "").strip() or "no reason given"
                note_to_stderr(f"outbox entry {n} refused: {err}")
        if kept != lines:
            write_outbox(path, kept)
    return reached


# ------------------------------------------------------------------- scope

# A role, not a hostname: hostnames rot, and "the laptop" is ambiguous once two
# Macs share one store. The server accepts only these.
MACHINE_ROLES = ("work", "personal", "omarchy", "nuc", "cloud", "phone")

# How a machine declares that it keeps no local memory. Saying it out loud is the
# point: an unset variable cannot then be mistaken for this answer, so a wrapper
# that stops exporting MEMHUB_LOCAL_ROOTS on the work machine fails loudly instead of
# quietly routing confidential work to the shared store.
NO_LOCAL_ROOTS = "none"

# Roles that keep a local store beside the shared one, so an empty
# MEMHUB_LOCAL_ROOTS on them is a misconfiguration rather than a choice.
LOCAL_STORE_ROLES = ("work",)


def project_name():
    """The nearest ancestor directory holding a `.git`, by name.

    Pure stdlib and no subprocess: this runs on every single command."""
    path = current_dir()
    if path is None:
        return None
    while True:
        if os.path.isdir(os.path.join(path, ".git")):
            return os.path.basename(path) or None
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def scope_payload(declared=None):
    """Where this command is standing, for the server's META.jsonl row.

    The client reports only what it can observe — its role, its host, its
    project, its actor — and `applies` only when the author declared it. Whether
    a project is machine-bound is a property of the project, identical on every
    machine, so that policy lives on the server: three copies of one list would
    be three chances to disagree about the same repo.

    None when MEMHUB_MACHINE is unset or unknown: the write still happens, it
    just lands unlabelled and `/verify` reports it. A missing label must never
    cost a memory.
    """
    machine = (os.environ.get("MEMHUB_MACHINE") or "").strip()
    if machine not in MACHINE_ROLES:
        return None
    payload = {
        "machine": machine,
        "host": socket.gethostname(),
        "project": project_name(),
        "actor": (os.environ.get("MEMHUB_ACTOR") or "").strip() or None,
    }
    if declared is not None:
        payload["applies"] = declared
        payload["asserted"] = "declared"
    return payload


def require_config():
    """Refuse to run until this machine has declared who it is and what stays
    here. Both are configuration, not runtime conditions: a missing label costs
    an annotation, but a missing boundary would send confidential work to the
    shared store, and that cannot be undone.

    This fails at `memo wake`, the first command of every session — the
    cheapest moment to find out, and impossible to miss.
    """
    problems = []
    machine = (os.environ.get("MEMHUB_MACHINE") or "").strip()
    roles = ", ".join(MACHINE_ROLES)
    if not machine:
        problems.append(
            f"MEMHUB_MACHINE is not set. Set it to this machine's role: {roles}."
        )
    elif machine not in MACHINE_ROLES:
        problems.append(
            f"MEMHUB_MACHINE is {machine!r}, which is not a known role."
            f" Use one of: {roles}."
        )
    if not (os.environ.get("MEMHUB_LOCAL_ROOTS") or "").strip():
        problems.append(
            "MEMHUB_LOCAL_ROOTS is not set. Set it to a colon-separated list of"
            " paths whose memory must stay on this machine, or to"
            f" '{NO_LOCAL_ROOTS}' if this machine keeps no local memory."
        )
    if problems:
        die(
            "memo: refusing to run — this machine's memory scope is not"
            " configured.\n" + "\n".join(f"  {problem}" for problem in problems)
        )


def take_applies(argv):
    """`note --here "..."` — the flag sits immediately after the verb, so a
    memory whose own text contains it is never mistaken for a flag."""
    if len(argv) > 1 and argv[1] in ("--here", "--anywhere"):
        applies = "host" if argv[1] == "--here" else "portable"
        return applies, argv[:1] + argv[2:]
    return None, argv


def print_scope_footer(scopes):
    """Name the memories above that were written somewhere else.

    Appended after the tool's own output, never woven into it: nothing here can
    corrupt a memory, and a scope that could not be looked up simply goes
    unmentioned. A memory marked portable is true everywhere, so it is never
    foreign, however far away it was written.
    """
    if not scopes:
        return
    mine = (os.environ.get("MEMHUB_MACHINE") or "").strip()
    foreign = [
        s for s in scopes if s.get("machine") != mine and s.get("applies") != "portable"
    ]
    if not foreign:
        return
    was = "was" if len(foreign) == 1 else "were"
    print(f"\n-- scope: {len(foreign)} of the memories above {was} written elsewhere")
    for row in foreign:
        label = row.get("label") or "scope not recorded"
        print(f"   {row.get('ref')} [{label}]")


# ------------------------------------------------------------------- main


def main(argv):
    if argv and argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    require_config()
    scope, rest = resolve_scope(argv)
    if not rest:
        print(USAGE)
        return 0

    if rest[0] not in VERBS:
        die(f"memo: unknown verb {rest[0]!r}.\n\n{USAGE}", 2)

    if scope == "local":
        run_local(rest)  # never returns
    guard_shared(rest)
    return run_shared(rest)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
