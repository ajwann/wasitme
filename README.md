# wasitme

"Did my change break this, or was it already broken?" `wasitme` runs a test command twice: once on
your working tree and once on the base commit, in a temporary `git worktree`. Untracked dependency
directories (`node_modules`, including nested ones, `.venv`, `.env`) are symlinked in so the base run
works without a reinstall. It then sorts every failing test:

```
$ wasitme -- pytest -q
wasitme: `pytest -q`
- base HEAD (3c1d9e2aa): exit 1, 2 failure(s)
- current tree: exit 1, 2 failure(s)
- INTRODUCED (fail now, passed on base): tests/test_api.py::test_create
- PRE-EXISTING (fail on base too): tests/test_slow.py::test_timeout
- FIXED (failed on base, pass now): tests/test_util.py::test_parse
```

It parses failures from pytest, go test, cargo test, jest/vitest, rspec, mocha/phpunit and unittest, and
falls back to exit codes. Base results are cached per commit, so a re-check only reruns the current tree.
Use `--base main` to compare with the merge-base, `--fresh` to ignore the cache, and `--json` for
machine-readable output.

**Hook:** `PostToolUseFailure(Bash)`. When a test command fails with uncommitted changes, Claude is told
that `wasitme` exists (once per session). If a cached base run exists, it is told straight away which
failures predate the change.

## Install

From Anthropic's plugin directory, once listed: search for `wasitme` in `/plugin directory` in Claude Code, or under **Customize > Plugins** on claude.ai.

To try it from a clone for one session: `claude --plugin-dir /path/to/wasitme`.

This plugin is for Claude Code: it is built on hooks and a command-line tool in `bin/`, which claude.ai chat does not load.

## Configuration

Settings are read from `~/.claude/wasitme.json` and then `<project>/.claude/wasitme.json`. The project file wins, objects merge by key, and a broken file is skipped and logged.

## What it runs and stores

It is Python 3.9+ standard library only, makes no network requests, and sends nothing anywhere. Every hook fails open: an internal error is logged to `~/.claude/wasitme/errors.log` and the hook exits 0.

- `wasitme` creates a temporary `git worktree` of the base commit, symlinks untracked dependency directories into it, runs your test command there, and removes the worktree.
- Caches base-commit results in `~/.claude/wasitme/baselines/` and per-session hint markers in `~/.claude/wasitme/hinted/`.

## Development

```
python3 -m pytest -q
claude plugin validate --strict .
```

`docs/HOOK_SCHEMA.md` records the hook payloads this plugin depends on.

## License

MIT. See `LICENSE`.
