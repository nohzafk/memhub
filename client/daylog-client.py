#!/usr/bin/env python3
"""daylog — the running narrative of a working day, routed by path.

The same three-step routing as memo, the same failure behaviour, and
the same reason for existing: the server never learns the cwd, so only the client
can keep a session under a work root from writing its narrative to the server.

  1. `-g`/`--shared` forces the shared store; `-l`/`--local` forces the local one.
  2. Otherwise, a cwd under any entry of MEMHUB_LOCAL_ROOTS routes local.
  3. Otherwise, shared.

Accepted consequence on the work machine: one calendar day can end up
split across two files, because sessions under ~/work write the local day file
and sessions elsewhere write the shared one. That split IS the boundary.

Stdlib only, and the routing core is duplicated from memo-client.py on purpose:
each file is vendored alone, so neither may import the other.
server/tests/test_client.py runs the scope matrix against both.
"""

import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

TOOL = "daylog"
DEFAULT_URL = "http://127.0.0.1:8900"
DEFAULT_TOKEN_FILE = "/run/secrets/memhub-token"
DEFAULT_STORE = "~/.agents/memory/daily"

CONNECT_TIMEOUT = 2.0
READ_TIMEOUT = 30.0

# `note` is the only verb that writes. recent, path, grep and read all read.
WRITE_VERBS = {"note"}

USAGE = """daylog — the day's running narrative, routed by path.

  daylog note "<text>"     prepend a timestamped entry to today's log
  daylog recent [N]        the N most recent non-empty logs (default 3)
  daylog path [DATE]       the log path for DATE
  daylog grep <pattern>    search every day ever written
  daylog read [DATE]       print one day in full

  daylog -g <cmd>    the shared log on the server
  daylog -l <cmd>    this machine's local log

With neither flag: a cwd under MEMHUB_LOCAL_ROOTS is local, everything else is
shared. MEMHUB_URL is %s.""" % (os.environ.get("MEMHUB_URL") or DEFAULT_URL)


def die(msg, code=1):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def note_to_stderr(msg):
    print(msg, file=sys.stderr)


# ----------------------------------------------------------------- routing


def local_roots():
    """MEMHUB_LOCAL_ROOTS as absolute, symlink-resolved paths. An empty entry is
    dropped: "" prefixes every path, so one stray colon would route everything
    local."""
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
    """The cwd, resolved. None when it cannot be determined at all."""
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
        # Cannot prove this is not a work directory, so choose the recoverable
        # mistake: a shared entry in the local log, never work text on the server.
        return "local", argv
    return ("local" if under(cwd, roots) else "shared"), argv


# ------------------------------------------------------- the deny list

DEFAULT_DENY_FILE = "~/.config/memhub/deny-shared"

# Verbs that carry new text of their own. A read is never gated: `recall
# <term>` is how you find out whether the leak is already in the store.
GATED_VERBS = {"note"}


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
    is plainly work, which is how it went wrong before. The shared store has no
    delete path, so the text is read here, before the request exists."""
    if argv[0] not in GATED_VERBS or os.environ.get("MEMHUB_ALLOW_SHARED") == "1":
        return
    text = " ".join(argv[1:])
    for pattern, src in deny_patterns():
        hit = pattern.search(text)
        if hit:
            die(
                f'{TOOL}: this text names "{hit.group(0)}" (pattern {src!r}),'
                " which stays on this machine. Nothing was written.\n"
                f'  {TOOL} -l {argv[0]} "..."  keeps it local\n'
                "  MEMHUB_ALLOW_SHARED=1  overrides this, deliberately",
                2,
            )


# ------------------------------------------------------------ local scope


def local_tool():
    path = os.environ.get("MEMHUB_DAYLOG_PY")
    if not path:
        path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "daylog.py")
    if not os.path.isfile(path):
        die(
            f"daylog: no local daylog.py at {path}.\n"
            "Set MEMHUB_DAYLOG_PY, or use -g to reach the shared log."
        )
    return path


def run_local(argv):
    """Hand the process over to the tool. Never returns."""
    env = dict(os.environ)
    # setdefault, not assignment: a caller who has already pointed DAYLOG_DIR
    # somewhere means it.
    env.setdefault("DAYLOG_DIR", os.path.expanduser(DEFAULT_STORE))
    # The interpreter already running this router, exactly as memo-client.py does
    # it. daylog was a bash script until it grew `grep` and `read`, and the exec
    # here used to test the store copy's mode bit and fall back to `bash <file>`.
    # For a Python file that fallback is a wall of syntax errors, and sys.executable
    # needs neither the mode bit nor a PATH lookup to be right.
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
            f"daylog: cannot read the memhub token at {path} ({e.strerror or e}).\n"
            "Set MEMHUB_TOKEN_FILE, or use -l to write locally."
        )
    if not value:
        die(f"daylog: the token file {path} is empty.")
    return value


def unreachable(url, is_write):
    msg = f"daylog: memhub unreachable at {url}; shared memory unavailable"
    if is_write:
        msg += (
            "\nThe entry was NOT recorded. Retry when the server is back, or run"
            '\n  daylog -l note "..."\nif it must not wait.'
        )
    die(msg)


def reachable_or_die(url, is_write):
    """A 2s TCP probe before the request. urlopen takes one timeout for the whole
    exchange, and the common failure — a server that is off — must not cost the
    30 seconds a live server is allowed."""
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        socket.create_connection(
            (parts.hostname, port), timeout=CONNECT_TIMEOUT
        ).close()
    except OSError:
        unreachable(base_url(), is_write)


def post(path, payload, is_write=False):
    url = base_url() + path
    reachable_or_die(url, is_write)
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": "Bearer " + token(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=READ_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        handle_http_error(e, base_url())
    except OSError:
        # The host accepted and then stalled; the probe already caught "off".
        unreachable(base_url(), is_write)
    except ValueError:
        die(f"daylog: {url} answered with something that is not JSON.")


def handle_http_error(error, url):
    detail = ""
    try:
        detail = json.loads(error.read().decode("utf-8")).get("detail", "")
    except (ValueError, OSError):
        pass
    if error.code == 401:
        die(
            f"daylog: {url} rejected the token (401).\n"
            "Check MEMHUB_TOKEN_FILE, or re-run deploy/deploy.sh to push the"
            " current value."
        )
    why = f": {detail}" if detail else ""
    die(f"daylog: {url} answered {error.code}{why}")


def run_shared(argv):
    declared, argv = take_applies(argv)
    payload = {"tool": TOOL, "argv": argv}
    scope = scope_payload(declared)
    if scope is not None:
        payload["scope"] = scope
    body = post("/run", payload, is_write=argv[0] in WRITE_VERBS)
    sys.stdout.write(body.get("stdout", ""))
    sys.stderr.write(body.get("stderr", ""))
    print_scope_footer(body.get("scopes"))
    return int(body.get("exit_code", 1))


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

    This fails at `daylog wake`, the first command of every session — the
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
            "daylog: refusing to run — this machine's memory scope is not"
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

    if scope == "local":
        run_local(rest)  # never returns
    guard_shared(rest)
    return run_shared(rest)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
