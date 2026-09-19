"""Offline regression tests for durable, caller-managed research rounds."""

import asyncio
import io
import json
from pathlib import Path
import time

import pytest

from claude_oracle import RoundSession, sdk
from claude_oracle import rounds as rounds_module


def plan(topic="Initial evidence"):
    return [{"dimension": topic, "prompt": f"{topic}: investigate angle {i}"} for i in range(10)]


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sdk, "_oauth_token", lambda: None)
    monkeypatch.delenv("GITHUB_PAT", raising=False)

    def unexpected_query(*args, **kwargs):
        pytest.fail("Round tests must never contact a model")

    monkeypatch.setattr(sdk, "query", unexpected_query)


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    async def fake_run(self, question, prompts=None):
        recorded.append({"question": question, "prompts": prompts, "oracle": self})
        self.metrics = sdk.OracleMetrics(start_time=time.time(), scout_count=10, chain_count=1)
        return f"Findings {len(recorded)} — cited evidence"

    monkeypatch.setattr(sdk.OracleSDK, "run", fake_run)
    return recorded


def cli(monkeypatch, args, prompts=None):
    monkeypatch.setattr(sdk.sys, "argv", ["claude-oracle", *args])
    monkeypatch.setattr(sdk.sys, "stdin", io.StringIO(json.dumps(prompts) if prompts is not None else ""))
    asyncio.run(sdk._async_main())


@pytest.mark.parametrize("value", [0, -1, 1.5, True, "3", None])
def test_round_budget_rejected_before_creating_files(value):
    with pytest.raises(ValueError, match="positive integer"):
        RoundSession.create("question", rounds=value, directory="session")
    assert not Path("session").exists()


def test_checkpoint_is_caller_controlled_and_validated():
    session = RoundSession.create("question")
    with pytest.raises(ValueError, match="between zero"):
        session.checkpoint(revision="r1", through_round=1)
    assert session.checkpoint(revision="draft-r0", through_round=0)["through_round"] == 0
    assert session.status()["checkpoint"]["revision"] == "draft-r0"


def test_checkpoint_during_round_survives_terminal_write(monkeypatch):
    session = RoundSession.create("question")
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_run(self, question, prompts=None):
        started.set()
        await release.wait()
        return "Completed evidence"

    monkeypatch.setattr(sdk.OracleSDK, "run", slow_run)

    async def scenario():
        task = asyncio.create_task(session.run_round(plan()))
        await started.wait()
        assert session.checkpoint(revision="during-round", through_round=0)["revision"] == "during-round"
        release.set()
        await task

    asyncio.run(scenario())
    state = session.status()
    assert state["completed_rounds"] == 1
    assert state["checkpoint"]["revision"] == "during-round"


def test_running_round_persists_live_progress(monkeypatch):
    session = RoundSession.create("question")
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_run(self, question, prompts=None):
        started.set()
        self.progress_callback({
            "phase": "research",
            "progress": {
                "scouts": {"completed": 3, "total": 10},
                "organizers": {"completed": 0, "total": 1},
            },
        })
        await release.wait()
        return "Completed evidence"

    monkeypatch.setattr(sdk.OracleSDK, "run", slow_run)

    async def scenario():
        task = asyncio.create_task(session.run_round(plan()))
        await started.wait()
        await asyncio.sleep(0)
        assert session.status()["progress"]["scouts"]["completed"] == 3
        release.set()
        await task

    asyncio.run(scenario())


def test_status_exposes_compatible_defaults_for_v1_state():
    session = RoundSession.create("question")
    state = session.status()
    state["schema_version"] = 1
    (session.path / "session.json").write_text(json.dumps(state), encoding="utf-8")
    restored = RoundSession.open(session.path).status()
    assert restored["research_outcome"] == "unknown"
    assert restored["checkpoint"]["through_round"] == 0
    session = RoundSession.open(session.path)
    session.checkpoint(revision="upgrade", through_round=0)
    assert json.loads((session.path / "session.json").read_text())["schema_version"] == 2


def test_default_session_and_existing_directory_are_preserved():
    session = RoundSession.create("question", directory="session")
    report = session.path / "canonical.md"
    report.write_text("User-authored report", encoding="utf-8")
    assert session.status()["rounds"] == 1
    assert (session.path / ".gitignore").read_text() == "*\n"
    with pytest.raises(FileExistsError):
        RoundSession.create("another question", directory="session")
    assert report.read_text() == "User-authored report"


def test_resume_uses_new_plan_saved_settings_and_immutable_history(calls):
    session = RoundSession.create("A research question", rounds=2, chains=2,
                                  local_tools=True, show_dollars=True)
    canonical = session.path / "canonical.md"
    first = asyncio.run(session.run_round(plan()))
    state = session.status()
    first_folder = session.path / state["attempts"][0]["directory"]
    saved_files = {p.name: p.read_bytes() for p in first_folder.iterdir()}
    assert state["completed_rounds"] == 1
    assert state["status"] == "awaiting_plan"
    assert json.loads(saved_files["metrics.json"])["scout_count"] == 10
    assert state["reported_usage"]["available"] is False
    assert state["reported_usage"]["input_tokens"] is None
    assert saved_files["report.md"].decode("utf-8") == first
    canonical.write_text("Final-draft-quality revision with sources", encoding="utf-8")

    reopened = RoundSession.open(session.path)
    second = asyncio.run(reopened.run_round(plan("Resolve contradiction from round 1")))
    assert reopened.status()["status"] == "rounds_complete"
    assert reopened.status()["completed_rounds"] == 2
    assert "round 2 of 2" in second
    assert len(calls) == 2
    assert calls[1]["question"] == "A research question"
    assert calls[1]["prompts"][0]["prompt"].startswith("Resolve contradiction from round 1")
    assert calls[1]["oracle"].local_tools is True
    assert calls[1]["oracle"].show_dollars is True
    assert calls[1]["oracle"].chains == 2
    assert canonical.read_text() == "Final-draft-quality revision with sources"
    assert len(reopened.status()["artifact_paths"]) == 6
    assert all(not Path(path).is_absolute() for path in reopened.status()["artifact_paths"])
    assert {p.name: p.read_bytes() for p in first_folder.iterdir()} == saved_files
    with pytest.raises(ValueError, match="already finished"):
        asyncio.run(reopened.run_round(plan()))
    assert len(calls) == 2


def test_later_round_requires_a_new_plan(calls):
    session = RoundSession.create("question", rounds=2)
    asyncio.run(session.run_round(plan()))
    with pytest.raises(ValueError, match="fresh prompt array"):
        asyncio.run(session.run_round())
    assert len(calls) == 1
    assert session.status()["completed_rounds"] == 1


def test_failed_attempt_is_preserved_and_retry_does_not_consume_round(monkeypatch):
    session = RoundSession.create("question", rounds=2)
    attempts = 0

    async def flaky_run(self, question, prompts=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transport disconnected")
        return "Recovered findings"

    monkeypatch.setattr(sdk.OracleSDK, "run", flaky_run)
    with pytest.raises(RuntimeError, match="transport disconnected"):
        asyncio.run(session.run_round(plan()))
    assert session.status()["completed_rounds"] == 0
    assert session.status()["status"] == "failed"
    with pytest.raises(ValueError, match="fresh prompt array"):
        asyncio.run(session.run_round())
    asyncio.run(RoundSession.open(session.path).run_round(plan("Retry")))
    state = session.status()
    assert state["completed_rounds"] == 1
    assert [a["round"] for a in state["attempts"]] == [1, 1]
    assert [a["status"] for a in state["attempts"]] == ["failed", "completed"]
    assert state["attempts"][0]["error"] == "transport disconnected"
    assert all((session.path / a["directory"] / "prompts.json").exists() for a in state["attempts"])


@pytest.mark.parametrize("errors, expected", [
    ((0, 0), "complete"),
    ((1, 0), "partial"),
])
def test_completed_round_derives_research_outcome(monkeypatch, errors, expected):
    session = RoundSession.create("question")

    async def fake_run(self, question, prompts=None):
        self.metrics = sdk.OracleMetrics(
            start_time=time.time(),
            scout_count=10,
            scout_errors=errors[0],
            compressor_count=1,
            compressor_errors=errors[1],
            chain_count=1,
        )
        return "Returned report"

    monkeypatch.setattr(sdk.OracleSDK, "run", fake_run)
    asyncio.run(session.run_round(plan()))
    assert session.status()["research_outcome"] == expected


def test_round_metrics_and_reported_usage_persist_cache_fields(monkeypatch):
    """Cache tokens are most of an organizer's billable input, so they have to
    survive into both the round's metrics file and the session's running total."""
    session = RoundSession.create("question")

    async def fake_run(self, question, prompts=None):
        self.metrics = sdk.OracleMetrics(start_time=time.time(), scout_count=10, chain_count=1)
        self.metrics.phase_usage["scout"] = sdk.UsageStats(
            input_tokens=10, output_tokens=20,
            cache_read_input_tokens=30, cache_creation_input_tokens=40,
            quota_units=123.5, usage_observed=True, usage_available=True,
        )
        return "Findings"

    monkeypatch.setattr(sdk.OracleSDK, "run", fake_run)
    asyncio.run(session.run_round(plan()))

    metrics_file = session.path / "rounds" / "round-001-attempt-001" / "metrics.json"
    total = json.loads(metrics_file.read_text(encoding="utf-8"))["total_usage"]
    assert total["cache_read_input_tokens"] == 30
    assert total["cache_creation_input_tokens"] == 40

    reported = session.status()["reported_usage"]
    assert reported["available"] is True
    assert reported["cache_read_input_tokens"] == 30
    assert reported["quota_units"] == 123.5


def test_all_scouts_failed_is_a_failed_outcome(monkeypatch):
    session = RoundSession.create("question")

    async def fake_run(self, question, prompts=None):
        self.metrics = sdk.OracleMetrics(
            start_time=time.time(), scout_count=10, scout_errors=10, chain_count=1
        )
        return "ERROR: All Smiths failed"

    monkeypatch.setattr(sdk.OracleSDK, "run", fake_run)
    asyncio.run(session.run_round(plan()))
    assert session.status()["research_outcome"] == "failed"


def test_failed_round_preserves_observed_progress(monkeypatch):
    session = RoundSession.create("question")

    async def fake_run(self, question, prompts=None):
        self.metrics = sdk.OracleMetrics(
            start_time=time.time(), scout_count=3, compressor_count=1, chain_count=1
        )
        raise RuntimeError("transport disconnected")

    monkeypatch.setattr(sdk.OracleSDK, "run", fake_run)
    with pytest.raises(RuntimeError, match="transport disconnected"):
        asyncio.run(session.run_round(plan()))
    progress = session.status()["progress"]
    assert progress == {
        "scouts": {"completed": 3, "total": 10},
        "organizers": {"completed": 1, "total": 1},
    }


def test_running_round_allows_status_and_report_edits_but_rejects_second_writer(monkeypatch):
    session = RoundSession.create("question", rounds=2)

    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()

        async def slow_run(self, question, prompts=None):
            started.set()
            await finish.wait()
            return "Completed evidence"

        monkeypatch.setattr(sdk.OracleSDK, "run", slow_run)
        task = asyncio.create_task(session.run_round(plan()))
        await started.wait()
        try:
            assert RoundSession.open(session.path).status()["status"] == "running"
            (session.path / "canonical.md").write_text("Strengthened while waiting", encoding="utf-8")
            with pytest.raises(RuntimeError, match="already has a round running"):
                await RoundSession.open(session.path).run_round(plan())
        finally:
            finish.set()
            await task
        assert (session.path / "canonical.md").read_text() == "Strengthened while waiting"
        assert len(session.status()["attempts"]) == 1

    asyncio.run(scenario())


def test_cancellation_releases_lock_and_preserves_round_budget(monkeypatch, calls):
    session = RoundSession.create("question")

    async def cancelled_run(self, question, prompts=None):
        raise asyncio.CancelledError()

    with monkeypatch.context() as patch:
        patch.setattr(sdk.OracleSDK, "run", cancelled_run)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(session.run_round(plan()))
    assert session.status()["completed_rounds"] == 0
    assert session.status()["attempts"][0]["error"] == "CancelledError"
    asyncio.run(session.run_round(plan()))
    assert session.status()["status"] == "rounds_complete"


def test_resume_after_crash_preserves_interrupted_and_orphan_attempts(calls):
    session = RoundSession.create("question")
    state = session.status()
    first = "rounds/round-001-attempt-001"
    orphan = session.path / "rounds/round-001-attempt-002"
    (session.path / first).mkdir()
    orphan.mkdir()
    (orphan / "report.md").write_text("Preserved orphan", encoding="utf-8")
    state.update(status="running", attempts=[{
        "round": 1, "attempt": 1, "directory": first, "status": "running"
    }])
    rounds_module._write_json(session.path / "session.json", state)
    asyncio.run(RoundSession.open(session.path).run_round(plan()))
    state = session.status()
    assert state["attempts"][0]["status"] == "interrupted"
    assert state["attempts"][1]["attempt"] == 3
    assert (orphan / "report.md").read_text() == "Preserved orphan"


@pytest.mark.parametrize("prompts", [
    [],
    [{"id": 0, "chain": "A", "prompt": "question"}],
    [{"id": True, "chain": "A", "prompt": "question"}],
    [{"id": 1, "chain": c, "prompt": "question"} for c in "AB"],
    [{"chain": str(i), "prompt": "question"} for i in range(9)],
])
def test_invalid_plan_does_not_launch_or_consume_round(calls, prompts):
    session = RoundSession.create("question")
    with pytest.raises(ValueError):
        asyncio.run(session.run_round(prompts))
    assert calls == []
    assert session.status()["attempts"] == []


def test_first_round_architect_plan_and_usage_are_saved(monkeypatch):
    session = RoundSession.create("question", rounds=2)

    async def fake_query(*args, **kwargs):
        class Message:
            result = json.dumps(sdk._normalize_prompts(plan()))
            usage = {"input_tokens": 12, "output_tokens": 34}
        yield Message()

    async def fake_scout(self, prompts):
        return []

    monkeypatch.setattr(sdk, "query", fake_query)
    monkeypatch.setattr(sdk.OracleSDK, "scout", fake_scout)
    asyncio.run(session.run_round())
    folder = session.path / session.status()["attempts"][0]["directory"]
    assert len(json.loads((folder / "prompts.json").read_text())) == 10
    metrics = json.loads((folder / "metrics.json").read_text())
    assert metrics["phase_usage"]["decompose"]["input_tokens"] == 12
    # Returned model-error reports remain visible and count as returned rounds.
    assert "ERROR: All Smiths" in (folder / "report.md").read_text()
    assert session.status()["completed_rounds"] == 1


@pytest.mark.parametrize("args", [[], ["--rounds", "1"]])
def test_single_round_cli_keeps_existing_output_and_creates_no_session(monkeypatch, calls, capsys, args):
    cli(monkeypatch, [*args, "question"])
    assert len(calls) == 1
    assert capsys.readouterr().out.strip() == "Findings 1 — cited evidence"
    assert not Path("research").exists()


@pytest.mark.parametrize("value", ["0", "-2", "1.5", "abc"])
def test_cli_rejects_invalid_rounds_before_models(monkeypatch, calls, value):
    with pytest.raises(SystemExit) as exc:
        cli(monkeypatch, ["--rounds", value, "question"])
    assert exc.value.code == 2
    assert calls == []


def test_cli_round_creation_resume_and_status_without_reading_stdin(monkeypatch, calls, capsys):
    cli(monkeypatch, ["--rounds", "2", "--session-dir", "session", "--local", "question"], plan())
    assert len(calls) == 1
    assert RoundSession.open("session").status()["status"] == "awaiting_plan"
    capsys.readouterr()

    class UnreadableInput:
        def read(self):
            pytest.fail("Status must not read stdin")

    monkeypatch.setattr(sdk.sys, "argv", ["claude-oracle", "--session-status", "session"])
    monkeypatch.setattr(sdk.sys, "stdin", UnreadableInput())
    asyncio.run(sdk._async_main())
    assert json.loads(capsys.readouterr().out)["completed_rounds"] == 1
    assert len(calls) == 1
    cli(monkeypatch, ["--resume", "session"], plan("Follow up"))
    assert len(calls) == 2
    assert calls[1]["oracle"].local_tools is True
    assert RoundSession.open("session").status()["status"] == "rounds_complete"


@pytest.mark.parametrize("args", [
    [], ["--rounds", "3"], ["--rounds", "1"], ["--chains", "1"],
    ["--local"], ["new question"],
])
def test_cli_resume_requires_plan_and_rejects_configuration_overrides(monkeypatch, calls, args):
    RoundSession.create("question", directory="session", rounds=2)
    with pytest.raises(SystemExit) as exc:
        cli(monkeypatch, ["--resume", "session", *args], plan() if args else None)
    assert exc.value.code == 2
    assert calls == []


def test_cli_help_describes_live_session_status(monkeypatch, capsys):
    monkeypatch.setattr(sdk.sys, "argv", ["claude-oracle", "--help"])
    with pytest.raises(SystemExit) as exc:
        asyncio.run(sdk._async_main())
    assert exc.value.code == 0
    assert "live phase/progress" in capsys.readouterr().out
