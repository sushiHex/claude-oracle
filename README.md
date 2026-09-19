# Claude Oracle

**Parallel research for Claude Code. Bring the findings back to your conversation.**

Oracle turns a broad question into focused research tasks, sends them to Haiku scouts, and has Sonnet organize the results. Your session gets the findings, sources, disagreements, and gaps to build a better answer with its existing context.

With `--rounds`, your session adapts the research after each return and evolves one canonical report to final-draft quality while the next round runs.

[Quick start](#quick-start) · [Adaptive rounds](#adaptive-research-rounds) · [How it works](#how-it-works) · [For agents](#for-agents) · [Configuration](docs/configuration.md)

## Why Oracle

- **Cover more ground.** Run 1–8 research chains, normally ten scouts per chain, to investigate different angles in parallel.
- **Keep the evidence useful.** Scouts are prompted to date sources and label confidence; Sonnet organizes each chain's full reports and calls out conflicts and missing information.
- **Keep the final judgment in your session.** Each chain returns its own briefing. Your calling agent compares them and writes the final answer.
- **Build on what you learn.** Later rounds investigate gaps and contradictions from earlier findings. The same canonical report grows stronger between rounds.

Useful for comparing tools, mapping an unfamiliar ecosystem, checking repository activity, or surveying approaches before implementation. Start with one chain; add more when the question has several distinct areas to investigate.

## Quick start

You need **Python 3.10+**, **Git** for the install command below, and **[Claude Code](https://claude.com/claude-code) installed and signed in** with a subscription that supports it. Node.js is needed only for the optional GitHub MCP integration. If your Python 3 command is `python3`, use it in place of `python` below.

```sh
python -m pip install git+https://github.com/sushiHex/claude-oracle.git
python -m claude_oracle.install
```

The installer adds `/oracle` to `~/.claude/skills/oracle/`, available across your Claude Code projects. It installs a skill file and a small launcher that delegates to the Python package.

In a Claude Code session:

```text
/oracle compare Python background-job libraries for a small production service
/oracle 4 map the tradeoffs between Redis, RabbitMQ, and managed task queues
/oracle --rounds 3 compare Python background-job libraries for a small production service
```

The optional leading number selects the chain count. One chain uses **10 Haiku scouts + 1 Sonnet organizer per round**; four use **40 + 4**. Rounds default to **1**. Your session plans the research using the conversation context and maintains the canonical report.

<details>
<summary>Upgrade an existing installation</summary>

```sh
python -m pip install --upgrade git+https://github.com/sushiHex/claude-oracle.git
python -m claude_oracle.install
```

Run both commands with the Python environment you use for Oracle. Reinstalling the skill refreshes its instructions as well as its launcher.

</details>

## Run from a terminal

```sh
python -m claude_oracle "compare Python background-job libraries"
python -m claude_oracle --chains 4 --verbose --report "compare task queue architectures"
python -m claude_oracle --local "map this repository's testing gaps"
```

The `claude-oracle` console command is equivalent. Without supplied prompts, a Sonnet **Architect** decomposes your question first. This separate invocation receives the question you pass, not your conversation history.

| Option | What it does |
| --- | --- |
| `--chains N`, `-c N` | Choose 1–8 chains; default `1`. Supplied JSON prompts determine their own chain count. |
| `--rounds N` | Set the research round budget; default `1`. Multiple rounds create a resumable session and return control after each round. |
| `--session-dir PATH` | Create a session at a new directory, including for a single round. Multiple rounds otherwise use `research/oracle-<unique-id>`. |
| `--resume PATH` | Run the next round with a fresh JSON prompt array on stdin, using saved session settings. |
| `--session-status PATH` | Print the session's lifecycle, research outcome, live phase and progress, checkpoint, artifact paths, and usage as JSON. Never runs models or reads stdin. |
| `--verbose`, `-v` | Add per-scout tool activity to the progress log. |
| `--report`, `-r` | Also save `oracle-report-YYYYMMDD-HHMMSS.md` in the current directory. |
| `--local` | Grant scouts local `Read`, `Grep`, and `Glob` tools for repository research. |
| `--usd` | Show the SDK-reported dollar cost instead of Oracle's estimated quota percentages. |
| `--help` | Show the command reference without starting research. |

The report goes to **stdout**; progress and diagnostics go to **stderr**. Reports include execution time, token usage, and scout/organizer error counts. Scouts use `HIGH` for directly sourced claims, `MEDIUM` for derived figures, and `LOW` for estimates. These are model assessments; verify consequential claims against their sources.

## Adaptive research rounds

```text
/oracle 2 --rounds 3 evaluate durable workflow engines for our Python service
```

The `/oracle` skill manages the full workflow in your current session:

1. **Investigate.** Plan and launch the first round with the user's context and constraints.
2. **Adapt.** Read the returned evidence, then target the next round at unresolved questions, contradictions, and promising leads.
3. **Strengthen.** While the next round runs, revise the same canonical report to **final-draft quality** using completed findings: integrate citations, replace stale claims, sharpen conclusions, and keep uncertainty visible.
4. **Finish.** Incorporate the last round's findings in a final substantive revision and deliver the report.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/rounds-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/rounds.svg">
  <img src="docs/rounds.svg" alt="The current session sets a research-round budget that defaults to one, plans each round, receives completed findings from Oracle, and uses those findings to adapt later rounds. While a later round runs, the session strengthens the same canonical report to final-draft quality using completed evidence; after the last return, it makes a final substantive revision and delivers the report." width="1120">
</picture>

**One canonical report is the default.** It is updated in place between rounds; additional canonical documents are created only when you ask. Per-round plans, raw reports, and metrics remain available as supporting history.

**Progress is readable while research runs.** `--session-status` (or `RoundSession.status()`) reports lifecycle and research outcome as separate things: a round can finish successfully while its `research_outcome` is `partial` because some scouts failed. Alongside those it returns the current phase, live scout and organizer counts, the active attempt, artifact paths, and cumulative usage — marked unavailable rather than zero when a model attempt reported none. Status reads never block on a running round and never start a model.

When your session has revised the canonical report, record that with `RoundSession.checkpoint(revision=..., through_round=N)`. Oracle validates `N` against completed rounds and never advances the checkpoint itself — it tracks how far your writing has caught up with the evidence, and says nothing about whether the report is finished.

The active session supplies the judgment and writing—Fable or Astra are preferred orchestrators when available. Oracle does not launch a separate manager. The **CLI and Python API execute one round per call**; `/oracle` follows the orchestration loop for you. Agents integrating the CLI should follow the [managed-round protocol](docs/agent-usage.md#managed-rounds). Ordinary CLI calls without a session keep their existing single-round output.

## How it works

Inside each research round:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/architecture-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/architecture.svg">
  <img src="docs/architecture.svg" alt="Your session or a Sonnet Architect plans the research. Each chain runs ten Haiku scouts, then a Sonnet organizer receives only that chain's reports. Separate chain briefings return to the caller for final synthesis." width="1120">
</picture>

1. **Plan.** Your session creates focused prompts, or the CLI asks Sonnet to do it.
2. **Research.** Haiku scouts gather evidence concurrently. Python groups their reports by chain and passes successful reports in full to that chain's Sonnet organizer.
3. **Return.** Organizers are asked to produce findings, corrections, disputes, and gaps. All chain briefings return to the caller; Oracle does not run an additional cross-chain merger or launch Opus.

In the logs, scouts are **Smiths**, organizers are **Andersons**, and the planner is the **Architect**. These names describe roles; chain isolation is enforced by the Python orchestration.

## For agents

Use `/oracle` inside Claude Code, or call the CLI from any agent that can launch local processes in the authenticated environment. Oracle is a Python package and CLI, not an MCP server.

For control over the research plan, write a UTF-8 JSON array to a file and pass it on **stdin**. Each entry has a `dimension` and a `prompt`. For predictable automatic grouping, supply exactly `10 × chains` entries (10–80). Oracle skips the Architect, assigns IDs, and routes these complete batches into consecutive chains of ten.

Partial unlabeled batches have different routing: **11–19 prompts all go to chain A**. To request uneven chain sizes, set explicit `chain` labels and unique IDs on every entry. See [prompt grouping](docs/agent-usage.md#prompt-grouping) for the rules and examples.

See the **[agent integration guide](docs/agent-usage.md)** for a complete prompt file, Bash and PowerShell commands, Python integration, and output/error handling. Carry the relevant conversation context into each prompt; scouts do not inherit it automatically.

## Configuration and privacy

Ordinary use needs no Oracle-specific token setup. Oracle uses your Claude Code authentication. Optional settings:

| Setting | Purpose |
| --- | --- |
| `GITHUB_PAT` | Enable the pinned GitHub MCP server through `npx` for repository research. Limit the token's permissions. |
| `ORACLE_OAUTH_TOKEN` | Supply an Oracle-specific OAuth token for isolated subprocess configuration. |
| `CLAUDE_CODE_OAUTH_TOKEN` | Also accepted; Oracle's own token takes precedence when both are set. |

**Research leaves your machine.** Prompts and findings are processed by Claude; web searches and fetches contact external services. Local tools are opt-in **for scouts**. The Architect and Anderson currently have local read tools independently of `--local`; this flag is not a filesystem sandbox for the whole pipeline. Combining local reads with untrusted web content can expose sensitive files through prompt injection.

Read **[configuration and access boundaries](docs/configuration.md)** before enabling local research or GitHub access. That guide also explains automatic startup isolation, credential handling, and recovery.

## Usage and reliability

More chains and rounds increase model and tool usage: two chains over three rounds normally dispatch **60 scouts and 6 organizers**, plus the orchestrating session's work. Oracle assigns broad searching to Haiku and organization to Sonnet; savings depend on the task. The displayed quota percentages are unofficial estimates based on fixed assumptions, not your account's remaining balance. They weight cached prompt tokens at their published rates rather than as fresh input — cache reads cost a fraction, cache writes a premium — which matters because organizer turns are cache-dominated. `--usd` is SDK-reported usage, not a subscription invoice.

Scouts have timeouts and a limited retry pass. Chains with no successful scouts skip organization. Some organizer failures preserve raw scout output; check the report for error or fallback sections before treating a run as complete. See [failure handling](docs/agent-usage.md#handle-results-and-failures).

## Development

Develop code and documentation in [sushiHex/claude-oracle](https://github.com/sushiHex/claude-oracle), with pull requests targeting `main`. Keep private research and report data out of public contributions.

```sh
python -m pip install -e ".[test]"
python -m pytest tests/ -q
python -m claude_oracle --help
```

Tests mock model calls and isolate credentials; they do not run live research. CI covers Python 3.10 and 3.14 on Ubuntu and Windows. Package code lives in `src/claude_oracle/`; see [CLAUDE.md](CLAUDE.md) for maintenance conventions.

## License

[MIT](LICENSE).
