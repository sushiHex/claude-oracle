# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Claude Oracle is a pip-installable research orchestrator for Claude Code. Haiku scouts, Sonnet organization, and caller-managed adaptive rounds.

## Stack
- Python 3.10+, claude_agent_sdk >= 0.1.48
- Node.js (npx for GitHub MCP)
- Development repository: `github.com/sushiHex/claude-oracle` (public). `github.com/sushiHex/claude-oracle-private` is reserved for private data.

## Structure
```
src/claude_oracle/
  sdk.py        — the engine (OracleSDK class) + CLI argument parsing
  rounds.py     — durable round sessions; caller owns planning and canonical.md
  install.py    — claude-oracle-install command
  __main__.py   — python -m claude_oracle
  data/SKILL.md — /oracle skill definition
tests/          — offline unit + regression tests (no network, no real credentials)
docs/           — agent-usage.md, configuration.md, architecture/rounds SVGs
pyproject.toml  — pip metadata, entry points
AGENTS.md       — the same conventions for non-Claude agents; keep it in sync
```

## Development

```sh
python -m pip install -e ".[test]"        # editable install + pytest
python -m pytest tests/ -q                # full offline suite (~74 tests, ~2s)
python -m pytest tests/test_rounds.py -q -k test_checkpoint   # a single test
python -m claude_oracle --help            # entry-point smoke check
claude-oracle --help                      # console-script smoke check
```

- No formatter or linter is configured. Match the surrounding style: four-space indent, `snake_case`, `UPPER_SNAKE_CASE` constants, existing type hints.
- CI (`.github/workflows/ci.yml`) runs on public `main` pushes and pull requests: fresh-environment install, unit tests, and entry-point smoke checks on Python 3.10 and 3.14 × Ubuntu and Windows. `main` requires all four contexts and is `strict` (branch must be current with `main`).
- **Fork PRs need a maintainer to approve the workflow run** before CI reports; until then the PR shows `BLOCKED` with no checks.
- Tests are hermetic by construction: `tests/test_oracle.py` has an autouse `_hermetic_auth` fixture that strips oracle tokens and repoints `_credentials_file` at a tmp path; `tests/test_rounds.py` monkeypatches `sdk.query` to fail the test if a model is contacted. Mock `sdk.query` with a fake async generator and drive coroutines with `asyncio.run` — there is no pytest-asyncio dependency.
- **`src/` layout gotcha:** pytest run from a second checkout or a git worktree still imports the editable install from the primary repo. Verify another tree with `PYTHONPATH=<tree>/src python -m pytest tests/ -q`, or you will "reproduce" failures that are only import shadowing.

## Architecture

One round is three phases inside `OracleSDK.run()`, and the caller — not Oracle — owns judgment:

1. **Plan.** Prompts arrive as a JSON array on **stdin** (`_normalize_prompts` validates and stamps `chain`/`id`/`dimension` + `SCOUT_SUFFIX`). Only when stdin is a TTY does the built-in Sonnet **Architect** (`decompose`) generate them. The calling Claude session is the preferred planner because it has conversation context the Architect never sees.
2. **Scout.** `scout()` dispatches N×10 Haiku **Smiths** concurrently, each its own `claude` subprocess. Web-only tools by default.
3. **Organize.** `compress()` runs one Sonnet **Anderson** per chain. **Each Anderson sees only its own chain's Smith reports** — chain isolation is enforced in Python, not by prompting. There is no cross-chain merger and no Opus phase; every chain briefing is returned to the caller verbatim.

`rounds.py` wraps that in a durable session so multi-round research survives process exits. `RoundSession.run_round()` executes exactly **one** round and returns; the caller reads the findings, writes the next plan, and resumes. Session directory layout:

```
research/oracle-<id>/
  session.json    — schema-versioned state, atomically replaced (_atomic_write)
  canonical.md    — the caller's report; Oracle creates it and never rewrites it
  .round.lock     — non-blocking OS lock: one round writer per session
  .session.lock   — blocking OS lock: serializes session.json read-modify-writes
  rounds/round-NNN-attempt-NNN/{prompts.json, report.md, metrics.json}
```

Failed rounds are retained as attempts and do **not** consume a round from the budget; resuming retries the same round number in a new attempt directory. `.round.lock` deliberately does not cover `canonical.md`, because the caller is expected to revise it while the next round runs.

**Two locks, two jobs.** `.round.lock` is non-blocking and admits one round writer. `.session.lock` is blocking and guards every `session.json` read-modify-write, because `checkpoint()` is called from outside the round — concurrently with a running round, by design. `_persist_state()` re-reads the latest `checkpoint` before each terminal write so a mid-round checkpoint is never clobbered, and `checkpoint()` validates against `completed_rounds` read inside the lock so it can never revert a finished round. Anything new that writes `session.json` must go through `_persist_state()` or take `_state_lock` itself.

`status()` is the machine-readable handoff: lifecycle (`status`) is separate from `research_outcome` (`unknown`/`complete`/`partial`/`failed`), alongside `current_phase`, live `progress`, `active_attempt`, `artifact_paths` (relative, accumulated), `reported_usage`, and the caller-owned `checkpoint`. Status reads never block and never start models.

## Conventions
- Refresh the installed skill with `python -m claude_oracle.install`; `~/.claude/skills/oracle/oracle_sdk.py` is a launcher that delegates to the installed package.
- **A release bumps exactly two strings**: `__version__` in `src/claude_oracle/sdk.py` and the
  `# Oracle vX.Y.Z` header in `data/SKILL.md`. Everything else derives — `pyproject.toml` reads
  the constant through setuptools' dynamic `attr =`, `__init__.py` re-exports it, and the CLI
  banner formats it. Then run `python -m claude_oracle.install` so the installed skill matches.
  The packaged `SKILL.md` is what external users get: it went stale at v4.3.1, and again across
  the two feature PRs that became v4.8.0, which is why the other strings were collapsed away.

## Gotchas (project-specific)
- HTTP MCP transport broken in SDK v0.1.48 — stdio only
- Smiths: no truncation — `_build_scout_data` sends full Smith outputs (Anderson runs on Sonnet's large context)
- Concurrent Claude subprocess startups race on the shared `~/.claude.json` and can corrupt it (upstream bug, reproduced on CLI 2.1.207). Defense: per-subprocess `CLAUDE_CONFIG_DIR` isolation whenever a token is available (`_oauth_token()` priority: ORACLE_OAUTH_TOKEN > CLAUDE_CODE_OAUTH_TOKEN > the session's own access token from `.credentials.json` when >1h remains — access token ONLY, never the refresh token, so children can't rotate credentials); otherwise a startup launch gate (`STARTUP_GATE_TIMEOUT_S`) plus a one-shot retry pass. Full run-parallelism in both modes. (The old `SCOUT_LAUNCH_STAGGER_S` fixed stagger is gone.)
- Scouts are web-only by default; `--local` (CLI) / `local_tools=True` (API) grants Read/Grep/Glob — keep it opt-in, it's a prompt-injection exfiltration surface when combined with web access. Note the Architect and Anderson hold local read tools regardless; `--local` is not a whole-pipeline sandbox.
- GitHub MCP npx package is version-pinned in `_github_mcp()` — bump deliberately, never float to latest
- `_normalize_prompts` warns if prompt count isn't a multiple of 10. Unlabeled batches of 11–19 all land in chain A; uneven chains require explicit `chain` labels and unique IDs.
- Don't hardcode model names — use MODEL_WEIGHTS dict
- **`effort="low"` on the Architect and Anderson is a benchmarked result, not an oversight.** `high` emitted 3.1x the tokens for identical Anderson output (marker recall, contradiction detection, stale-vs-fresh rejection all 100% at every level). Anderson is ~50% of run spend. Same for Smiths' `thinking: disabled` (-26% output tokens, citation breadth unchanged). Do not "fix" these upward without re-running the benchmark.
- `max_turns: 30` on Smiths is a runaway guard; observed healthy range is 9–23 turns. **Do not lower below 25.**
- An empty Smith result is an **error**, not a silent success — otherwise it passes the all-failed guard and contributes nothing downstream.
- Preserve the recovery ladder: a failed Anderson stashes `fallback_scouts` so the report emits raw Smith output verbatim and recovery is "re-run Anderson", not "re-run 10 Smiths". The retry pass is skipped when more than half the Smiths failed (that pattern is systemic — auth or network — and a serial retry just repeats it slowly).
- Quota percentages are estimates from a reverse-engineered credit model, not account balances. `--usd` is SDK-reported usage, not an invoice. Cached prompt tokens are reported separately from `input_tokens` and must be counted **at their own rates** — `CACHE_READ_MULTIPLIER` (0.1) and `CACHE_WRITE_MULTIPLIER` (1.25), not 1.0. Anderson turns are cache-dominated (a real one showed `input_tokens=3` against `cache_read_input_tokens=3,040`), so counting cache reads at zero understates badly and counting them at full price overstates the dominant term.
- `UsageStats` distinguishes `usage_observed` (an attempt happened) from `usage_available` (the provider returned numbers). `add()` is monotone on availability: one attempt without usage data makes the aggregate unavailable, and `_record_usage` then blanks the session's cumulative total rather than reporting a fabricated figure. Be aware this is all-or-nothing per session.
- Timeouts are empirical: `SCOUT_TIMEOUT_S=720`, `ANDERSON_TIMEOUT_S=1200`. Both were raised after real production timeouts.

## Repository workflow
- All code, documentation, issues, and pull requests belong in `sushiHex/claude-oracle`. Develop on feature branches and target the public repository's `main` branch.
- `origin` must point to `https://github.com/sushiHex/claude-oracle.git`. Confirm the repository and base branch before opening or merging a PR.
- Use `sushiHex/claude-oracle-private` only to preserve private reports, research, and other private data. It is not a development upstream or release source.
- **Private by policy, never published**: `research/` and Oracle report outputs. Keep these gitignored in the public checkout; preserve selected private artifacts in the private repository separately. Note `RoundSession.create()` defaults its session directory to `research/`, so live sessions are gitignored by default.
- This extends to **benchmark figures in code comments and PR descriptions**. Publish the conclusion and a reproducible method ("sweep low/medium/high over a full-size chain, score marker recall against tokens spent"), not the private run's numbers. Keep the measurements themselves in `research/` or the private repository.
- Preserve public Git history. Do not merge private history into the public repository or replace public `main` with an orphan snapshot. When recovering misplaced work, transfer only the reviewed source/documentation files onto a branch based on public `main`.
- `scripts/publish_mirror.sh` is retired and exits without changing Git state. Use the normal public PR workflow.
