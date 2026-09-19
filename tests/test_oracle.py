"""Unit tests for the Oracle SDK — pure logic + fixed edge cases.

No network: the async paths monkeypatch claude_oracle.sdk.query with a fake
async generator. Tests are plain sync functions that drive coroutines with
asyncio.run (no pytest-asyncio dependency).
"""
import asyncio
import os
from pathlib import Path

import pytest

from claude_oracle import sdk
from claude_oracle.sdk import (
    OracleSDK,
    ScoutResult,
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    MAX_CHAINS,
    MODEL_WEIGHTS,
    OUTPUT_MULTIPLIER,
    SCOUT_SUFFIX,
    _extract_json_array,
    _extract_usage,
    _normalize_prompts,
)

_UNSET = object()


class _Msg:
    """Fake SDK message. Sets .result only when provided (mirrors ResultMessage,
    where .result may be absent or None)."""

    def __init__(self, result=_UNSET, usage=None):
        if result is not _UNSET:
            self.result = result
        if usage is not None:
            self.usage = usage


def _fake_query(messages, calls=None):
    """Return a stand-in for claude_agent_sdk.query yielding `messages`."""

    def _query(*args, **kwargs):
        if calls is not None:
            calls.append(kwargs)

        async def _gen():
            for m in messages:
                yield m

        return _gen()

    return _query


async def _no_sleep(*_a, **_k):
    return None


@pytest.fixture(autouse=True)
def _hermetic_auth(monkeypatch, tmp_path):
    """Unit tests must never read the developer's real Claude credentials,
    inherit their real oracle tokens, or create isolation dirs from them
    (leaked oracle-iso-* temp dirs, observed 2026-07-12). Tests that need a
    token set env vars or monkeypatch _credentials_file themselves — those
    run AFTER this fixture and win."""
    monkeypatch.delenv("ORACLE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(sdk, "_credentials_file", lambda: str(tmp_path / "no-creds.json"))


# --------------------------------------------------------------------------
# _extract_json_array
# --------------------------------------------------------------------------

def test_extract_json_array_plain():
    assert _extract_json_array('[{"a": 1}]') == [{"a": 1}]


def test_extract_json_array_fenced():
    assert _extract_json_array('```json\n[{"a": 1}]\n```') == [{"a": 1}]


def test_extract_json_array_embedded_in_prose():
    assert _extract_json_array("here you go: [1, 2, 3] done") == [1, 2, 3]


def test_extract_json_array_fence_without_newline_raises_valueerror():
    # Regression: `text.split("\n", 1)[1]` used to raise IndexError here.
    with pytest.raises(ValueError):
        _extract_json_array("```")


def test_extract_json_array_prose_raises_valueerror():
    # Regression: unguarded json.loads used to raise JSONDecodeError and crash run.
    with pytest.raises(ValueError):
        _extract_json_array("I need more context to decompose this.")


def test_extract_json_array_empty_or_none_raises_valueerror():
    with pytest.raises(ValueError):
        _extract_json_array("")
    with pytest.raises(ValueError):
        _extract_json_array(None)


# --------------------------------------------------------------------------
# _normalize_prompts
# --------------------------------------------------------------------------

def test_normalize_auto_assigns_chain_and_id():
    out = _normalize_prompts([{"dimension": "d", "prompt": "p"}])
    assert out[0]["chain"] == "A"
    assert out[0]["id"] == 1
    assert out[0]["prompt"].endswith(SCOUT_SUFFIX)


def test_normalize_fills_missing_dimension():
    # Regression: `p["dimension"]` used to KeyError.
    out = _normalize_prompts([{"prompt": "p"}])
    assert out[0]["dimension"]


def test_normalize_has_chains_branch_fills_id_and_dimension():
    # Regression: chain-labeled prompts without id/dimension used to KeyError
    # in the scout dispatch (and crash the whole run via gather).
    out = _normalize_prompts([{"chain": "A", "prompt": "p"}])
    assert out[0]["id"] == 1
    assert out[0]["dimension"]
    assert out[0]["prompt"].endswith(SCOUT_SUFFIX)


def test_normalize_is_idempotent():
    once = _normalize_prompts([{"dimension": "d", "prompt": "p"}])
    before = once[0]["prompt"]
    twice = _normalize_prompts(once)
    assert twice[0]["prompt"] == before
    assert twice[0]["prompt"].count("For any specific number") == 1


def test_normalize_rejects_empty_or_missing_prompt():
    with pytest.raises(ValueError):
        _normalize_prompts([{"dimension": "d", "prompt": "   "}])
    with pytest.raises(ValueError):
        _normalize_prompts([{"dimension": "d"}])


def test_normalize_rejects_non_list_and_empty():
    with pytest.raises(ValueError):
        _normalize_prompts({})
    with pytest.raises(ValueError):
        _normalize_prompts([])


# --------------------------------------------------------------------------
# _extract_usage
# --------------------------------------------------------------------------

def test_extract_usage_attr_object():
    class U:
        input_tokens = 10
        output_tokens = 2

    class M:
        usage = U()

    s = _extract_usage(M(), "haiku")
    assert s.input_tokens == 10 and s.output_tokens == 2
    assert s.usage_available is True


def test_extract_usage_dict():
    class M:
        usage = {"input_tokens": 3, "output_tokens": 4}

    s = _extract_usage(M(), "sonnet")
    assert s.input_tokens == 3 and s.output_tokens == 4


def test_extract_usage_dict_with_none_does_not_crash():
    # Regression: `.get("input_tokens", 0)` returned None -> TypeError in the math.
    class M:
        usage = {"input_tokens": None, "output_tokens": 5}

    s = _extract_usage(M(), "haiku")
    assert s.input_tokens == 0 and s.output_tokens == 5


def test_extract_usage_missing_usage():
    class M:
        pass

    s = _extract_usage(M(), "haiku")
    assert s.input_tokens == 0 and s.output_tokens == 0
    assert s.usage_observed is True and s.usage_available is False


def test_extract_usage_includes_cache_token_fields():
    class M:
        usage = {
            "input_tokens": 3,
            "output_tokens": 4,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 6,
        }

    s = _extract_usage(M(), "sonnet")
    assert s.cache_read_input_tokens == 5
    assert s.cache_creation_input_tokens == 6
    assert s.total_tokens == 18
    assert s.usage_available is True


def test_extract_usage_weights_cache_tokens_against_fresh_input():
    # Cache reads bill ~0.1x fresh input and cache writes ~1.25x; charging them
    # at 1.0x overstates the dominant term on cache-heavy Anderson turns.
    class M:
        usage = {
            "input_tokens": 3,
            "output_tokens": 4000,
            "cache_read_input_tokens": 3040,
            "cache_creation_input_tokens": 400,
        }

    s = _extract_usage(M(), "sonnet")
    expected_input = 3 + 3040 * CACHE_READ_MULTIPLIER + 400 * CACHE_WRITE_MULTIPLIER
    assert s.quota_units == (expected_input + 4000 * OUTPUT_MULTIPLIER) * 1.0
    # Unweighted accounting would have charged the full 3,440 cached tokens.
    assert s.quota_units < (3 + 3040 + 400 + 4000 * OUTPUT_MULTIPLIER) * 1.0


def test_report_footer_token_breakdown_sums_to_the_total(monkeypatch):
    """The footer's parts must account for the total it prints. Regression:
    total_tokens started counting cache tokens while the breakdown still showed
    only in/out, so the numbers on screen no longer added up."""
    import re

    usage = {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 40,
    }
    monkeypatch.setattr(sdk, "query", _fake_query([_Msg(result="finding", usage=usage)]))

    prompts = [{"chain": "A", "id": 1, "dimension": "d", "prompt": "p"}]
    report = asyncio.run(OracleSDK(chains=1).run("q", prompts=prompts))

    line = next(ln for ln in report.splitlines() if ln.startswith("- Total tokens:"))
    total, inp, out, cache_read, cache_write = (
        int(n.replace(",", "")) for n in re.findall(r"[\d,]+", line)
    )
    assert inp + out + cache_read + cache_write == total


def test_extract_usage_applies_model_weight_after_cache_weighting():
    class M:
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 0,
        }

    haiku = _extract_usage(M(), "haiku")
    assert haiku.quota_units == 1000 * CACHE_READ_MULTIPLIER * MODEL_WEIGHTS["haiku"]


def test_progress_callback_reports_completed_work():
    updates = []
    oracle = OracleSDK(chains=1, progress_callback=updates.append)
    oracle._progress_totals = {"scouts": 2, "organizers": 1}
    oracle._active_scouts = {1: "done", 2: "running"}
    oracle._completed_organizers = 1
    oracle._emit_progress("organize")
    assert updates[-1] == {
        "phase": "organize",
        "progress": {
            "scouts": {"completed": 1, "total": 2},
            "organizers": {"completed": 1, "total": 1},
        },
    }


# --------------------------------------------------------------------------
# OracleSDK.run — MAX_CHAINS enforcement for library callers
# --------------------------------------------------------------------------

def test_run_rejects_more_than_max_chains():
    prompts = [
        {"chain": chr(65 + i), "id": i + 1, "dimension": "d", "prompt": "p"}
        for i in range(MAX_CHAINS + 1)
    ]
    with pytest.raises(ValueError):
        asyncio.run(OracleSDK(chains=1).run("q", prompts=prompts))


# --------------------------------------------------------------------------
# _run_scout — empty result is an error, not a silent success
# --------------------------------------------------------------------------

def test_scout_empty_result_is_error(monkeypatch):
    monkeypatch.setattr(sdk, "query", _fake_query([_Msg(result="")]))
    r = asyncio.run(OracleSDK(chains=1)._run_scout(1, "A", "dim", "prompt"))
    assert r.error is not None
    assert r.result_text == ""


def test_scout_none_result_is_error(monkeypatch):
    monkeypatch.setattr(sdk, "query", _fake_query([_Msg(result=None)]))
    r = asyncio.run(OracleSDK(chains=1)._run_scout(1, "A", "dim", "prompt"))
    assert r.error is not None


def test_scout_no_result_message_is_error(monkeypatch):
    monkeypatch.setattr(sdk, "query", _fake_query([_Msg()]))
    r = asyncio.run(OracleSDK(chains=1)._run_scout(1, "A", "dim", "prompt"))
    assert r.error is not None


def test_scout_normal_result_succeeds(monkeypatch):
    monkeypatch.setattr(sdk, "query", _fake_query([_Msg(result="Real findings")]))
    r = asyncio.run(OracleSDK(chains=1)._run_scout(2, "A", "dim", "prompt"))
    assert r.error is None
    assert r.result_text == "Real findings"


# --------------------------------------------------------------------------
# compress — a chain whose Smiths all failed is skipped (no Sonnet call)
# --------------------------------------------------------------------------

def test_compress_skips_all_errored_chain(monkeypatch):
    calls = []
    monkeypatch.setattr(sdk, "query", _fake_query([_Msg(result="ANDERSON OK")], calls=calls))
    monkeypatch.setattr(sdk.asyncio, "sleep", _no_sleep)  # skip the stagger delay

    scouts = [
        ScoutResult(scout_id=1, chain="A", dimension="d", result_text="data", error=None),
        ScoutResult(scout_id=2, chain="B", dimension="d", result_text="", error="Timed out"),
        ScoutResult(scout_id=3, chain="B", dimension="d", result_text="", error="Empty"),
    ]
    results = asyncio.run(OracleSDK(chains=2).compress(scouts))
    by_chain = {r.chain: r for r in results}

    assert set(by_chain) == {"A", "B"}
    assert by_chain["A"].error is None
    assert by_chain["A"].summary == "ANDERSON OK"
    assert by_chain["B"].error is not None
    assert by_chain["B"].summary == ""
    assert len(calls) == 1  # only chain A reached the model


def test_compress_anderson_failure_stashes_raw_smith_fallback(monkeypatch):
    # When Anderson itself raises, the chain's raw Smith outputs are preserved
    # as fallback_scouts instead of being discarded.
    monkeypatch.setattr(sdk.asyncio, "sleep", _no_sleep)
    o = OracleSDK(chains=1)

    async def _boom(chain, scouts):
        raise RuntimeError("anderson blew up")

    monkeypatch.setattr(o, "_run_compressor", _boom)
    scouts = [ScoutResult(scout_id=1, chain="A", dimension="d", result_text="kept data", error=None)]
    results = asyncio.run(o.compress(scouts))

    assert len(results) == 1
    r = results[0]
    assert r.error is not None
    assert r.fallback_scouts is not None and len(r.fallback_scouts) == 1
    assert r.fallback_scouts[0].result_text == "kept data"


def test_raw_smith_fallback_renders_data_and_errors():
    out = OracleSDK._raw_smith_fallback([
        ScoutResult(scout_id=1, chain="A", dimension="ok", result_text="finding", error=None),
        ScoutResult(scout_id=2, chain="A", dimension="bad", result_text="", error="boom"),
    ])
    assert "Smith #1" in out and "finding" in out
    assert "Smith #2" in out and "error: boom" in out


def test_scout_gate_blocks_next_launch_until_first_message(monkeypatch):
    """The launch gate holds Smith N+1's subprocess spawn until Smith N's
    stream emits its first message (startup config I/O settled). No sleeps
    to patch — v4.3.3 has no fixed stagger; only the gate may block."""
    monkeypatch.delenv("ORACLE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(sdk, "_session_access_token", lambda: None)  # force gated mode

    async def _main():
        ev = asyncio.Event()
        calls = []

        def gated_query(*, prompt=None, options=None, **_kw):
            idx = len(calls)
            calls.append(idx)

            async def _gen(i=idx):
                if i == 0:
                    await ev.wait()  # hold Smith #1's startup open
                yield _Msg(result="ok")

            return _gen()

        monkeypatch.setattr(sdk, "query", gated_query)
        prompts = [
            {"chain": "A", "id": i + 1, "dimension": f"d{i}", "prompt": "p"}
            for i in range(2)
        ]
        task = asyncio.create_task(OracleSDK(chains=1).scout(prompts))
        for _ in range(100):
            await asyncio.sleep(0)
        # Smith #2 must NOT have launched while #1's startup is unfinished
        assert len(calls) == 1
        ev.set()
        results = await task
        assert len(calls) == 2
        assert all(r.error is None for r in results)

    asyncio.run(_main())


def test_scout_gate_watchdog_frees_wedged_startup(monkeypatch):
    """A subprocess that never emits a first message may hold the gate only
    STARTUP_GATE_TIMEOUT_S — the rest of the fleet launches and succeeds, and
    the wedged Smith recovers via the retry pass."""
    monkeypatch.delenv("ORACLE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(sdk, "_session_access_token", lambda: None)  # force gated mode
    monkeypatch.setattr(sdk, "STARTUP_GATE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(sdk, "SCOUT_TIMEOUT_S", 0.3)
    state = {"first_call": True}

    def gated_query(*, prompt=None, options=None, **_kw):
        wedge = state["first_call"]
        state["first_call"] = False

        async def _gen():
            if wedge:
                await asyncio.sleep(3600)  # wedged: no first message, ever
            yield _Msg(result="ok")

        return _gen()

    monkeypatch.setattr(sdk, "query", gated_query)
    prompts = [
        {"chain": "A", "id": i + 1, "dimension": f"d{i}", "prompt": "p"}
        for i in range(2)
    ]
    o = OracleSDK(chains=1)
    results = asyncio.run(o.scout(prompts))

    assert len(results) == 2
    # Smith #2 was never blocked past the watchdog window; Smith #1's wedged
    # launch timed out, then recovered on the retry pass with a fresh process.
    assert all(r.error is None for r in results)
    assert o.metrics.scout_errors == 0


# --------------------------------------------------------------------------
# scout() retry pass — failed Smiths are relaunched exactly once
# --------------------------------------------------------------------------

def _retry_prompts(n):
    return [
        {"chain": "A", "id": i + 1, "dimension": f"d{i}", "prompt": "p"}
        for i in range(n)
    ]


def test_scout_retries_failed_smith_once(monkeypatch):
    """A Smith that errors on first launch is retried and recovers."""
    monkeypatch.setattr(sdk.asyncio, "sleep", _no_sleep)
    calls: dict[int, int] = {}

    async def fake_run(self, scout_id, chain, dimension, prompt, on_started=None):
        calls[scout_id] = calls.get(scout_id, 0) + 1
        if scout_id == 1 and calls[scout_id] == 1:
            return sdk.ScoutResult(scout_id=scout_id, chain=chain,
                                   dimension=dimension, result_text="",
                                   error="Command failed with exit code 1")
        return sdk.ScoutResult(scout_id=scout_id, chain=chain,
                               dimension=dimension, result_text="ok")

    monkeypatch.setattr(OracleSDK, "_run_scout", fake_run)
    o = OracleSDK(chains=1)
    results = asyncio.run(o.scout(_retry_prompts(3)))

    assert [r.error for r in results] == [None, None, None]
    assert calls[1] == 2 and calls[2] == 1 and calls[3] == 1
    assert o.metrics.scout_errors == 0  # recovered Smiths are not errors


def test_scout_retry_happens_only_once(monkeypatch):
    """A Smith that fails twice stays failed — no retry loop."""
    monkeypatch.setattr(sdk.asyncio, "sleep", _no_sleep)
    calls: dict[int, int] = {}

    async def fake_run(self, scout_id, chain, dimension, prompt, on_started=None):
        calls[scout_id] = calls.get(scout_id, 0) + 1
        return sdk.ScoutResult(scout_id=scout_id, chain=chain,
                               dimension=dimension, result_text="",
                               error="still broken")

    monkeypatch.setattr(OracleSDK, "_run_scout", fake_run)
    o = OracleSDK(chains=1)
    results = asyncio.run(o.scout(_retry_prompts(1)))

    assert calls[1] == 2
    assert results[0].error
    assert o.metrics.scout_errors == 1


def test_scout_retry_skipped_on_systemic_failure(monkeypatch):
    """If more than half the Smiths failed, something systemic is wrong
    (auth, network) — do not burn a serial retry pass on all of them."""
    monkeypatch.setattr(sdk.asyncio, "sleep", _no_sleep)
    calls: dict[int, int] = {}

    async def fake_run(self, scout_id, chain, dimension, prompt, on_started=None):
        calls[scout_id] = calls.get(scout_id, 0) + 1
        return sdk.ScoutResult(scout_id=scout_id, chain=chain,
                               dimension=dimension, result_text="",
                               error="systemic")

    monkeypatch.setattr(OracleSDK, "_run_scout", fake_run)
    o = OracleSDK(chains=1)
    results = asyncio.run(o.scout(_retry_prompts(4)))

    assert all(calls[i] == 1 for i in calls)  # no retries attempted
    assert o.metrics.scout_errors == 4


# --------------------------------------------------------------------------
# Config isolation — per-subprocess CLAUDE_CONFIG_DIR when token auth exists
# --------------------------------------------------------------------------

def _cap_query(captured, result="ok"):
    def _q(*, prompt=None, options=None, **_kw):
        captured.append(options)

        async def _gen():
            yield _Msg(result=result)

        return _gen()

    return _q


def test_scout_isolation_gives_each_smith_its_own_config_dir(monkeypatch):
    monkeypatch.setenv("ORACLE_OAUTH_TOKEN", "tok-test")
    captured = []
    monkeypatch.setattr(sdk, "query", _cap_query(captured))

    o = OracleSDK(chains=1)
    prompts = [
        {"chain": "A", "id": i + 1, "dimension": f"d{i}", "prompt": "p"}
        for i in range(2)
    ]
    results = asyncio.run(o.scout(prompts))

    assert all(r.error is None for r in results)
    dirs = [c.env["CLAUDE_CONFIG_DIR"] for c in captured]
    assert len(dirs) == 2 and len(set(dirs)) == 2   # distinct dirs
    assert all(os.path.isdir(d) for d in dirs)       # actually created
    # token forwarded to children under the name the CLI honors
    assert all(c.env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-test" for c in captured)
    # children keep the parent environment (PATH etc.)
    assert all("PATH" in c.env or "Path" in c.env for c in captured)

    root = os.path.dirname(dirs[0])
    o._cleanup_isolation()
    assert not os.path.exists(root)


def test_scout_isolation_skips_gate(monkeypatch):
    """With isolated config dirs there is nothing to race on — Smith #2 must
    launch even while Smith #1's startup is still open."""
    monkeypatch.setenv("ORACLE_OAUTH_TOKEN", "tok-test")

    async def _main():
        ev = asyncio.Event()
        calls = []

        def gated_query(*, prompt=None, options=None, **_kw):
            idx = len(calls)
            calls.append(idx)

            async def _gen(i=idx):
                if i == 0:
                    await ev.wait()
                yield _Msg(result="ok")

            return _gen()

        monkeypatch.setattr(sdk, "query", gated_query)
        o = OracleSDK(chains=1)
        prompts = [
            {"chain": "A", "id": i + 1, "dimension": f"d{i}", "prompt": "p"}
            for i in range(2)
        ]
        task = asyncio.create_task(o.scout(prompts))
        for _ in range(100):
            await asyncio.sleep(0)
        assert len(calls) == 2  # NOT gated: both launched immediately
        ev.set()
        results = await task
        o._cleanup_isolation()
        assert all(r.error is None for r in results)

    asyncio.run(_main())


def test_scout_no_isolation_without_token(monkeypatch):
    monkeypatch.delenv("ORACLE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(sdk, "_session_access_token", lambda: None)  # no session either
    captured = []
    monkeypatch.setattr(sdk, "query", _cap_query(captured))

    prompts = [{"chain": "A", "id": 1, "dimension": "d", "prompt": "p"}]
    results = asyncio.run(OracleSDK(chains=1).scout(prompts))

    assert results[0].error is None
    assert not captured[0].env.get("CLAUDE_CONFIG_DIR")  # shared config, gated mode


def test_run_cleans_up_isolation_dirs_on_crash(monkeypatch):
    """A crash mid-run must not leak per-run isolated config dirs."""
    monkeypatch.setenv("ORACLE_OAUTH_TOKEN", "tok-test")
    o = OracleSDK(chains=1)

    async def boom(self, prompts):
        self._isolated_env("smith-1")  # simulate dirs created before the crash
        raise RuntimeError("mid-run crash")

    monkeypatch.setattr(OracleSDK, "scout", boom)
    prompts = [{"chain": "A", "id": 1, "dimension": "d", "prompt": "p"}]

    with pytest.raises(RuntimeError):
        asyncio.run(o.run("q", prompts=prompts))
    assert o._iso_root is None  # cleanup ran despite the exception


# --------------------------------------------------------------------------
# Scout toolset — web-only by default; local file tools are opt-in
# --------------------------------------------------------------------------

def test_scout_default_toolset_is_web_only(monkeypatch):
    """Prompt-injection hardening: scouts process untrusted web content, so
    the local-filesystem tools (Read/Grep/Glob) must be opt-in, not default."""
    captured = []
    monkeypatch.setattr(sdk, "query", _cap_query(captured))
    asyncio.run(OracleSDK(chains=1)._run_scout(1, "A", "d", "p"))
    tools = captured[0].allowed_tools
    assert "WebSearch" in tools and "WebFetch" in tools
    assert not any(t in tools for t in ("Read", "Grep", "Glob"))


def test_scout_local_tools_opt_in(monkeypatch):
    captured = []
    monkeypatch.setattr(sdk, "query", _cap_query(captured))
    asyncio.run(OracleSDK(chains=1, local_tools=True)._run_scout(1, "A", "d", "p"))
    tools = captured[0].allowed_tools
    assert all(t in tools for t in ("Read", "Grep", "Glob", "WebSearch", "WebFetch"))


def test_github_mcp_version_is_pinned(monkeypatch):
    monkeypatch.setenv("GITHUB_PAT", "ghp_test")
    mcp = sdk._github_mcp()
    pkg = [a for a in mcp["github"]["args"] if "server-github" in a][0]
    assert "@2025." in pkg  # exact-version pin, not a floating latest


# --------------------------------------------------------------------------
# Release hygiene
# --------------------------------------------------------------------------

def test_packaged_skill_header_matches_version():
    """A release bumps __version__ and the packaged SKILL.md header; everything
    else derives from the constant. The packaged copy is what external users
    get, and it went stale at v4.3.1 and again across v4.8.0's feature PRs —
    so the pairing is enforced here rather than remembered."""
    skill = Path(sdk.__file__).parent / "data" / "SKILL.md"
    header = next(
        line for line in skill.read_text(encoding="utf-8").splitlines()
        if line.startswith("# Oracle v")
    )
    assert header.startswith(f"# Oracle v{sdk.__version__} ")


# --------------------------------------------------------------------------
# Cost settings — benchmarked choices, not defaults left unset. Each of these
# reaches ClaudeAgentOptions; the rationale for the value lives in sdk.py.
# --------------------------------------------------------------------------

def test_architect_plans_at_low_effort(monkeypatch):
    captured = []
    plan = '[{"chain": "A", "id": 1, "dimension": "d", "prompt": "p"}]'
    monkeypatch.setattr(sdk, "query", _cap_query(captured, result=plan))
    asyncio.run(OracleSDK(chains=1).decompose("q"))
    assert captured[0].effort == "low"


def test_organizer_runs_at_low_effort(monkeypatch):
    captured = []
    monkeypatch.setattr(sdk, "query", _cap_query(captured))
    scouts = [ScoutResult(scout_id=1, chain="A", dimension="d", result_text="finding")]
    asyncio.run(OracleSDK(chains=1)._run_compressor("A", scouts))
    assert captured[0].effort == "low"


def test_scouts_run_without_a_thinking_budget(monkeypatch):
    captured = []
    monkeypatch.setattr(sdk, "query", _cap_query(captured))
    asyncio.run(OracleSDK(chains=1)._run_scout(1, "A", "d", "p"))
    assert captured[0].thinking == {"type": "disabled"}
    # Runaway guard only — healthy Smiths run well under this. Asserting the
    # documented floor rather than the literal: raising it is fine, lowering
    # it past 25 would start truncating real scouts.
    assert captured[0].max_turns >= 25


# --------------------------------------------------------------------------
# Session-token auto-isolation — zero-setup fast path (v4.6.0)
# --------------------------------------------------------------------------

def _write_creds(tmp_path, token="sess-tok", minutes_left=120):
    import json as _json, time as _time
    p = tmp_path / ".credentials.json"
    p.write_text(_json.dumps({"claudeAiOauth": {
        "accessToken": token,
        "expiresAt": (_time.time() + minutes_left * 60) * 1000,
    }}), encoding="utf-8")
    return str(p)


def test_session_token_used_when_fresh(monkeypatch, tmp_path):
    monkeypatch.delenv("ORACLE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(sdk, "_credentials_file", lambda: _write_creds(tmp_path))
    assert sdk._oauth_token() == "sess-tok"


def test_session_token_rejected_near_expiry(monkeypatch, tmp_path):
    monkeypatch.delenv("ORACLE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(sdk, "_credentials_file",
                        lambda: _write_creds(tmp_path, minutes_left=10))
    assert sdk._oauth_token() is None  # < margin -> gated mode, not a dead run


def test_session_token_missing_or_malformed_is_gated(monkeypatch, tmp_path):
    monkeypatch.delenv("ORACLE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(sdk, "_credentials_file", lambda: str(tmp_path / "nope.json"))
    assert sdk._oauth_token() is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(sdk, "_credentials_file", lambda: str(bad))
    assert sdk._oauth_token() is None


def test_explicit_env_token_beats_session_token(monkeypatch, tmp_path):
    monkeypatch.setenv("ORACLE_OAUTH_TOKEN", "explicit-tok")
    monkeypatch.setattr(sdk, "_credentials_file", lambda: _write_creds(tmp_path))
    assert sdk._oauth_token() == "explicit-tok"
