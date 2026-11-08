"""The rules store: one text file of standing rules in a git repository.

Layout (docs/rules-store.md, D1-D5):

    <dir>/RULES.txt   one rule per line, ``#<id> <YYYY-MM-DD> <text>``
    <dir>/NEXT        the next id: one more than the largest id ever held
    <dir>/.git        every write is one commit

Blank lines and ``//`` lines in RULES.txt are not rules; they are kept where
they are and never written by this tool. Every write takes an exclusive
``flock`` on ``<dir>/.lock`` around the read-modify-write and the commit.

Stdlib only, so the same file serves as the server runner's module and as the
local ``memo`` CLI: ``python3 rules.py <verb> [args]`` against ``$MEMORY_DIR``.

Exit codes: 0 ok, 1 user error, 2 bad regex, 3 store missing or not
initialised.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

RULES = "RULES.txt"
NEXT = "NEXT"
LOCK = ".lock"
# Files in the store directory that git must never show: the lock, and the
# server's scope metadata (scope.META_NAME, scope.EPOCH_NAME).
EXCLUDED = (LOCK, "META.jsonl", "EPOCH")

MAX_TEXT_BYTES = 280
BUDGET_RULES = 120
BUDGET_BYTES = 24000
CURATE = "curate: the wake is over budget; merge or drop with memo merge / memo drop."
UNTAGGED = "(untagged)"

EXIT_OK = 0
EXIT_USER = 1
EXIT_REGEX = 2
EXIT_STORE = 3

LINE = re.compile(r"#(\d+) (\d{4}-\d{2}-\d{2}) (.*)")
# A tag is a word: it starts the text or follows whitespace, so `a#b` is not one.
TAG = re.compile(r"(?<!\S)#([A-Za-z][\w-]*)")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

USAGE = """usage: rules.py <verb> [args]
  init
  wake
  recall <regex>
  note "<text>"
  edit <id> "<text>"
  drop <id> ["<why>"]
  merge <id>,<id>[,...] "<text>"
"""


class RulesError(Exception):
    """A refused call: ``code`` is the exit code, the message goes to stderr."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Rule:
    id: int
    date: str
    text: str

    @property
    def line(self) -> str:
        return f"#{self.id} {self.date} {self.text}"


# ---- reading -----------------------------------------------------------------


def _is_rule_line(line: str) -> bool:
    return bool(line.strip()) and not line.startswith("//")


def _parse(line: str, lineno: int) -> Rule:
    m = LINE.fullmatch(line)
    if m is None or int(m.group(1)) <= 0:
        raise RulesError(EXIT_STORE, f"{RULES} line {lineno} is not a rule: {line!r}")
    return Rule(int(m.group(1)), m.group(2), m.group(3))


def _require_store(d: Path) -> None:
    if not ((d / RULES).is_file() and (d / NEXT).is_file() and (d / ".git").exists()):
        raise RulesError(
            EXIT_STORE, f"no rules store at {d}; run `rules.py init` to create one"
        )


def _read_lines(d: Path) -> list[str]:
    return (d / RULES).read_text(encoding="utf-8").splitlines()


def _rules_of(lines: list[str]) -> list[Rule]:
    return [_parse(ln, i) for i, ln in enumerate(lines, 1) if _is_rule_line(ln)]


def rule_count(d: Path | str) -> int:
    """How many lines of RULES.txt are rules; 0 when there is no file. Parses
    nothing, so a malformed line still counts and /health cannot fail on it."""
    try:
        lines = (Path(d) / RULES).read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    return sum(1 for ln in lines if _is_rule_line(ln))


def read_rules(d: Path | str) -> list[Rule]:
    """The rules in force, in id order."""
    d = Path(d)
    _require_store(d)
    return sorted(_rules_of(_read_lines(d)), key=lambda r: r.id)


def _read_next(d: Path, rules: list[Rule]) -> int:
    raw = (d / NEXT).read_text(encoding="utf-8").strip()
    if not raw.isdigit() or int(raw) <= 0:
        raise RulesError(EXIT_STORE, f"{NEXT} does not hold a positive id: {raw!r}")
    # A hand-written line above NEXT must not have its id handed out again.
    return max([int(raw)] + [r.id + 1 for r in rules])


# ---- checks ------------------------------------------------------------------


def check_text(text: str) -> str:
    """The text check of note, edit and merge; returns the text stripped."""
    text = text.strip()
    if not text:
        raise RulesError(EXIT_USER, "the text is empty")
    if "\n" in text or "\r" in text:
        raise RulesError(EXIT_USER, "the text must be one line")
    n = len(text.encode("utf-8"))
    if n > MAX_TEXT_BYTES:
        raise RulesError(
            EXIT_USER,
            f"the text is {n} bytes; at most {MAX_TEXT_BYTES} UTF-8 bytes are allowed",
        )
    return text


def parse_id(s: str) -> int:
    """`12` or `#12`."""
    s = s.strip().removeprefix("#")
    if not s.isdigit() or int(s) <= 0:
        raise RulesError(EXIT_USER, f"not a rule id: {s!r}")
    return int(s)


def parse_ids(s: str) -> list[int]:
    ids = [parse_id(p) for p in s.split(",") if p.strip()]
    if len(ids) < 2:
        raise RulesError(EXIT_USER, "merge needs at least two ids: <id>,<id>[,...]")
    if len(set(ids)) != len(ids):
        raise RulesError(EXIT_USER, "merge lists an id twice")
    return ids


def today() -> str:
    """Today in UTC, or ``$MEMO_DATE`` when it is set."""
    forced = os.environ.get("MEMO_DATE")
    if forced is not None:
        if not DATE.fullmatch(forced):
            raise RulesError(EXIT_USER, f"MEMO_DATE is not YYYY-MM-DD: {forced!r}")
        return forced
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d")


# ---- git and the lock --------------------------------------------------------


def _author(author: str | None = None) -> tuple[str, str]:
    """``"role host"`` -> ("role host", "role@host").

    ``author`` is the caller's; without one, ``$MEMHUB_AUTHOR`` is read. The
    server passes it, because a threaded server cannot set the environment per
    request.
    """
    raw = (os.environ.get("MEMHUB_AUTHOR", "") if author is None else author).strip()
    parts = raw.split()
    if len(parts) == 2:
        return raw, f"{parts[0]}@{parts[1]}"
    if raw:
        return raw, "memhub@localhost"
    return "memhub", "memhub@localhost"


def _git(d: Path, *args: str, author: str | None = None) -> None:
    name, email = _author(author)
    env = dict(os.environ)
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME=name,
        GIT_AUTHOR_EMAIL=email,
        GIT_COMMITTER_NAME=name,
        GIT_COMMITTER_EMAIL=email,
    )
    argv = ["git", "-C", str(d), "-c", "commit.gpgsign=false", *args]
    p = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {p.stderr.strip()}")


def _commit(d: Path, message: str, author: str | None = None) -> None:
    _git(d, "add", "--", RULES, NEXT, author=author)
    _git(d, "commit", "-q", "--no-verify", "-m", message, author=author)


@contextmanager
def _locked(d: Path):
    with open(d / LOCK, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _write_atomic(path: Path, data: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def _store(
    d: Path, lines: list[str], next_id: int, message: str, author: str | None = None
) -> None:
    """Write RULES.txt and NEXT and commit them; undo the files if the commit fails."""
    old_rules = (d / RULES).read_text(encoding="utf-8")
    old_next = (d / NEXT).read_text(encoding="utf-8")
    _write_atomic(d / RULES, "".join(ln + "\n" for ln in lines))
    _write_atomic(d / NEXT, f"{next_id}\n")
    try:
        _commit(d, message, author)
    except BaseException:
        _write_atomic(d / RULES, old_rules)
        _write_atomic(d / NEXT, old_next)
        raise


def _index_of(lines: list[str], rule_id: int) -> int:
    for i, ln in enumerate(lines):
        if _is_rule_line(ln) and _parse(ln, i + 1).id == rule_id:
            return i
    raise RulesError(EXIT_USER, f"no rule #{rule_id}")


# ---- verbs -------------------------------------------------------------------


def init(d: Path | str) -> None:
    """Create the store: a git repository with an empty RULES.txt and NEXT=1."""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    if (d / RULES).exists() or (d / NEXT).exists():
        raise RulesError(EXIT_USER, f"a rules store already exists at {d}")
    with _locked(d):
        _git(d, "init", "-q")
        exclude = d / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        # The server's scope metadata (scope.py) lives here too and travels
        # with /export, but it is not the store's history.
        with exclude.open("a", encoding="utf-8") as f:
            f.write("".join(f"{name}\n" for name in EXCLUDED))
        (d / RULES).write_text("", encoding="utf-8")
        (d / NEXT).write_text("1\n", encoding="utf-8")
        _commit(d, "init")


def wake(d: Path | str) -> str:
    """Every rule, grouped by first tag (D5), then the footer (D4)."""
    d = Path(d)
    rules = read_rules(d)
    groups: dict[str, list[Rule]] = {}
    untagged: list[Rule] = []
    for r in rules:  # id order, so groups appear in order of their earliest id
        m = TAG.search(r.text)
        if m is None:
            untagged.append(r)
        else:
            groups.setdefault(m.group(1), []).append(r)
    out: list[str] = []
    for tag, members in groups.items():
        out.append(f"## {tag}")
        out.extend(r.line for r in members)
    if untagged:
        out.append(f"## {UNTAGGED}")
        out.extend(r.line for r in untagged)
    size = (d / RULES).stat().st_size
    out.append(f"{len(rules)} rules, {size} bytes.")
    if len(rules) > BUDGET_RULES or size > BUDGET_BYTES:
        out.append(CURATE)
    return "".join(ln + "\n" for ln in out)


def recall(d: Path | str, pattern: str) -> str:
    """Every rule whose text matches ``pattern``, case-insensitive, in id order."""
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise RulesError(EXIT_REGEX, f"bad regex {pattern!r}: {e}") from None
    return "".join(r.line + "\n" for r in read_rules(d) if rx.search(r.text))


def note(d: Path | str, text: str, author: str | None = None) -> int:
    """Append a rule with the next id and today's date; returns the id."""
    d = Path(d)
    text = check_text(text)
    date = today()
    _require_store(d)
    with _locked(d):
        lines = _read_lines(d)
        new_id = _read_next(d, _rules_of(lines))
        lines.append(Rule(new_id, date, text).line)
        _store(d, lines, new_id + 1, f"note #{new_id}", author)
    return new_id


def edit(d: Path | str, rule_id: int, text: str, author: str | None = None) -> None:
    """Replace a rule's text in place; id and date stay."""
    d = Path(d)
    text = check_text(text)
    _require_store(d)
    with _locked(d):
        lines = _read_lines(d)
        rules = _rules_of(lines)
        i = _index_of(lines, rule_id)
        old = _parse(lines[i], i + 1)
        lines[i] = Rule(old.id, old.date, text).line
        _store(d, lines, _read_next(d, rules), f"edit #{rule_id}", author)


def drop(
    d: Path | str, rule_id: int, why: str | None = None, author: str | None = None
) -> None:
    """Remove a rule; ``why`` goes into the commit message only."""
    d = Path(d)
    why = (why or "").strip()
    if "\n" in why or "\r" in why:
        raise RulesError(EXIT_USER, "the reason must be one line")
    _require_store(d)
    with _locked(d):
        lines = _read_lines(d)
        rules = _rules_of(lines)
        del lines[_index_of(lines, rule_id)]
        message = f"drop #{rule_id}: {why}" if why else f"drop #{rule_id}"
        _store(d, lines, _read_next(d, rules), message, author)


def merge(d: Path | str, ids: list[int], text: str, author: str | None = None) -> int:
    """Remove the listed rules and append one with a new id, in one commit."""
    d = Path(d)
    text = check_text(text)
    if len(ids) < 2 or len(set(ids)) != len(ids):
        raise RulesError(EXIT_USER, "merge needs at least two distinct ids")
    date = today()
    _require_store(d)
    with _locked(d):
        lines = _read_lines(d)
        rules = _rules_of(lines)
        gone = {lines[_index_of(lines, i)] for i in ids}
        new_id = _read_next(d, rules)
        lines = [ln for ln in lines if ln not in gone]
        lines.append(Rule(new_id, date, text).line)
        src = ",".join(f"#{i}" for i in ids)
        _store(d, lines, new_id + 1, f"merge {src} -> #{new_id}", author)
    return new_id


# ---- argv --------------------------------------------------------------------


def _arity(args: list[str], lo: int, hi: int, verb: str) -> None:
    if not lo <= len(args) <= hi:
        raise RulesError(EXIT_USER, f"wrong number of arguments for {verb}\n{USAGE}")


def run(
    argv: list[str], d: Path | str, author: str | None = None
) -> tuple[int, str, str]:
    """Run one call; returns (exit code, stdout, stderr). Never raises RulesError.

    ``author`` names the commit's author for a write (see ``_author``).
    """
    try:
        if not argv:
            raise RulesError(EXIT_USER, USAGE)
        verb, args = argv[0], argv[1:]
        match verb:
            case "init":
                _arity(args, 0, 0, verb)
                init(d)
                return EXIT_OK, f"Initialised rules store at {d}.\n", ""
            case "wake":
                _arity(args, 0, 0, verb)
                return EXIT_OK, wake(d), ""
            case "recall":
                _arity(args, 1, 1, verb)
                return EXIT_OK, recall(d, args[0]), ""
            case "note":
                _arity(args, 1, 1, verb)
                return EXIT_OK, f"Saved as #{note(d, args[0], author)}.\n", ""
            case "edit":
                _arity(args, 2, 2, verb)
                rule_id = parse_id(args[0])
                edit(d, rule_id, args[1], author)
                return EXIT_OK, f"Edited #{rule_id}.\n", ""
            case "drop":
                _arity(args, 1, 2, verb)
                rule_id = parse_id(args[0])
                drop(d, rule_id, args[1] if len(args) > 1 else None, author)
                return EXIT_OK, f"Dropped #{rule_id}.\n", ""
            case "merge":
                _arity(args, 2, 2, verb)
                return (
                    EXIT_OK,
                    f"Saved as #{merge(d, parse_ids(args[0]), args[1], author)}.\n",
                    "",
                )
            case _:
                raise RulesError(EXIT_USER, f"unknown verb {verb!r}\n{USAGE}")
    except RulesError as e:
        msg = e.message if e.message.endswith("\n") else e.message + "\n"
        return e.code, "", msg


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    d = os.environ.get("MEMORY_DIR")
    if not d:
        sys.stderr.write("MEMORY_DIR is not set; it names the rules store\n")
        return EXIT_STORE
    code, out, err = run(argv, d)
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


if __name__ == "__main__":
    sys.exit(main())
