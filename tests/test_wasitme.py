import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT
from wasitme.core import failures, handle, looks_like_tests

CLI = [sys.executable, str(ROOT / "bin" / "wasitme")]
# A tiny "test runner": each tests/*.ok file is a test; content "fail" makes it fail. Prints pytest-style lines.
RUNNER = """import pathlib, sys
bad = [p.stem for p in sorted(pathlib.Path('tests').glob('*.ok')) if p.read_text().strip() == 'fail']
for b in bad: print(f'FAILED tests/{b}.py::test_{b} - assert False')
sys.exit(1 if bad else 0)
"""


def sh(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(project):
    sh(project, "init", "-q")
    (project / "run.py").write_text(RUNNER)
    (project / "tests").mkdir()
    (project / "tests" / "a.ok").write_text("pass")
    (project / "tests" / "b.ok").write_text("fail")   # broken before we arrive
    (project / "tests" / "c.ok").write_text("fail")   # also broken, will be fixed
    (project / "node_modules").mkdir()               # untracked dependency dir
    (project / ".gitignore").write_text("node_modules/\n")
    (project / "node_modules" / "dep.txt").write_text("x")
    sh(project, "add", ".")
    sh(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return project


def cli(repo, *args, home):
    env = dict(os.environ, HOME=str(home))
    return subprocess.run(CLI + list(args), cwd=repo, capture_output=True, text=True, env=env, timeout=120)


def test_parsers():
    out = "FAILED t/a.py::test_x - boom\n--- FAIL: TestY (0.01s)\ntest m::z ... FAILED\n  ✕ adds numbers (5 ms)\n"
    assert failures(out) == ["t/a.py::test_x", "TestY", "m::z", "adds numbers"]
    assert looks_like_tests("cd web && pnpm test -- --run") and looks_like_tests("python -m pytest -x")
    assert looks_like_tests("go test ./...") and not looks_like_tests("ls tests/")


def test_classifies_introduced_preexisting_fixed(repo, isolated):
    (repo / "tests" / "a.ok").write_text("fail")   # we break a
    (repo / "tests" / "c.ok").write_text("pass")   # we fix c
    r = cli(repo, "--json", "--", f"{sys.executable} run.py", home=isolated)
    data = json.loads(r.stdout)
    assert data["introduced"] == ["tests/a.py::test_a"]
    assert data["pre_existing"] == ["tests/b.py::test_b"]
    assert data["fixed"] == ["tests/c.py::test_c"]
    assert data["base"]["cached"] is False
    assert sh(repo, "worktree", "list").count("\n") == 1          # temp worktree cleaned up
    r2 = cli(repo, "--", f"{sys.executable} run.py", home=isolated)
    assert "[cached]" in r2.stdout and "INTRODUCED" in r2.stdout and "tests/a.py::test_a" in r2.stdout


def test_dependencies_are_shared(repo, isolated):
    (repo / "tests" / "a.ok").write_text("fail")
    check = f"{sys.executable} -c \"import pathlib,sys; sys.exit(0 if pathlib.Path('node_modules/dep.txt').exists() else 3)\""
    r = cli(repo, "--json", "--", check, home=isolated)
    assert json.loads(r.stdout)["base"]["exit"] == 0


def test_clean_tree_refuses(repo, isolated):
    r = cli(repo, "--", "true", home=isolated)
    assert r.returncode == 2 and "no uncommitted changes" in r.stdout


def test_hook_hints_once_then_uses_cache(repo, isolated):
    (repo / "tests" / "a.ok").write_text("fail")
    cmd = f"{sys.executable} run.py"
    ev = {"hook_event_name": "PostToolUseFailure", "cwd": str(repo), "session_id": "s", "tool_name": "Bash",
          "tool_input": {"command": cmd + " # pytest"}, "error": "Exit code 1\nFAILED tests/a.py::test_a - x"}
    text = handle(ev)["hookSpecificOutput"]["additionalContext"]
    assert "wasitme -- " in text
    assert handle(ev) is None                                      # once per session
    cli(repo, "--", cmd + " # pytest", home=isolated)              # populate the cache
    ev["error"] = "Exit code 1\nFAILED tests/a.py::test_a - x\nFAILED tests/b.py::test_b - y"
    text = handle(ev)["hookSpecificOutput"]["additionalContext"]
    assert "pre-existing): tests/b.py::test_b" in text and "uncommitted changes: tests/a.py::test_a" in text


def test_hook_ignores_non_tests_and_clean_trees(repo):
    ev = {"hook_event_name": "PostToolUseFailure", "cwd": str(repo), "session_id": "s", "tool_name": "Bash",
          "tool_input": {"command": "pytest"}, "error": "Exit code 1\nFAILED x"}
    assert handle(ev) is None  # clean tree
    (repo / "run.py").write_text("x")
    ev["tool_input"] = {"command": "ls nope"}
    assert handle(ev) is None
