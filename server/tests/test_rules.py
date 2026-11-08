"""The rules store (docs/rules-store.md D1-D5), driven against a real git repo."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from memhub import rules
from memhub.rules import RulesError

RULES_PY = Path(rules.__file__)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMO_DATE", "2026-01-02")
    monkeypatch.delenv("MEMHUB_AUTHOR", raising=False)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    d = tmp_path / "rules"
    rules.init(d)
    return d


def git(d: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(d), *args], capture_output=True, text=True, check=True
    ).stdout


def log(d: Path) -> list[str]:
    """Commit subjects, oldest first."""
    return git(d, "log", "--reverse", "--format=%s").splitlines()


def file_lines(d: Path) -> list[str]:
    return (d / "RULES.txt").read_text(encoding="utf-8").splitlines()


def next_id(d: Path) -> str:
    return (d / "NEXT").read_text(encoding="utf-8").strip()


def test_init_creates_a_repo_with_one_commit(store: Path):
    assert log(store) == ["init"]
    assert file_lines(store) == []
    assert next_id(store) == "1"
    assert git(store, "status", "--porcelain") == ""


def test_init_refuses_an_existing_store(store: Path):
    with pytest.raises(RulesError) as e:
        rules.init(store)
    assert e.value.code == 1


def test_note_assigns_ids_and_commits_each(store: Path):
    assert [rules.note(store, f"rule {i}") for i in range(3)] == [1, 2, 3]
    assert file_lines(store) == [
        "#1 2026-01-02 rule 0",
        "#2 2026-01-02 rule 1",
        "#3 2026-01-02 rule 2",
    ]
    assert next_id(store) == "4"
    assert log(store) == ["init", "note #1", "note #2", "note #3"]
    assert git(store, "status", "--porcelain") == ""


def test_author_comes_from_memhub_author(store: Path, monkeypatch):
    rules.note(store, "default author")
    monkeypatch.setenv("MEMHUB_AUTHOR", "laptop alpha")
    rules.note(store, "scoped author")
    authors = git(store, "log", "--reverse", "--format=%an <%ae>").splitlines()
    assert authors[1:] == ["memhub <memhub@localhost>", "laptop alpha <laptop@alpha>"]


@pytest.mark.parametrize(
    "text, needle",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("two\nlines", "one line"),
        # 140 two-byte chars = 280 bytes is fine; one more ASCII char is 281
        # bytes but only 141 characters.
        ("é" * 140 + "x", "281 bytes"),
    ],
)
def test_text_check_rejects_and_writes_nothing(store: Path, text: str, needle: str):
    rules.note(store, "keep me")
    before = (file_lines(store), next_id(store), log(store))
    for call in (
        lambda: rules.note(store, text),
        lambda: rules.edit(store, 1, text),
        lambda: rules.merge(store, [1, 1], text),
    ):
        with pytest.raises(RulesError) as e:
            call()
        assert e.value.code == 1
        assert needle in e.value.message
    assert (file_lines(store), next_id(store), log(store)) == before


def test_text_of_exactly_280_bytes_is_accepted(store: Path):
    assert rules.note(store, "é" * 140) == 1


def test_edit_keeps_id_and_date(store: Path, monkeypatch):
    rules.note(store, "first")
    rules.note(store, "second")
    monkeypatch.setenv("MEMO_DATE", "2027-05-06")
    rules.edit(store, 1, "first, corrected")
    assert file_lines(store) == [
        "#1 2026-01-02 first, corrected",
        "#2 2026-01-02 second",
    ]
    assert next_id(store) == "3"
    assert log(store)[-1] == "edit #1"


def test_unknown_id_is_a_user_error(store: Path):
    rules.note(store, "only")
    for call in (
        lambda: rules.edit(store, 9, "x"),
        lambda: rules.drop(store, 9),
        lambda: rules.merge(store, [1, 9], "x"),
    ):
        with pytest.raises(RulesError) as e:
            call()
        assert (e.value.code, e.value.message) == (1, "no rule #9")
    assert file_lines(store) == ["#1 2026-01-02 only"]
    assert len(log(store)) == 2


def test_drop_removes_the_line_and_never_frees_the_id(store: Path):
    rules.note(store, "a")
    rules.note(store, "b")
    rules.drop(store, 2, "superseded by #1")
    assert file_lines(store) == ["#1 2026-01-02 a"]
    assert next_id(store) == "3"
    assert log(store)[-1] == "drop #2: superseded by #1"
    rules.drop(store, 1)
    assert log(store)[-1] == "drop #1"
    assert rules.note(store, "c") == 3


def test_merge_is_one_commit(store: Path, monkeypatch):
    for t in ("a", "b", "c"):
        rules.note(store, t)
    monkeypatch.setenv("MEMO_DATE", "2026-03-04")
    commits = len(log(store))
    assert rules.merge(store, [1, 3], "a and c") == 4
    assert file_lines(store) == ["#2 2026-01-02 b", "#4 2026-03-04 a and c"]
    assert next_id(store) == "5"
    assert log(store)[commits:] == ["merge #1,#3 -> #4"]


def test_wake_groups_by_first_tag(store: Path):
    for t in (
        "#git push with care",  # 1
        "#nix rebuild first",  # 2
        "no tag here",  # 3
        "#git rebase #nix too",  # 4: first tag decides
        "a#b is not a tag, #nix is",  # 5
        "corrects #3 only",  # 6: a number is not a tag
    ):
        rules.note(store, t)
    size = (store / "RULES.txt").stat().st_size
    assert rules.wake(store).splitlines() == [
        "## git",
        "#1 2026-01-02 #git push with care",
        "#4 2026-01-02 #git rebase #nix too",
        "## nix",
        "#2 2026-01-02 #nix rebuild first",
        "#5 2026-01-02 a#b is not a tag, #nix is",
        "## (untagged)",
        "#3 2026-01-02 no tag here",
        "#6 2026-01-02 corrects #3 only",
        f"6 rules, {size} bytes.",
    ]


def test_group_order_follows_earliest_id_after_a_merge(store: Path):
    rules.note(store, "#b one")  # 1
    rules.note(store, "#a two")  # 2
    rules.merge(store, [1, 2], "#b merged")  # 3, appended after nothing
    rules.note(store, "#a four")  # 4
    out = rules.wake(store).splitlines()
    assert [ln for ln in out if ln.startswith("##")] == ["## b", "## a"]


def test_wake_of_an_empty_store(store: Path):
    assert rules.wake(store) == "0 rules, 0 bytes.\n"


def test_wake_over_budget(store: Path):
    lines = [f"#{i} 2026-01-02 rule {i}" for i in range(1, 121)]
    (store / "RULES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert rules.wake(store).splitlines()[-1].startswith("120 rules, ")
    rules.note(store, "one too many")
    out = rules.wake(store).splitlines()
    assert out[-2].startswith("121 rules, ")
    assert out[-1] == rules.CURATE


def test_wake_over_budget_by_bytes(store: Path):
    for i in range(100):
        rules.note(store, f"{i:03d} " + "x" * 250)
    out = rules.wake(store).splitlines()
    assert out[-2].startswith("100 rules, ")
    assert out[-1] == rules.CURATE


def test_recall(store: Path):
    # The file is not in id order (a hand edit); recall still answers in id order.
    (store / "RULES.txt").write_text(
        "#4 2026-01-02 the KEYCHAIN rule, merged\n#3 2026-01-02 keychain again\n",
        encoding="utf-8",
    )
    assert rules.recall(store, "keychain") == (
        "#3 2026-01-02 keychain again\n#4 2026-01-02 the KEYCHAIN rule, merged\n"
    )
    assert rules.recall(store, "nothing matches") == ""
    with pytest.raises(RulesError) as e:
        rules.recall(store, "(unclosed")
    assert e.value.code == 2


def test_recall_matches_text_not_the_date(store: Path):
    rules.note(store, "plain")
    assert rules.recall(store, "2026") == ""


def test_blank_and_comment_lines_are_not_rules(store: Path):
    (store / "RULES.txt").write_text(
        "// the rules\n\n#1 2026-01-02 #x one\n   \n// #2 2026-01-02 not a rule\n",
        encoding="utf-8",
    )
    (store / "NEXT").write_text("2\n", encoding="utf-8")
    assert rules.wake(store).splitlines()[:-1] == ["## x", "#1 2026-01-02 #x one"]
    assert rules.recall(store, "rule") == ""
    assert rules.note(store, "two") == 2
    assert rules.recall(store, ".") == "#1 2026-01-02 #x one\n#2 2026-01-02 two\n"


def test_uninitialised_dir_is_exit_3(tmp_path: Path):
    for argv in (["wake"], ["recall", "x"], ["note", "x"], ["drop", "1"]):
        code, _, err = rules.run(argv, tmp_path / "nothing")
        assert code == 3, argv
        assert "init" in err
    assert not (tmp_path / "nothing").exists()


def test_run_reports_user_errors(store: Path):
    assert rules.run(["frobnicate"], store)[0] == 1
    assert rules.run([], store)[0] == 1
    assert rules.run(["note"], store)[0] == 1
    assert rules.run(["edit", "x", "t"], store)[0] == 1
    assert rules.run(["merge", "1", "t"], store)[0] == 1
    assert rules.run(["recall", "["], store)[0] == 2


def test_run_verbs(store: Path):
    assert rules.run(["note", "a"], store) == (0, "Saved as #1.\n", "")
    assert rules.run(["note", "b"], store) == (0, "Saved as #2.\n", "")
    assert rules.run(["edit", "#1", "a2"], store) == (0, "Edited #1.\n", "")
    assert rules.run(["merge", "1,#2", "ab"], store) == (0, "Saved as #3.\n", "")
    assert rules.run(["drop", "3", "why"], store) == (0, "Dropped #3.\n", "")
    assert rules.run(["drop", "3"], store) == (1, "", "no rule #3\n")


def test_cli_smoke(tmp_path: Path):
    env = {
        "MEMORY_DIR": str(tmp_path / "store"),
        "MEMO_DATE": "2026-01-02",
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
    }

    def cli(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RULES_PY), *argv],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    assert cli("wake").returncode == 3
    assert cli("init").returncode == 0
    p = cli("note", "#cli hello")
    assert (p.returncode, p.stdout) == (0, "Saved as #1.\n")
    p = cli("wake")
    assert p.returncode == 0
    assert p.stdout.splitlines()[:2] == ["## cli", "#1 2026-01-02 #cli hello"]
    p = cli("note", "")
    assert p.returncode == 1 and p.stderr
