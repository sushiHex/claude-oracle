# Repository Guidelines

## Project Structure & Module Organization

- `src/claude_oracle/sdk.py` contains `OracleSDK`, scout orchestration, synthesis, and CLI handling.
- `install.py` installs the `/oracle` skill; `__main__.py` supports module execution; `data/SKILL.md` is the packaged skill definition.
- `tests/test_oracle.py` holds unit and regression tests. `docs/architecture.svg` illustrates the architecture.
- `pyproject.toml` defines dependencies, entry points, and packaging; `.github/workflows/ci.yml` defines CI. See `CLAUDE.md` for additional maintenance conventions.

## Build, Test, and Development Commands

Use Python 3.10+ and a virtual environment.

- `python -m pip install -e ".[test]"`: install editable source and pytest.
- `python -m pytest tests/ -q`: run the offline test suite.
- `claude-oracle --help` and `python -m claude_oracle --help`: smoke-test both entry points.
- `python -m claude_oracle "your research question"`: run live research; requires authenticated Claude Code and consumes subscription usage.
- `python -m claude_oracle.install`: refresh the installed skill and package-delegating shim under `~/.claude/skills/oracle/`.
- `python -m pip wheel --no-deps --wheel-dir dist .`: build a package wheel.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions and variables, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Follow existing type hints and docstrings. Reuse model constants and `MODEL_WEIGHTS`. No formatter or linter is configured.

For releases, synchronize versions in `pyproject.toml`, `__init__.py`, the `sdk.py` docstring and banner, and the packaged skill header; refresh the installed skill afterward.

## Testing Guidelines

Use pytest with `test_*.py` files and descriptive `test_<behavior>` functions. Mock `sdk.query` with fake async generators; drive coroutines with `asyncio.run`. Preserve credential isolation through `monkeypatch` and `tmp_path`; tests must avoid real credentials and network calls. Add regression coverage for changed failure paths, launch gating, cleanup, and chain isolation. No numeric coverage threshold is configured. CI runs Python 3.10 and 3.14 on Ubuntu and Windows.

## Commit & Pull Request Guidelines

Develop code and documentation in `sushiHex/claude-oracle`; target PRs at `main` on `origin`. Use `sushiHex/claude-oracle-private` only for private data, never as the development upstream. Preserve the public repository's Git history.

Use concise, action-oriented commit subjects; prefixes such as `docs:`, `CI:`, or a release version are welcome. Describe the behavior change, link relevant issues, and report validation in PRs. Update documentation when CLI behavior changes and ensure CI passes.

## Security & Configuration

Keep local scout tools opt-in via `--local`, preserve per-chain isolation and untruncated scout output, and retain the pinned GitHub MCP version. Never commit tokens or copy refresh credentials. Keep `research/` private and excluded from public commits; this includes the raw scout evidence round sessions save under each attempt's `scouts/` directory. Organizer-only recovery (`--recover`, `RoundSession.recover_organizers`) must never launch scouts or the Architect, mix chains, rewrite `canonical.md`, or change the round budget.
