"""Repo-confined write/edit/shell tools (2026-06-29).

The one hard limit Rob asked for: agents can act, but CANNOT edit files outside
this repo or touch other repos. write_file/edit_file enforce this airtight;
run_shell rejects out-of-repo path references (best-effort — no kernel jail here).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.research.agent_tools import write_file, edit_file, run_shell, _confine_to_repo


def test_write_inside_repo_ok(tmp_path):
    rel = "outputs/_unit_confine_test.txt"
    r = write_file(path=rel, content="hello")
    assert r["ok"] is True
    p = REPO_ROOT / rel
    assert p.read_text() == "hello"
    p.unlink()


def test_write_other_repo_refused():
    r = write_file(path="/home/rob/scout/HACKED.txt", content="x")
    assert r["ok"] is False and "outside the repo" in r["error"]
    assert not Path("/home/rob/scout/HACKED.txt").exists()


def test_write_parent_traversal_refused():
    r = write_file(path="../../etc/passwd_test", content="x")
    assert r["ok"] is False


def test_write_absolute_system_path_refused():
    r = write_file(path="/etc/cron.d/evil", content="x")
    assert r["ok"] is False


def test_edit_confined_and_unique(tmp_path):
    rel = "outputs/_unit_edit_test.txt"
    write_file(path=rel, content="alpha beta gamma")
    assert edit_file(path=rel, old="beta", new="DELTA")["ok"] is True
    assert (REPO_ROOT / rel).read_text() == "alpha DELTA gamma"
    # non-unique refused
    write_file(path=rel, content="x x x")
    assert edit_file(path=rel, old="x", new="y")["ok"] is False
    # outside repo refused
    assert edit_file(path="/home/rob/scout/foo", old="a", new="b")["ok"] is False
    (REPO_ROOT / rel).unlink()


def test_run_shell_in_repo_ok():
    r = run_shell(command="echo confined && pwd")
    assert r["ok"] is True
    assert str(REPO_ROOT) in r["stdout"]  # cwd is the repo


def test_run_shell_other_repo_refused():
    r = run_shell(command="echo x > /home/rob/scout/x.txt")
    assert r["ok"] is False and "refused" in r["error"]


def test_run_shell_traversal_refused():
    assert run_shell(command="cat ../../etc/passwd")["ok"] is False


def test_confine_helper_rejects_escape():
    with pytest.raises(ValueError):
        _confine_to_repo("/home/rob/other-repo/x")
    # in-repo resolves fine
    assert _confine_to_repo("src/config.py") == REPO_ROOT / "src" / "config.py"
