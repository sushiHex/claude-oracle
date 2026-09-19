---
name: oracle
description: "Orchestrator-managed research. Adapt Haiku scout plans across rounds, receive isolated Sonnet chain briefings, and evolve one canonical report while research runs."
argument-hint: "[chains] [--rounds N] <research question>"
user-invocable: true
---

# Oracle v4.7.0 — Managed Research Rounds

Use the installed `claude_oracle` package. You, the current session, are the
orchestrator: plan the research, adapt later rounds to the findings, and write
the canonical report. Fable or Astra are preferred when already available as
the orchestrating session; keep the active session in charge rather than
launching a separate model to manage the rounds.

## Parse arguments

`$ARGUMENTS` has the form `[chains] [--rounds N] <question>`.

- The optional leading integer is the chain count, from 1 to 8; default **1**.
- `--rounds N` (or `--rounds=N`) selects a positive number of sequential research
  rounds; default **1**. Parse this option before the question.
- Reject missing, non-integer, zero, or negative round counts before launching
  research. Preserve the existing leading-chain syntax.

Examples:

- `/oracle compare Python task queues` → 1 chain, 1 round.
- `/oracle 4 compare Python task queues` → 4 chains, 1 round.
- `/oracle --rounds 3 compare Python task queues` → 1 chain, 3 rounds.
- `/oracle 4 --rounds 3 compare Python task queues` → 4 chains per round, 3 rounds.

Rounds are adaptive, not repetitions of one static plan. The next round is
planned only after reading the previous round's findings.

## One canonical report

Use `RoundSession.status()` for the machine-readable handoff: lifecycle is
separate from `research_outcome` (`unknown`, `complete`, `partial`, or `failed`).
It also reports phase/progress, active attempt, last update, artifact paths, and
next action. After editing the canonical report, record the caller-owned
checkpoint with `session.checkpoint(revision=..., through_round=...)`; it is
validated against completed rounds and never advanced automatically.

Create **one canonical report by default**, at the session's `canonical.md`.
Create additional canonical documents only when the user explicitly requests
them. Keep the same report throughout the run, including across resumptions.
Per-round raw reports are evidence and history, not separate final deliverables.

Every revision between rounds must aim for **final-draft quality** using the
available evidence: a coherent structure, clear conclusions, integrated source
citations, and explicit uncertainty. Strengthen and restructure the document;
reconcile contradictions, replace superseded claims, and improve the argument.
Do not merely append round notes, leave a running scratchpad, or polish only at
the very end. Keep unresolved questions visible without inventing answers.

The runtime creates an initial scaffold exactly once. You own its content from
then on; Oracle never overwrites it. If continuing existing work, read the
current canonical report before editing it.

## Plan the first round

Use the user's goal, constraints, and conversation context to produce exactly
`chains * 10` focused prompts. Write a UTF-8 JSON array to a file:

```json
[
  {"dimension": "short label", "prompt": "one focused research question"},
  {"dimension": "another label", "prompt": "a distinct research question"}
]
```

This abbreviated schema illustrates entries; the actual plan must contain ten
entries per chain. Oracle assigns IDs, chain labels, and confidence/source-date
instructions. Keep each prompt under 150 words, target one narrow question,
and include the context a scout needs; scouts do not inherit the conversation.
Group related dimensions in consecutive batches of ten.

Scouts have web tools and optional GitHub MCP. Add `--local` only when the task
requires this machine's files. See the package's access-boundary documentation
before combining local reads and untrusted web material.

## Start research and work while it runs

Choose a new, unique session directory under `research/`, such as
`research/oracle-<date>-<topic>-<unique-suffix>`. Session creation refuses existing
directories so earlier reports are preserved. Use `--resume` for an existing
session. Pass the original question when creating the session.

Bash / zsh example (substitute the requested counts, question, and paths):

```sh
PYTHONUTF8=1 python -m claude_oracle --rounds 3 --session-dir research/oracle-topic-run1 --verbose "Research question" < round-001-prompts.json
```

PowerShell:

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
Get-Content -Raw -Encoding UTF8 .\round-001-prompts.json | python -m claude_oracle --rounds 3 --session-dir research/oracle-topic-run1 --verbose "Research question"
```

Use the host's background execution or yielding process handle so the current
session can edit the canonical report while Oracle runs. Retain that handle
and observe completion. During the first round, develop the report's scope,
structure, and known context without claiming findings that have not returned.
Do not use heredocs for JSON or invoke a script by its installed skill path.

Progress is on stderr; each round's report is on stdout and saved in its attempt
directory. Inspect progress without starting models or waiting for the process:

```sh
python -m claude_oracle --session-status research/oracle-topic-run1
```

The status snapshot includes lifecycle, research outcome, checkpoint, usage, and
live scout/organizer progress. Token fields are unavailable when the provider
did not report usage; do not interpret missing usage as zero.

## Evolve the next round and the report

After each round:

1. Read the full saved `report.md` and `metrics.json` from the completed attempt
   listed in `session.json`. Inspect errors, missing chains, disputes, and gaps.
2. If rounds remain, design a **fresh** `chains * 10` prompt plan using those
   findings, earlier evidence, the canonical report, and the original goal.
   Prioritize unresolved questions, conflicting claims, stronger primary
   sources, and promising leads. Avoid repeating already-settled coverage.
3. Save the next plan and launch the next round using the same session:

   ```sh
   PYTHONUTF8=1 python -m claude_oracle --resume research/oracle-topic-run1 --verbose < round-002-prompts.json
   ```

   In PowerShell, use the same UTF-8 setup and `Get-Content` pipeline as above,
   replacing the creation arguments with `--resume <session>`.
4. **While that next round runs, revise the same canonical report to final-draft
   quality using all completed findings.** Integrate sources and corrections,
   remove stale claims, tighten the narrative, and state outstanding questions.
   Do not cite a running round as evidence. Finish this revision even if the
   research returns quickly; then incorporate its results into the next cycle.

`--resume` uses the saved question, round budget, and tool configuration. It
executes one round and requires a fresh prompt array on stdin. Do not start
another process for a session that already has a round running. Failed or
interrupted attempts can be retried with `--resume`; their files are retained.
An ordinary returned report can still contain failed model work: inspect the
metrics and stop to diagnose systemic failures instead of blindly spending the
remaining rounds.

## Finish

When session status is `rounds_complete`, perform the final substantive revision
of the canonical report using the last round's findings. That status means the
research calls have returned, not that writing is finished.

Deliver the canonical report's path, the main conclusions, remaining
uncertainties, and a concise summary of rounds and usage. Keep all raw round
outputs available for traceability. The canonical report must be a finished,
coherent document; additional canonical documents remain opt-in by explicit
user instruction.
