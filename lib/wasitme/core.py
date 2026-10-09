"""Run a command on the base commit in a temporary worktree and compare failures with the current tree."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import hookio

PLUGIN = "wasitme"

DEFAULTS = {
    "link": ["node_modules", ".venv", "venv", ".env", ".env.local"],  # shared into the worktree if untracked
    "nested_node_modules_depth": 3,
    "timeout_s": 900,
    "hint_once_per_session": True,
}

TEST_CMD_RE = re.compile(
    r"(?:^|[\s/])(pytest|py\.test|jest|vitest|mocha|ava|rspec|phpunit|tox|nox|ctest|"
    r"go\s+test|cargo\s+(?:test|nextest)|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test(?::\S+)?|"
    r"make\s+(?:test|check)\S*|(?:\./)?gradlew?\s+\S*test|mvn\s+\S*test|dotnet\s+test|mix\s+test|"
    r"python3?\s+-m\s+(?:pytest|unittest)|swift\s+test|deno\s+test|bundle\s+exec\s+rspec)\b")

# failure-id parsers: (regex, group) applied line by line
FAIL_PATTERNS = [
    re.compile(r"^FAILED\s+(\S+?)(?:\s+-\s+.*)?$"),                         # pytest -ra / summary
    re.compile(r"^ERROR\s+(\S+::\S+)"),                                   # pytest collection/setup errors
    re.compile(r"^\s*--- FAIL: (\S+)"),                                   # go test
    re.compile(r"^test (\S+) \.\.\. FAILED$"),                            # cargo test
    re.compile(r"^\s*(?:✕|×|✗)\s+(.+?)(?:\s+\(\d+\s*m?s\))?$"),           # jest / vitest
    re.compile(r"^\s*●\s+(.+?)\s*$"),                                      # jest failure headers
    re.compile(r"^rspec\s+(\./\S+:\d+)"),                                  # rspec rerun lines
    re.compile(r"^\s*\d+\)\s+(\S.+?)$"),                                   # mocha / phpunit numbered
    re.compile(r"^(?:FAIL|ERROR):\s+(\S+\s+\(\S+\))"),                    # unittest
]


# ---------------------------------------------------------------- helpers

def git(root: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120, errors="replace")
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:400]}")
    return r.stdout


def failures(output: str) -> list[str]:
    out = []
    for line in output.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*m", "", line).rstrip()
        for rx in FAIL_PATTERNS:
            m = rx.match(line)
            if m:
                fid = re.sub(r"\s+\(\d+(\.\d+)?\s*m?s\)$", "", m.group(1).strip())
                if fid and fid not in out:
                    out.append(fid)
                break
    return out


def looks_like_tests(command: str) -> bool:
    return bool(TEST_CMD_RE.search(command))


def cache_path(base_sha: str, command: str, rel_cwd: str) -> Path:
    key = hashlib.sha1(f"{base_sha}\n{rel_cwd}\n{command}".encode()).hexdigest()[:16]
    d = hookio.data_dir(PLUGIN) / "baselines"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def run_shell(command: str, cwd: Path, timeout: int) -> dict:
    t0 = time.time()
    try:
        r = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           errors="replace", env=dict(os.environ, CI="1", FORCE_COLOR="0", NO_COLOR="1"))
        code, out = r.returncode, r.stdout + "\n" + r.stderr
    except subprocess.TimeoutExpired as e:
        code = 124
        out = f"{e.stdout or ''}\n{e.stderr or ''}\n[wasitme: timed out after {timeout}s]"
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
    return {"exit": code, "failures": failures(out), "tail": out[-4000:], "seconds": round(time.time() - t0, 1)}


def link_dependencies(root: Path, wt: Path, cfg: dict) -> list[str]:
    linked = []
    names = list(cfg.get("link") or [])
    for name in names:
        src, dst = root / name, wt / name
        if src.exists() and not dst.exists():
            dst.symlink_to(src, target_is_directory=src.is_dir())
            linked.append(name)
    depth = int(cfg.get("nested_node_modules_depth", 0))
    if depth and (root / "package.json").exists():
        for dirpath, dirnames, _ in os.walk(root):
            rel = Path(dirpath).relative_to(root)
            if len(rel.parts) >= depth or ".git" in rel.parts:
                dirnames[:] = []
                continue
            if "node_modules" in dirnames and rel.parts:
                src, dst = Path(dirpath) / "node_modules", wt / rel / "node_modules"
                if dst.parent.exists() and not dst.exists():
                    dst.symlink_to(src, target_is_directory=True)
                    linked.append(str(rel / "node_modules"))
            dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git", ".venv", "venv")]
    return linked


def resolve_base(root: Path, base: Optional[str]) -> tuple[str, str]:
    """(sha, label). `--base main` means the merge-base of main and HEAD."""
    if not base or base == "HEAD":
        return git(root, "rev-parse", "HEAD").strip(), "HEAD"
    if re.fullmatch(r"[0-9a-f]{7,40}", base) or "~" in base or "^" in base:
        return git(root, "rev-parse", base).strip(), base
    mb = git(root, "merge-base", base, "HEAD", check=False).strip()
    return (mb, f"merge-base({base}, HEAD)") if mb else (git(root, "rev-parse", base).strip(), base)


def baseline(root: Path, sha: str, command: str, rel_cwd: str, cfg: dict, fresh: bool = False) -> dict:
    cp = cache_path(sha, command, rel_cwd)
    if cp.exists() and not fresh:
        data = json.loads(cp.read_text())
        data["cached"] = True
        return data
    tmp = Path(tempfile.mkdtemp(prefix="wasitme-"))
    wt = tmp / "wt"
    git(root, "worktree", "add", "--detach", "--quiet", str(wt), sha)
    try:
        linked = link_dependencies(root, wt, cfg)
        result = run_shell(command, wt / rel_cwd, int(cfg["timeout_s"]))
        result["linked"] = linked
    finally:
        git(root, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(tmp, ignore_errors=True)
        git(root, "worktree", "prune", check=False)
    cp.write_text(json.dumps(result))
    result["cached"] = False
    return result


def compare(base: dict, cur: dict) -> dict:
    b, c = set(base["failures"]), set(cur["failures"])
    return {"introduced": sorted(c - b), "pre_existing": sorted(c & b), "fixed": sorted(b - c)}


def report(cmd: str, label: str, sha: str, base: dict, cur: dict) -> str:
    cmp = compare(base, cur)
    lines = [f"wasitme: `{cmd}`",
             f"- base {label} ({sha[:9]}): exit {base['exit']}, {len(base['failures'])} failure(s)"
             f"{' [cached]' if base.get('cached') else ''}",
             f"- current tree: exit {cur['exit']}, {len(cur['failures'])} failure(s)"]
    if not base["failures"] and not cur["failures"]:
        if base["exit"] != 0 and cur["exit"] != 0:
            verdict = "both fail with no parseable test ids, so the failure most likely predates the change"
        elif cur["exit"] != 0:
            verdict = "the base passes and the current tree fails, so the change introduced the failure"
        elif base["exit"] != 0:
            verdict = "the base fails and the current tree passes, so the change fixed it"
        else:
            verdict = "both pass"
        lines.append(f"- verdict: {verdict}")
    else:
        for key, title in (("introduced", "INTRODUCED (fail now, passed on base)"),
                           ("pre_existing", "PRE-EXISTING (fail on base too)"),
                           ("fixed", "FIXED (failed on base, pass now)")):
            if cmp[key]:
                lines.append(f"- {title}: " + ", ".join(cmp[key][:30]) + (" …" if len(cmp[key]) > 30 else ""))
    if base.get("linked"):
        lines.append(f"- shared into the worktree: {', '.join(base['linked'])}")
    if base["exit"] not in (0, 1) and not base["failures"]:
        lines.append("- base output tail:\n" + "\n".join("    " + l for l in base["tail"].splitlines()[-8:]))
    return "\n".join(lines)


# ---------------------------------------------------------------- hook

def handle(event: dict) -> Optional[dict]:
    if event.get("hook_event_name") != "PostToolUseFailure" or not hookio.is_bash_event(event):
        return None
    run = hookio.BashRun(event)
    if not run.command or run.interrupted or not looks_like_tests(run.command):
        return None
    project = hookio.project_dir(event)
    cfg = hookio.load_config(PLUGIN, project, DEFAULTS)
    cwd = Path(event.get("cwd") or project)
    try:
        root = Path(git(cwd, "rev-parse", "--show-toplevel").strip())
        dirty = bool(git(root, "status", "--porcelain", "--untracked-files=no").strip())
        sha = git(root, "rev-parse", "HEAD").strip()
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return None
    if not dirty:
        return None  # HEAD is the current tree: nothing to compare against
    rel_cwd = os.path.relpath(cwd, root)
    cp = cache_path(sha, run.command, rel_cwd)
    if cp.exists():
        base = json.loads(cp.read_text())
        base["cached"] = True
        cur = {"exit": run.exit_code, "failures": failures(run.output)}
        cmp = compare(base, cur)
        bits = [f"wasitme: a cached run of this command on HEAD ({sha[:9]}) exited {base['exit']}."]
        if cmp["pre_existing"]:
            bits.append("Also failing on HEAD (pre-existing): " + ", ".join(cmp["pre_existing"][:15]) + ".")
        if cmp["introduced"]:
            bits.append("Failing only with the uncommitted changes: " + ", ".join(cmp["introduced"][:15]) + ".")
        return hookio.context("PostToolUseFailure", " ".join(bits))
    if cfg.get("hint_once_per_session", True):
        marker = hookio.data_dir(PLUGIN) / "hinted" / re.sub(r"[^\w-]", "_", event.get("session_id") or "x")
        marker.parent.mkdir(parents=True, exist_ok=True)
        if marker.exists():
            return None
        marker.write_text("1")
    quoted = run.command.replace("'", "'\\''")
    return hookio.context(
        "PostToolUseFailure",
        "wasitme: this test command failed with uncommitted changes in the tree. "
        f"`wasitme -- '{quoted}'` re-runs it on HEAD in a temporary git worktree (dependencies such as "
        "node_modules/.venv are shared) and lists which failures are introduced versus pre-existing.")
