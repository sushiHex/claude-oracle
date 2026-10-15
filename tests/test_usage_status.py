"""Issue #11: session status must stay a trustworthy input when some usage is unknown.

Exact usage fields keep their contract (exact, or null when any execution's usage is
unknown). Alongside them, `recorded` carries what the provider did report, with counts
that say how complete it is, so one failed scout no longer blanks a whole session.
"""

import asyncio
import json
import time

import pytest

from claude_oracle import RoundSession, sdk
from claude_oracle import rounds as rounds_module


def plan(topic="Initial evidence"):
    return [{"dimension": topic, "prompt": f"{topic}: investigate angle {i}"} for i in range(10)]


def known(tokens):
    return sdk.UsageStats(input_tokens=tokens, output_tokens=tokens, quota_units=float(tokens),
                          usage_observed=True, usage_available=True)


def unknown(tokens=0):
    """Usage the provider partly reported: some known tokens, availability lost."""
    stats = sdk.UsageStats(input_tokens=tokens, output_tokens=tokens, quota_units=float(tokens),
                           usage_observed=True, usage_available=False)
    return stats


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sdk, "_oauth_token", lambda: None)
    monkeypatch.delenv("GITHUB_PAT", raising=False)

    def unexpected_query(*args, **kwargs):
        pytest.fail("Usage tests must never contact a model")

    monkeypatch.setattr(sdk, "query", unexpected_query)


def run_with_usage(monkeypatch, *usages):
    """Each successive round reports the next usage as its whole scout phase."""
    pending = list(usages)

    async def fake_run(self, question, prompts=None):
        self.metrics = sdk.OracleMetrics(start_time=time.time(), scout_count=10, chain_count=1)
        self.metrics.phase_usage["scout"] = pending.pop(0)
        return "Findings"

    monkeypatch.setattr(sdk.OracleSDK, "run", fake_run)


# --- 1. one unknown execution no longer blanks the session ------------------------

def test_unknown_round_keeps_later_rounds_visible_in_recorded_usage(monkeypatch):
    session = RoundSession.create("question", rounds=3)
    run_with_usage(monkeypatch, unknown(100), known(200), known(300))
    for topic in ("one", "two", "three"):
        asyncio.run(session.run_round(plan(topic)))

    usage = session.status()["reported_usage"]
    assert usage["available"] is False and usage["input_tokens"] is None  # exact total unknowable
    assert usage["recorded"]["input_tokens"] == 600
    assert usage["recorded"]["quota_units"] == 600.0
    assert usage["recorded"]["executions"] == 3
    assert usage["recorded"]["incomplete_executions"] == 1


def test_all_known_rounds_report_exact_totals_and_matching_recorded(monkeypatch):
    session = RoundSession.create("question", rounds=2)
    run_with_usage(monkeypatch, known(10), known(20))
    asyncio.run(session.run_round(plan("one")))
    asyncio.run(session.run_round(plan("two")))

    usage = session.status()["reported_usage"]
    assert usage["available"] is True and usage["input_tokens"] == 30
    assert usage["recorded"]["input_tokens"] == 30
    assert usage["recorded"]["incomplete_executions"] == 0


def test_attempt_snapshot_keeps_partial_numbers_as_recorded(monkeypatch):
    session = RoundSession.create("question")
    run_with_usage(monkeypatch, unknown(70))
    asyncio.run(session.run_round(plan()))
    snapshot = session.status()["attempts"][0]["usage"]
    assert snapshot["available"] is False and snapshot["input_tokens"] is None
    assert snapshot["recorded"]["input_tokens"] == 70


def test_interrupted_attempt_counts_as_incomplete_not_as_zero(monkeypatch):
    session = RoundSession.create("question", rounds=2)
    run_with_usage(monkeypatch, known(50), known(60))
    asyncio.run(session.run_round(plan("one")))
    state = session.status()
    state["status"] = "running"
    state["attempts"].append({"round": 2, "attempt": 1, "directory": "rounds/round-002-attempt-001",
                              "status": "running"})
    (session.path / "rounds/round-002-attempt-001").mkdir()
    rounds_module._write_json(session.path / "session.json", state)

    asyncio.run(RoundSession.open(session.path).run_round(plan("two")))
    usage = session.status()["reported_usage"]
    assert usage["available"] is False
    assert usage["recorded"]["input_tokens"] == 110
    assert usage["recorded"]["executions"] == 3
    assert usage["recorded"]["incomplete_executions"] == 1


def test_usage_is_counted_once_when_the_terminal_write_fails(monkeypatch):
    """The old running total was mutated, then re-added by the failure handler."""
    session = RoundSession.create("question")
    run_with_usage(monkeypatch, known(40))
    original = RoundSession._persist_state
    writes = {"n": 0}

    def flaky_persist(self, state):
        writes["n"] += 1
        if state.get("status") == "awaiting_plan" or state.get("status") == "rounds_complete":
            if not writes.get("failed"):
                writes["failed"] = True
                raise OSError("disk full")
        return original(self, state)

    monkeypatch.setattr(RoundSession, "_persist_state", flaky_persist)
    with pytest.raises(OSError):
        asyncio.run(session.run_round(plan()))
    usage = session.status()["reported_usage"]
    assert usage["recorded"]["input_tokens"] == 40
    assert usage["recorded"]["executions"] == 1


def test_legacy_v480_records_without_recorded_block_are_read_conservatively():
    known_snapshot = {"input_tokens": 5, "output_tokens": 5, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0, "cost_usd": 0.0, "quota_units": 5.0,
                      "available": True}
    null_snapshot = {**{k: None for k in known_snapshot if k != "available"}, "available": False}
    state = {"attempts": [{"status": "completed", "usage": known_snapshot},
                          {"status": "failed", "usage": null_snapshot}],
             "recoveries": []}
    usage = RoundSession._session_usage(state)
    assert usage["available"] is False
    assert usage["recorded"]["input_tokens"] == 5
    assert usage["recorded"]["incomplete_executions"] == 1


def test_recovery_that_never_started_a_model_is_not_unknown_spend(monkeypatch):
    session = RoundSession.create("question")
    run_with_usage(monkeypatch, known(10))
    asyncio.run(session.run_round(plan()))
    folder = session.path / session.status()["attempts"][0]["directory"] / rounds_module.EVIDENCE_DIR
    folder.mkdir(exist_ok=True)
    writer = rounds_module.ScoutEvidenceWriter(folder)
    prompts = sdk._normalize_prompts(plan())
    writer.plan_ready(prompts)
    result = sdk.ScoutResult(scout_id=1, chain="A", dimension="d", result_text="found", usage=known(1))
    writer.scout_finished(prompts[0], result, 1)

    def broken_init(self, *args, **kwargs):
        raise RuntimeError("cannot construct")

    monkeypatch.setattr(sdk.OracleSDK, "__init__", broken_init)
    with pytest.raises(RuntimeError, match="cannot construct"):
        asyncio.run(session.recover_organizers())
    usage = session.status()["reported_usage"]
    assert usage["available"] is True and usage["input_tokens"] == 10


def test_recovery_failing_before_any_dispatch_is_not_unknown_spend(monkeypatch):
    session = RoundSession.create("question")
    run_with_usage(monkeypatch, known(10))
    asyncio.run(session.run_round(plan()))
    folder = session.path / session.status()["attempts"][0]["directory"] / rounds_module.EVIDENCE_DIR
    folder.mkdir(exist_ok=True)
    writer = rounds_module.ScoutEvidenceWriter(folder)
    prompts = sdk._normalize_prompts(plan())
    writer.plan_ready(prompts)
    writer.scout_finished(prompts[0], sdk.ScoutResult(scout_id=1, chain="A", dimension="d",
                                                      result_text="found", usage=known(1)), 1)

    async def fails_before_organizers(self, scout_results):
        raise RuntimeError("stderr closed")

    monkeypatch.setattr(sdk.OracleSDK, "reorganize", fails_before_organizers)
    with pytest.raises(RuntimeError, match="stderr closed"):
        asyncio.run(session.recover_organizers())
    assert session.status()["reported_usage"]["available"] is True


def test_new_session_file_already_has_the_recorded_block():
    session = RoundSession.create("question")
    on_disk = json.loads((session.path / "session.json").read_text(encoding="utf-8"))
    assert on_disk["reported_usage"]["recorded"]["executions"] == 0


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(recoveries=None),
    lambda s: s.update(attempts=[None]),
])
def test_malformed_containers_get_the_clean_rejection(mutate):
    session = RoundSession.create("question")
    state = json.loads((session.path / "session.json").read_text(encoding="utf-8"))
    mutate(state)
    (session.path / "session.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid Oracle round session state"):
        RoundSession.open(session.path).status()


@pytest.mark.parametrize("snapshot", [
    {"available": True},                                        # numbers missing
    {"available": True, "input_tokens": "10", "output_tokens": 1, "cache_read_input_tokens": 0,
     "cache_creation_input_tokens": 0, "cost_usd": 0, "quota_units": 0},  # a string
    {"available": True, "input_tokens": True, "output_tokens": 1, "cache_read_input_tokens": 0,
     "cache_creation_input_tokens": 0, "cost_usd": 0, "quota_units": 0},  # a bool
])
def test_malformed_usage_record_counts_as_incomplete_not_exact(snapshot):
    usage = RoundSession._session_usage({"attempts": [{"status": "completed", "usage": snapshot}],
                                         "recoveries": []})
    assert usage["available"] is False
    assert usage["recorded"]["incomplete_executions"] == 1
    assert usage["recorded"]["input_tokens"] == 0


def test_architect_usage_survives_an_unusable_plan(monkeypatch):
    session = RoundSession.create("question")

    async def fake_query(*args, **kwargs):
        class Message:
            result = "not a JSON plan"
            usage = {"input_tokens": 12, "output_tokens": 34}
        yield Message()

    monkeypatch.setattr(sdk, "query", fake_query)
    with pytest.raises(RuntimeError, match="valid prompt list"):
        asyncio.run(session.run_round())
    attempt = session.status()["attempts"][0]
    assert attempt["usage"]["available"] is True
    assert attempt["usage"]["input_tokens"] == 12


# --- engine accounting that fed the aggregate wrong numbers ------------------------

def test_retry_keeps_the_first_attempts_usage(monkeypatch):
    oracle = sdk.OracleSDK(chains=1)
    attempts = {}

    async def fake_scout(self, scout_id, chain, dimension, prompt, on_started=None):
        attempts[scout_id] = attempts.get(scout_id, 0) + 1
        failed = scout_id == 1 and attempts[scout_id] == 1
        return sdk.ScoutResult(scout_id=scout_id, chain=chain, dimension=dimension,
                               result_text="" if failed else "found",
                               error="empty" if failed else None, usage=known(10))

    monkeypatch.setattr(sdk.OracleSDK, "_run_scout", fake_scout)
    asyncio.run(oracle.scout(sdk._normalize_prompts(plan())))
    scout_usage = oracle.metrics.phase_usage["scout"]
    assert attempts[1] == 2
    assert scout_usage.input_tokens == 110  # ten effective results + the superseded attempt
    assert scout_usage.usage_available is True


def test_skipped_organizer_does_not_make_usage_unknown(monkeypatch):
    oracle = sdk.OracleSDK(chains=2)

    async def fake_compress(self, chain, scouts):
        return sdk.CompressorResult(chain=chain, summary="organized", usage=known(5))

    monkeypatch.setattr(sdk.OracleSDK, "_run_compressor", fake_compress)
    scouts = [sdk.ScoutResult(scout_id=1, chain="A", dimension="d", result_text="ok", usage=known(1)),
              sdk.ScoutResult(scout_id=2, chain="B", dimension="d", result_text="", error="x",
                              usage=known(1))]
    asyncio.run(oracle.compress(scouts))
    assert oracle.metrics.phase_usage["compress"].usage_available is True


# --- 2. the report footer tells the same story as session.json ---------------------

def test_footer_marks_incomplete_usage_instead_of_printing_it_as_exact():
    oracle = sdk.OracleSDK(chains=1)
    oracle.metrics = sdk.OracleMetrics(start_time=time.time())
    oracle.metrics.phase_usage["scout"] = unknown(100)
    footer = oracle._metrics_footer()
    assert "incomplete" in footer.lower()
    assert "Total tokens:" not in footer
    assert "Recorded tokens: 200" in footer


def test_footer_unchanged_when_usage_is_complete():
    oracle = sdk.OracleSDK(chains=1)
    oracle.metrics = sdk.OracleMetrics(start_time=time.time())
    oracle.metrics.phase_usage["scout"] = known(100)
    footer = oracle._metrics_footer()
    assert "Total tokens: 200" in footer and "incomplete" not in footer.lower()


# --- 3. planning is not "zero work scheduled" ---------------------------------------

def test_architect_phase_reports_planning_with_unknown_totals(monkeypatch):
    session = RoundSession.create("question")
    seen = {}

    async def fake_decompose(self, question):
        seen.update(session.status())
        return sdk._normalize_prompts(plan())

    async def fake_scout(self, prompts):
        return []

    monkeypatch.setattr(sdk.OracleSDK, "decompose", fake_decompose)
    monkeypatch.setattr(sdk.OracleSDK, "scout", fake_scout)
    asyncio.run(session.run_round())
    assert seen["current_phase"] == "planning"
    assert seen["progress"]["scouts"]["total"] is None
    assert seen["progress"]["organizers"]["total"] is None
    assert session.status()["progress"]["scouts"]["total"] == 10


def test_failed_planning_keeps_totals_unknown(monkeypatch):
    session = RoundSession.create("question")

    async def failing_decompose(self, question):
        raise RuntimeError("architect returned invalid JSON")

    monkeypatch.setattr(sdk.OracleSDK, "decompose", failing_decompose)
    with pytest.raises(RuntimeError, match="invalid JSON"):
        asyncio.run(session.run_round())
    progress = session.status()["progress"]
    assert progress["scouts"]["total"] is None
    assert progress["organizers"]["total"] is None


def test_status_json_round_trips_null_totals():
    session = RoundSession.create("question")
    state = session.status()
    state["progress"] = {"scouts": {"completed": 0, "total": None, "retained": 0},
                         "organizers": {"completed": 0, "total": None}}
    rounds_module._write_json(session.path / "session.json", state)
    assert json.loads((session.path / "session.json").read_text())["progress"]["scouts"]["total"] is None
    assert RoundSession.open(session.path).status()["progress"]["scouts"]["total"] is None
