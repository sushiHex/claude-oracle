# Configuration and access boundaries

[← README](../README.md)

Oracle normally uses an existing Claude Code login. No Oracle-specific environment variables are required for the standard workflow.

## Optional GitHub tools

Set `GITHUB_PAT` in the environment that launches Oracle to enable GitHub MCP. Node.js and `npx` must be available. The integration uses the pinned `@modelcontextprotocol/server-github@2025.4.8` package over stdio.

Scouts are instructed to prefer GitHub tools for repository search, file contents, issues, and commits. Without `GITHUB_PAT`, Oracle does not start this MCP server; ordinary web research can still visit GitHub.

Use your OS environment-variable settings or a secrets manager to provide the token. For public research, avoid granting private-repository access. The integration allows the server's `mcp__github__*` tool set, so do not assume it enforces read-only GitHub operations; the token's permissions matter. Oracle does not load a `.env` file itself. Restart the launching application after changing persistent environment settings.

## Authentication and concurrent startup

Oracle launches multiple Claude subprocesses. To reduce races in their shared startup configuration, it chooses between two modes automatically.

| Mode | Selected when | Behavior |
| --- | --- | --- |
| Isolated configuration | An explicit OAuth token is available, or the stored session access token has more than one hour remaining | Each scout and organizer gets its own temporary `CLAUDE_CONFIG_DIR`; their launches do not need the shared-configuration gate. |
| Gated startup | No usable token is available for isolated configuration | Scout startup windows are serialized until the first SDK message or a 10-second watchdog; research overlaps once started. Multiple organizers launch with a short stagger. |

Gated startup is a normal fallback, including when credentials are available only through the macOS Keychain. It does not mean the whole research run is serial. These mechanisms address shared startup state, not filesystem permissions or isolation from malicious content.

Token priority is:

1. `ORACLE_OAUTH_TOKEN`.
2. `CLAUDE_CODE_OAUTH_TOKEN`.
3. The current session access token from the Claude Code credentials file, if sufficiently fresh.

For headless use or explicit configuration isolation, generate a token with:

```sh
claude setup-token
```

Provide the resulting secret as `ORACLE_OAUTH_TOKEN` through your environment or secrets manager. This variable applies to Oracle without changing how your interactive Claude Code sessions authenticate. Treat the token as a password; keep it out of commits, reports, and shell history.

For automatic isolation, Oracle parses the primary credentials file and selects the access token and expiry. It does not copy the credentials file or forward its refresh token into isolated subprocesses. Do not duplicate `.credentials.json` into temporary config directories: independent token refreshes can disrupt your primary login.

Temporary configuration directories are cleaned in `run()`'s `finally` block, including Python exceptions and cancellation. An abrupt process or machine termination can bypass cleanup. This mechanism applies to scouts and organizers; the fallback Architect uses the ordinary SDK environment.

## Data and tool access

| Surface | What to expect |
| --- | --- |
| Claude calls | Your question, supplied prompts, and research findings are sent through Claude Code/SDK authentication for model processing. |
| Web research | Search terms and fetched URLs reach external search and web services. |
| GitHub MCP | Enabled by `GITHUB_PAT`; requests use that token and can reach repositories it can access. |
| Scout local tools | `Read`, `Grep`, and `Glob` are added only with `--local` or `OracleSDK(local_tools=True)`. Scouts explicitly disallow `Bash`, `Write`, `Edit`, `NotebookEdit`, and `Agent`. |
| Other model stages | The fallback Architect and Sonnet organizers currently have local read tools regardless of the scout setting. |
| Saved reports | `--report` writes a dated Markdown file in the current directory. You control where captured stdout and Python API results are stored. |
| Round sessions | `--rounds N` for N > 1 or `--session-dir` saves settings, scout plans, returned reports, and metrics. The caller maintains `canonical.md`. Sessions default to a unique directory under `research/`. |

`--local` controls scout tooling; it is not an OS sandbox or a repository-only boundary. Untrusted web material can try to steer agents into exposing local data. Run in an environment without accessible secrets when combining local and web research, and grant only the GitHub permissions the task needs.

Chain isolation means Python supplies each organizer only its own chain's scout reports. It is a data-routing property, not a separate security boundary around the process. Oracle itself adds no separate telemetry service; Claude and the contacted services have their own data handling.

Keep credentials and sensitive findings out of source control. This repository ignores `oracle-report-*.md` and keeps `research/` private; those exclusions do not automatically apply to other projects where you run Oracle.

Each new round session includes a `.gitignore` that excludes its contents, including when you choose a directory outside `research/`. This is a Git convenience, not encryption or access control. Session metadata stores the research question, tool settings, per-round progress and outcome, attempt history, relative artifact paths, cumulative estimated usage, and the checkpoint your session records — but no authentication tokens and no scout content. Keep the full session when preserving private research, and resume with the same directory to retain the canonical report and round history.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `claude-oracle` is not found | Use `python -m claude_oracle --help`. Install into that same Python environment. |
| `/oracle` is missing or has old instructions | Run `python -m claude_oracle.install` again, then start a new Claude Code session. |
| `No module named claude_oracle` | Install with `python -m pip` using the interpreter that will launch Oracle. |
| Login errors or many scouts fail together | Confirm Claude Code works interactively in the same environment. Check token configuration without printing secrets. Broad failures skip the normal retry pass. |
| Scouts start gradually | Check the startup log: gated mode deliberately sequences launches. An explicit token enables isolated configuration. |
| Invalid JSON on stdin | Pass a saved UTF-8 JSON array, not Markdown fences or shell-escaped prose. Use the [agent guide's shell examples](agent-usage.md#execute). |
| GitHub tools fail | Confirm `npx` is available and the token is present in the launching environment with suitable permissions. |
| Report contains errors or raw scout output | Follow [failure handling](agent-usage.md#handle-results-and-failures); a successful process exit can still contain incomplete research. |
| `--rounds 3` returns after one round | Expected: the current agent plans the next round and uses `--resume`. The `/oracle` skill manages this loop. See [managed rounds](agent-usage.md#managed-rounds). |
| Session already has a round running | Keep observing that process and update the canonical report. Do not launch a duplicate; the OS releases the lock when the process exits. |
| A crashed session still says `running` | Confirm its process exited, then resume with a fresh plan. The old attempt is retained as interrupted. |
| Session directory already exists | Choose a new directory for new research; use `--resume` to continue existing research. |

Use `--verbose` to capture tool activity alongside progress. When opening an issue, include the package version, OS, Python version, command shape, and sanitized diagnostics. Remove prompts, findings, paths, and credentials you do not intend to share.
