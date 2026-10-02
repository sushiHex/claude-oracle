"""Offline regression tests for retained scout evidence and organizer-only recovery.

These drive the real OracleSDK.run -> scout -> compress pipeline with fake Smiths
and Andersons, so persistence is exercised at the actual seam between phases.
"""

import asyncio
import io
import json
from pathlib import Path

import pytest

from claude_oracle import RoundSession, sdk
from claude_oracle import rounds as rounds_module

SCOUTS = {"A": (1, 2, 3), "B": (4, 5, 6)}


def plan():
    return [
        {"id": scout_id, "chain": chain, "dimension": f"{chain} angle {scout_id}",
         "prompt": f"Investigate {chain}-{scout_id}"}
        for chain, ids in SCOUTS.items() for scout_id in ids
    ]


def finding(scout_id: int) -> str:
    # Non-ASCII and markdown on purpose: evidence must be stored verbatim.
    return f"Finding #{scout_id}: 42% faster — see https://example.test/{scout_id} ✓\n\n| a | b |"


def usage(tokens: int) -> sdk.UsageStats:
    return sdk.UsageStats(input_tokens=tokens, output_tokens=tokens, quota_units=float(tokens),
                          usage_observed=True, usage_available=True)


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # A token selects ungated, unstaggered dispatch; no subprocess ever starts
    # because _run_scout and _run_compressor are replaced below.
    monkeypatch.setattr(sdk, "_oauth_token", lambda: "offline-test-token")
    monkeypatch.delenv("GITHUB_PAT", raising=False)

    def unexpected_query(*args, **kwargs):
        pytest.fail("Evidence tests must never contact a model")

    monkeypatch.setattr(sdk, "query", unexpected_query)


class Fleet:
    """Fake Smiths and Andersons that record exactly what each one received."""

    def __init__(self, monkeypatch, *, fail_first=(), fail_always=(), block=(),
                 organizer_error=None, organizer_check=None, scout_check=None):
        self.scout_calls: list[int] = []
        self.organizer_inputs: dict[str, list] = {}
        self.fail_first, self.fail_always, self.block = set(fail_first), set(fail_always), set(block)
        self.organizer_error = organizer_error
        self.organizer_check = organizer_check
        self.scout_check = scout_check
        self.release = None
        fleet = self

        async def fake_scout(oracle, scout_id, chain, dimension, prompt, on_started=None):
            if on_started:
                on_started()
            if fleet.scout_check:
                fleet.scout_check(scout_id)
            fleet.scout_calls.append(scout_id)
            attempt = fleet.scout_calls.count(scout_id)
            if scout_id in fleet.block:
                await fleet.release.wait()
            status, error, text = "done", None, finding(scout_id)
            if scout_id in fleet.fail_always or (scout_id in fleet.fail_first and attempt == 1):
                status, error, text = "error", f"transport reset (attempt {attempt})", ""
            oracle._active_scouts[scout_id] = status
            oracle._emit_progress("research")
            return sdk.ScoutResult(scout_id=scout_id, chain=chain, dimension=dimension,
                                   result_text=text, error=error, duration_ms=7,
                                   usage=usage(100 * scout_id) if not error else sdk._unknown_usage())

        async def fake_organizer(oracle, chain, scout_results):
            fleet.organizer_inputs.setdefault(chain, []).append(
                [(r.scout_id, r.chain, r.result_text, r.error) for r in scout_results]
            )
            if fleet.organizer_check:
                fleet.organizer_check(chain)
            if fleet.organizer_error is not None:
                raise fleet.organizer_error
            return sdk.CompressorResult(chain=chain, summary=f"Organized chain {chain}",
                                        duration_ms=5, usage=usage(1000))

        monkeypatch.setattr(sdk.OracleSDK, "_run_scout", fake_scout)
        monkeypatch.setattr(sdk.OracleSDK, "_run_compressor", fake_organizer)


def evidence_dir(session: RoundSession, index: int = 0) -> Path:
    return session.path / session.status()["attempts"][index]["directory"] / "scouts"


def records(folder: Path) -> dict[str, dict]:
    return {p.name: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(folder.glob("scout-*.json"))}


async def interrupt_after_records(session: RoundSession, fleet: Fleet, count: int) -> None:
    """Cancel a running round once `count` evidence records exist (bounded wait)."""
    fleet.release = asyncio.Event()
    task = asyncio.create_task(session.run_round(plan()))
    folder = session.path / "rounds/round-001-attempt-001/scouts"
    try:
        for _ in range(500):
            if len(records(folder)) >= count or task.done():
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail(f"expected {count} evidence records before organization")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def killed_during_organization(monkeypatch) -> tuple[RoundSession, Fleet]:
    """The field report: every scout succeeds, then the process dies in Phase 2."""
    session = RoundSession.create("question", chains=2)
    seen_on_disk = []

    def check(chain):
        # Evidence must already be durable when organization starts.
        seen_on_disk.append(len(records(evidence_dir(session))))
        assert (evidence_dir(session) / "manifest.json").exists()

    fleet = Fleet(monkeypatch, organizer_check=check, organizer_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(session.run_round(plan()))
    assert seen_on_disk and all(n == 6 for n in seen_on_disk)
    return session, fleet


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def test_scout_evidence_is_durable_before_organization_starts(monkeypatch):
    session, _ = killed_during_organization(monkeypatch)
    saved = records(evidence_dir(session))
    assert sorted(saved) == [f"scout-{i:03d}-attempt-1.json" for i in range(1, 7)]
    prompts = {p["id"]: p for p in json.loads(
        (evidence_dir(session).parent / "prompts.json").read_text(encoding="utf-8"))}
    one = saved["scout-004-attempt-1.json"]
    assert one["schema"] == "claude-oracle/scout-evidence" and one["schema_version"] == 1
    assert one["scout_id"] == 4 and one["chain"] == "B" and one["dimension"] == "B angle 4"
    assert one["prompt"] == prompts[4]["prompt"]  # the effective prompt, suffix included
    assert one["status"] == "succeeded" and one["error"] is None
    assert one["result_text"] == finding(4)  # verbatim, never truncated
    assert one["duration_ms"] == 7 and one["recorded_at"]
    assert one["usage"]["available"] is True and one["usage"]["input_tokens"] == 400
    manifest = json.loads((evidence_dir(session) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is True
    assert manifest["planned_scouts"] == [1, 2, 3, 4, 5, 6]
    assert manifest["effective"]["4"] == {"record": "scout-004-attempt-1.json", "status": "succeeded"}


def test_status_distinguishes_scouts_that_ran_from_evidence_retained(monkeypatch):
    session, _ = killed_during_organization(monkeypatch)
    progress = session.status()["progress"]
    assert progress["scouts"] == {"completed": 6, "total": 6, "retained": 6}
    assert progress["organizers"]["completed"] == 0


def test_failed_and_retried_scouts_keep_distinct_records(monkeypatch):
    session = RoundSession.create("question", chains=2)
    Fleet(monkeypatch, fail_first={2}, fail_always={3})
    asyncio.run(session.run_round(plan()))
    saved = records(evidence_dir(session))
    assert saved["scout-002-attempt-1.json"]["status"] == "failed"
    assert saved["scout-002-attempt-1.json"]["usage"]["available"] is False
    assert saved["scout-002-attempt-2.json"]["result_text"] == finding(2)
    assert saved["scout-003-attempt-2.json"]["error"] == "transport reset (attempt 2)"
    manifest = json.loads((evidence_dir(session) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["effective"]["2"] == {"record": "scout-002-attempt-2.json", "status": "succeeded"}
    assert manifest["effective"]["3"] == {"record": "scout-003-attempt-2.json", "status": "failed"}
    state = session.status()
    assert state["progress"]["scouts"]["retained"] == 5
    assert "rounds/round-001-attempt-001/scouts/manifest.json" in state["artifact_paths"]


def test_interrupted_scouting_leaves_a_readable_set_marked_incomplete(monkeypatch):
    session = RoundSession.create("question", chains=2)
    fleet = Fleet(monkeypatch, block={6})
    asyncio.run(interrupt_after_records(session, fleet, 5))
    folder = evidence_dir(session)
    assert len(records(folder)) == 5
    assert not (folder / "manifest.json").exists()  # no manifest == incomplete set
    assert session.status()["progress"]["scouts"]["retained"] == 5
    assert fleet.organizer_inputs == {}


def test_evidence_write_failure_never_aborts_research(monkeypatch):
    session = RoundSession.create("question", chains=2)
    Fleet(monkeypatch)

    def disk_full(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(rounds_module.ScoutEvidenceWriter, "_write_record", disk_full)
    report = asyncio.run(session.run_round(plan()))
    assert "Organized chain A" in report
    assert session.status()["completed_rounds"] == 1


# ---------------------------------------------------------------------------
# Organizer-only recovery
# ---------------------------------------------------------------------------

def forbid_research_dispatch(monkeypatch):
    async def no_scouts(*args, **kwargs):
        pytest.fail("Recovery must not dispatch scouts")

    async def no_architect(*args, **kwargs):
        pytest.fail("Recovery must not run the Architect")

    monkeypatch.setattr(sdk.OracleSDK, "scout", no_scouts)
    monkeypatch.setattr(sdk.OracleSDK, "decompose", no_architect)


def test_recovery_replays_organizers_from_saved_evidence_only(monkeypatch):
    session, _ = killed_during_organization(monkeypatch)
    canonical = (session.path / "canonical.md").read_bytes()
    attempt = session.path / session.status()["attempts"][0]["directory"]
    before = {p.relative_to(attempt): p.read_bytes() for p in attempt.rglob("*") if p.is_file()}

    fleet = Fleet(monkeypatch)
    forbid_research_dispatch(monkeypatch)
    report = asyncio.run(session.recover_organizers())

    # Chain isolation: each organizer saw only its own chain's saved findings.
    assert {c: [s[0] for s in calls[0]] for c, calls in fleet.organizer_inputs.items()} == \
        {"A": [1, 2, 3], "B": [4, 5, 6]}
    assert fleet.organizer_inputs["B"][0][0] == (4, "B", finding(4), None)
    recovered = attempt / "recovery-001"
    assert (recovered / "report.md").read_text(encoding="utf-8") == report
    assert "Organized chain A" in report and "Organized chain B" in report
    assert json.loads((recovered / "metrics.json").read_text(encoding="utf-8"))["compressor_count"] == 2
    # Nothing earlier is rewritten; the canonical report and round budget are untouched.
    assert (session.path / "canonical.md").read_bytes() == canonical
    assert {p: attempt.joinpath(p).read_bytes() for p in before} == before
    state = session.status()
    assert state["completed_rounds"] == 0
    [recovery] = state["recoveries"]
    assert recovery["attempt"] == "rounds/round-001-attempt-001"
    assert recovery["directory"] == "rounds/round-001-attempt-001/recovery-001"
    assert recovery["status"] == "completed" and recovery["complete_evidence"] is True
    assert recovery["usage"]["available"] is True and recovery["usage"]["input_tokens"] == 2000
    assert "rounds/round-001-attempt-001/recovery-001/report.md" in state["artifact_paths"]


def test_repeated_recovery_writes_a_new_artifact(monkeypatch):
    session, _ = killed_during_organization(monkeypatch)
    Fleet(monkeypatch)
    forbid_research_dispatch(monkeypatch)
    first = asyncio.run(session.recover_organizers())
    second = asyncio.run(session.recover_organizers())
    attempt = session.path / "rounds/round-001-attempt-001"
    assert (attempt / "recovery-001/report.md").read_text(encoding="utf-8") == first
    assert (attempt / "recovery-002/report.md").read_text(encoding="utf-8") == second
    assert [r["directory"].rsplit("/", 1)[1] for r in session.status()["recoveries"]] == \
        ["recovery-001", "recovery-002"]


def test_recovery_from_an_incomplete_set_names_the_missing_scouts(monkeypatch):
    session = RoundSession.create("question", chains=2)
    asyncio.run(interrupt_after_records(session, Fleet(monkeypatch, block={6}), 5))
    fleet = Fleet(monkeypatch)
    forbid_research_dispatch(monkeypatch)
    report = asyncio.run(session.recover_organizers())
    assert "incomplete" in report.lower() and "#6" in report
    [(sid, chain, text, error)] = [s for s in fleet.organizer_inputs["B"][0] if s[0] == 6]
    assert text == "" and "not retained" in error
    assert session.status()["recoveries"][0]["complete_evidence"] is False


def test_organizer_failure_keeps_evidence_and_recovery_completes_it(monkeypatch):
    session = RoundSession.create("question", chains=2)
    Fleet(monkeypatch, organizer_error=RuntimeError("organizer overloaded"))
    first = asyncio.run(session.run_round(plan()))
    assert "RAW SMITH FALLBACK" in first  # existing in-process fallback still works
    assert json.loads((evidence_dir(session) / "manifest.json").read_text())["complete"] is True
    Fleet(monkeypatch)
    forbid_research_dispatch(monkeypatch)
    recovered = asyncio.run(session.recover_organizers())
    assert "Organized chain A" in recovered


def test_recovery_without_retained_evidence_launches_nothing(monkeypatch):
    session = RoundSession.create("question")

    async def failing_run(self, question, prompts=None):
        raise RuntimeError("died before any scout finished")

    monkeypatch.setattr(sdk.OracleSDK, "run", failing_run)
    with pytest.raises(RuntimeError):
        asyncio.run(session.run_round(plan()))
    fleet = Fleet(monkeypatch)
    with pytest.raises(ValueError, match="No retained scout evidence"):
        asyncio.run(session.recover_organizers())
    assert fleet.organizer_inputs == {}
    assert session.status()["recoveries"] == []


def test_recovery_rejects_attempts_outside_the_session(monkeypatch):
    session, _ = killed_during_organization(monkeypatch)
    fleet = Fleet(monkeypatch)
    for bad in ("../outside", "rounds/../../outside", "canonical.md"):
        with pytest.raises(ValueError):
            asyncio.run(session.recover_organizers(attempt=bad))
    assert fleet.organizer_inputs == {}


def test_recovery_waits_for_no_one_and_refuses_a_running_round(monkeypatch):
    session, _ = killed_during_organization(monkeypatch)

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
            with pytest.raises(RuntimeError, match="already has a round running"):
                await RoundSession.open(session.path).recover_organizers(
                    attempt="rounds/round-001-attempt-001")
        finally:
            finish.set()
            await task

    asyncio.run(scenario())


def test_leftover_lock_files_after_a_crash_do_not_block_recovery_or_resume(monkeypatch):
    """Field report defect 2: the 1-byte lock files survive a kill. They are OS
    byte locks, released when the owning process dies, so their presence must
    never block the documented recovery path."""
    session, _ = killed_during_organization(monkeypatch)
    state = session.status()
    state.update(status="running", current_phase="organize",
                 active_attempt=state["attempts"][0]["directory"])
    state["attempts"][0].update(status="running")
    state["attempts"][0].pop("finished_at", None)
    rounds_module._write_json(session.path / "session.json", state)
    for name in (".round.lock", ".session.lock"):
        (session.path / name).write_bytes(b"0")

    Fleet(monkeypatch)
    forbid_research_dispatch(monkeypatch)
    asyncio.run(RoundSession.open(session.path).recover_organizers())
    after = session.status()
    assert after["attempts"][0]["status"] == "interrupted"
    assert after["status"] == "failed" and after["active_attempt"] is None


def test_unsaved_retry_is_never_replaced_by_its_earlier_failure(monkeypatch):
    """Codex review P1: if the successful retry cannot be saved, the manifest must
    not fall back to the failed first attempt and still claim completeness."""
    session = RoundSession.create("question", chains=2)
    Fleet(monkeypatch, fail_first={2})
    real_write = rounds_module.ScoutEvidenceWriter._write_record

    def lose_the_retry(self, path, record):
        if path.name == "scout-002-attempt-2.json":
            raise OSError("No space left on device")
        real_write(self, path, record)

    monkeypatch.setattr(rounds_module.ScoutEvidenceWriter, "_write_record", lose_the_retry)
    asyncio.run(session.run_round(plan()))
    manifest = json.loads((evidence_dir(session) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["effective"]["2"] == {"record": None, "status": "succeeded"}
    assert manifest["complete"] is False

    fleet = Fleet(monkeypatch)
    forbid_research_dispatch(monkeypatch)
    report = asyncio.run(session.recover_organizers())
    [(_, _, text, error)] = [s for s in fleet.organizer_inputs["A"][0] if s[0] == 2]
    assert text == "" and "not saved" in error  # not the stale attempt-1 failure
    assert "#2" in report and session.status()["recoveries"][0]["complete_evidence"] is False


def test_architect_plan_is_saved_before_any_scout_runs(monkeypatch):
    """Codex review P1: a hard kill during Architect-planned scouting must still
    leave the full plan, so missing scouts (and whole chains) stay identifiable."""
    session = RoundSession.create("question", chains=2)

    async def architect(self, question):
        self._has_architect = True
        return sdk._normalize_prompts(plan())

    observed = []

    def plan_on_disk(scout_id):
        # Record, don't assert: scout() converts exceptions into scout errors.
        saved = session.path / "rounds/round-001-attempt-001/prompts.json"
        observed.append(len(json.loads(saved.read_text(encoding="utf-8"))) if saved.exists() else None)

    monkeypatch.setattr(sdk.OracleSDK, "decompose", architect)
    Fleet(monkeypatch, scout_check=plan_on_disk)
    asyncio.run(session.run_round())
    assert observed == [6] * 6


def test_recovery_without_a_saved_plan_says_the_planned_set_is_unknown(monkeypatch):
    session, _ = killed_during_organization(monkeypatch)
    attempt = session.path / "rounds/round-001-attempt-001"
    (attempt / "prompts.json").unlink()
    (attempt / "scouts/manifest.json").unlink()
    Fleet(monkeypatch)
    forbid_research_dispatch(monkeypatch)
    report = asyncio.run(session.recover_organizers())
    assert "planned scouts are unknown" in report.lower()
    assert session.status()["recoveries"][0]["complete_evidence"] is False


def test_cancelled_organization_makes_round_usage_unknown(monkeypatch):
    """Codex review P1: organizer usage is aggregated only after every organizer
    returns, so a cancelled round must not report the scouts' usage as the total."""
    session, _ = killed_during_organization(monkeypatch)
    state = session.status()
    assert state["attempts"][0]["usage"]["available"] is False
    assert state["reported_usage"]["available"] is False


def test_failed_recovery_marks_usage_unknown_instead_of_undercounting(monkeypatch):
    session = RoundSession.create("question", chains=2)
    Fleet(monkeypatch)
    asyncio.run(session.run_round(plan()))
    assert session.status()["reported_usage"]["available"] is True
    Fleet(monkeypatch, organizer_error=asyncio.CancelledError())
    forbid_research_dispatch(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(session.recover_organizers())
    state = session.status()
    assert state["recoveries"][0]["status"] == "failed"
    assert state["reported_usage"]["available"] is False  # spend happened but is unknown


def test_dead_round_found_by_recovery_makes_session_usage_unknown(monkeypatch):
    """Codex review P1: a hard-killed round recorded no usage. Marking it
    interrupted must not leave an 'available' total that silently omits it."""
    session = RoundSession.create("question", rounds=2, chains=2)
    Fleet(monkeypatch)
    asyncio.run(session.run_round(plan()))
    after_round_one = session.status()["reported_usage"]
    assert after_round_one["available"] is True

    Fleet(monkeypatch, organizer_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(session.run_round(plan()))
    state = session.status()  # rewrite as a hard kill: no failure accounting ran
    state.update(status="running", reported_usage=after_round_one)
    state["attempts"][1].update(status="running")
    for key in ("usage", "error", "finished_at"):
        state["attempts"][1].pop(key, None)
    rounds_module._write_json(session.path / "session.json", state)

    Fleet(monkeypatch)
    forbid_research_dispatch(monkeypatch)
    asyncio.run(session.recover_organizers())
    after = session.status()
    assert after["attempts"][1]["status"] == "interrupted"
    assert after["reported_usage"]["available"] is False


def test_recovery_is_visible_in_status_while_it_runs(monkeypatch):
    session, _ = killed_during_organization(monkeypatch)
    seen = []

    def observe(chain):
        state = RoundSession.open(session.path).status()
        seen.append((state["current_phase"], state["active_attempt"], state["recoveries"][-1]["status"]))

    Fleet(monkeypatch, organizer_check=observe)
    forbid_research_dispatch(monkeypatch)
    asyncio.run(session.recover_organizers())
    target = "rounds/round-001-attempt-001/recovery-001"
    assert seen and all(entry == ("recovery", target, "running") for entry in seen)
    after = session.status()
    assert after["current_phase"] != "recovery" and after["active_attempt"] is None


def test_recovery_artifact_failure_is_recorded_and_a_stale_recovery_is_interrupted(monkeypatch):
    session, _ = killed_during_organization(monkeypatch)
    Fleet(monkeypatch)
    forbid_research_dispatch(monkeypatch)
    real_write = rounds_module._atomic_write

    def full_disk(path, text):
        if path.parent.name.startswith("recovery-") and path.name == "report.md":
            raise OSError("No space left on device")
        real_write(path, text)

    with monkeypatch.context() as patch:
        patch.setattr(rounds_module, "_atomic_write", full_disk)
        with pytest.raises(OSError):
            asyncio.run(session.recover_organizers())
    assert session.status()["recoveries"][0]["status"] == "failed"

    state = session.status()  # a recovery killed mid-flight leaves "running" behind
    state["recoveries"][0]["status"] = "running"
    rounds_module._write_json(session.path / "session.json", state)
    asyncio.run(session.recover_organizers())
    assert [r["status"] for r in session.status()["recoveries"]] == ["interrupted", "completed"]


def test_one_unreadable_record_does_not_strand_the_rest(monkeypatch):
    session, _ = killed_during_organization(monkeypatch)
    (evidence_dir(session) / "scout-005-attempt-1.json").write_text("{truncated", encoding="utf-8")
    fleet = Fleet(monkeypatch)
    forbid_research_dispatch(monkeypatch)
    report = asyncio.run(session.recover_organizers())
    inputs = {s[0]: s for s in fleet.organizer_inputs["B"][0]}
    assert inputs[4][2] == finding(4) and inputs[6][2] == finding(6)
    assert inputs[5][2] == "" and "unreadable" in inputs[5][3]
    assert "#5" in report and session.status()["recoveries"][0]["complete_evidence"] is False


def test_failed_target_selection_still_reconciles_stale_work(monkeypatch):
    session = RoundSession.create("question")
    state = session.status()
    (session.path / "rounds/round-001-attempt-001").mkdir()
    state.update(status="running", attempts=[{
        "round": 1, "attempt": 1, "directory": "rounds/round-001-attempt-001", "status": "running"}])
    rounds_module._write_json(session.path / "session.json", state)
    with pytest.raises(ValueError, match="No retained scout evidence"):
        asyncio.run(session.recover_organizers())
    assert session.status()["attempts"][0]["status"] == "interrupted"


def test_default_recovery_skips_newer_attempts_without_usable_evidence(monkeypatch):
    session, _ = killed_during_organization(monkeypatch)
    Fleet(monkeypatch, fail_always={1, 2, 3, 4, 5, 6})
    asyncio.run(session.run_round(plan()))  # all scouts fail: evidence of failure only
    assert (session.path / "rounds/round-001-attempt-002/scouts").is_dir()
    Fleet(monkeypatch)
    forbid_research_dispatch(monkeypatch)
    asyncio.run(session.recover_organizers())
    assert session.status()["recoveries"][0]["attempt"] == "rounds/round-001-attempt-001"


def test_a_real_run_after_reorganize_is_not_labeled_as_replayed(monkeypatch):
    Fleet(monkeypatch)
    oracle = sdk.OracleSDK(chains=2)
    saved = [sdk.ScoutResult(scout_id=1, chain="A", dimension="d", result_text="x")]
    assert "saved evidence, not re-run" in asyncio.run(oracle.reorganize(saved))
    assert "saved evidence, not re-run" not in asyncio.run(oracle.run("question", prompts=plan()))


def test_reorganize_after_an_architect_run_does_not_claim_an_architect(monkeypatch):
    Fleet(monkeypatch)
    oracle = sdk.OracleSDK(chains=2)
    oracle._has_architect = True  # as left by a previous Architect-planned run
    saved = [sdk.ScoutResult(scout_id=1, chain="A", dimension="d", result_text="x")]
    assert "Architect ->" not in asyncio.run(oracle.reorganize(saved))


# ---------------------------------------------------------------------------
# Real process death (not asyncio cancellation)
# ---------------------------------------------------------------------------

def _spawn(code: str, tmp_path: Path):
    import subprocess
    import sys
    script = tmp_path / "child.py"
    script.write_text(code, encoding="utf-8")
    src = Path(sdk.__file__).resolve().parents[1]
    env = {**__import__("os").environ, "PYTHONPATH": str(src), "PYTHONUTF8": "1"}
    return subprocess.Popen([sys.executable, str(script)], env=env, cwd=tmp_path)


def _wait_until_ready(child, marker: Path, timeout: float = 60) -> None:
    """Bounded wait for the child's readiness file; never hang CI on a stuck child."""
    import time
    deadline = time.monotonic() + timeout
    while not marker.exists():
        if child.poll() is not None:
            pytest.fail(f"child exited early with {child.returncode}")
        if time.monotonic() > deadline:
            pytest.fail("child never became ready")
        time.sleep(0.05)


READY = "from pathlib import Path\ndef ready():\n    Path('ready').write_text('1')\n"


def test_os_releases_the_round_lock_when_its_owner_is_killed(tmp_path):
    lock = tmp_path / ".round.lock"
    child = _spawn(
        READY + "import time\n"
        "from claude_oracle import rounds\n"
        f"with rounds._session_lock(Path({str(lock)!r})):\n"
        "    ready()\n"
        "    time.sleep(120)\n",
        tmp_path,
    )
    try:
        _wait_until_ready(child, tmp_path / "ready")
        with pytest.raises(RuntimeError, match="already has a round running"):
            with rounds_module._session_lock(lock):
                pass
    finally:
        child.kill()  # TerminateProcess / SIGKILL: no cleanup code runs
        child.wait(timeout=30)
    assert lock.exists()  # the file survives the kill, but the lock does not.
    # Windows releases a dead process's locks asynchronously ("the time it takes ...
    # depends upon available system resources" -- LockFileEx docs), so poll, bounded.
    import time
    deadline = time.monotonic() + 30
    while True:
        try:
            with rounds_module._session_lock(lock):
                break
        except RuntimeError:
            if time.monotonic() > deadline:
                pytest.fail("the OS never released the dead owner's lock")
            time.sleep(0.1)


def test_hard_kill_during_organization_is_recoverable(monkeypatch, tmp_path):
    """The field report end to end: every scout succeeds, the process is killed
    mid-organization with no chance to run exception handlers, then recovery
    organizes the saved findings without re-running a single scout."""
    session_dir = tmp_path / "killed-session"
    child = _spawn(
        READY + "import asyncio\n"
        "from claude_oracle import RoundSession, sdk\n"
        "sdk._oauth_token = lambda: 'offline-test-token'\n"
        "async def scout(self, scout_id, chain, dimension, prompt, on_started=None):\n"
        "    if on_started: on_started()\n"
        "    self._active_scouts[scout_id] = 'done'\n"
        "    return sdk.ScoutResult(scout_id=scout_id, chain=chain, dimension=dimension,\n"
        "                           result_text=f'kept finding {scout_id}')\n"
        "async def organizer(self, chain, results):\n"
        "    ready()\n"
        "    await asyncio.sleep(120)\n"
        "sdk.OracleSDK._run_scout = scout\n"
        "sdk.OracleSDK._run_compressor = organizer\n"
        f"session = RoundSession.create('question', chains=2, directory={str(session_dir)!r})\n"
        f"asyncio.run(session.run_round({plan()!r}))\n",
        tmp_path,
    )
    try:
        _wait_until_ready(child, tmp_path / "ready")
    finally:
        child.kill()
        child.wait(timeout=30)

    session = RoundSession.open(session_dir)
    state = session.status()
    assert state["status"] == "running" and state["current_phase"] == "organize"
    assert state["progress"]["scouts"]["retained"] == 6
    fleet = Fleet(monkeypatch)
    forbid_research_dispatch(monkeypatch)
    report = asyncio.run(session.recover_organizers())
    assert "Organized chain A" in report and "Organized chain B" in report
    assert [s[2] for s in fleet.organizer_inputs["B"][0]] == [f"kept finding {i}" for i in (4, 5, 6)]
    assert session.status()["attempts"][0]["status"] == "interrupted"


def test_hard_killed_recovery_does_not_fail_a_completed_session(monkeypatch, tmp_path):
    """Codex review P2: a recovery that dies must be reconciled as an interrupted
    recovery, not as a failed research round. The completed session stays complete."""
    session = RoundSession.create("question", chains=2, directory=tmp_path / "done-session")
    Fleet(monkeypatch)
    asyncio.run(session.run_round(plan()))
    before = session.status()
    assert before["status"] == "rounds_complete" and before["research_outcome"] == "complete"

    child = _spawn(
        READY + "import asyncio\n"
        "from claude_oracle import RoundSession, sdk\n"
        "sdk._oauth_token = lambda: 'offline-test-token'\n"
        "async def organizer(self, chain, results):\n"
        "    ready()\n"
        "    await asyncio.sleep(120)\n"
        "sdk.OracleSDK._run_compressor = organizer\n"
        f"asyncio.run(RoundSession.open({str(session.path)!r}).recover_organizers())\n",
        tmp_path,
    )
    try:
        _wait_until_ready(child, tmp_path / "ready")
    finally:
        child.kill()
        child.wait(timeout=30)
    assert session.status()["current_phase"] == "recovery"

    Fleet(monkeypatch)
    forbid_research_dispatch(monkeypatch)
    asyncio.run(session.recover_organizers())
    after = session.status()
    assert [r["status"] for r in after["recoveries"]] == ["interrupted", "completed"]
    assert after["status"] == "rounds_complete" and after["research_outcome"] == "complete"
    assert after["current_phase"] == before["current_phase"] and after["active_attempt"] is None
    assert after["completed_rounds"] == before["completed_rounds"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cli(monkeypatch, args):
    class UnreadableInput:
        def isatty(self):
            return False

        def read(self):
            pytest.fail("Recovery must not read stdin")

    monkeypatch.setattr(sdk.sys, "argv", ["claude-oracle", *args])
    monkeypatch.setattr(sdk.sys, "stdin", UnreadableInput())
    asyncio.run(sdk._async_main())


def test_cli_recover_prints_the_recovered_report(monkeypatch, capsys):
    session, _ = killed_during_organization(monkeypatch)
    capsys.readouterr()
    Fleet(monkeypatch)
    forbid_research_dispatch(monkeypatch)
    cli(monkeypatch, ["--recover", str(session.path)])
    out = capsys.readouterr().out
    assert "Organized chain B" in out
    assert out.strip() == (session.path / "rounds/round-001-attempt-001/recovery-001/report.md") \
        .read_text(encoding="utf-8").strip()


@pytest.mark.parametrize("extra", [["question"], ["--rounds", "2"], ["--chains", "2"], ["--local"], ["--usd"]])
def test_cli_recover_rejects_configuration_overrides(monkeypatch, extra):
    session = RoundSession.create("question", directory="session")
    fleet = Fleet(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli(monkeypatch, ["--recover", str(session.path), *extra])
    assert exc.value.code == 2
    assert fleet.organizer_inputs == {}


def test_cli_help_documents_recovery(monkeypatch, capsys):
    monkeypatch.setattr(sdk.sys, "argv", ["claude-oracle", "--help"])
    with pytest.raises(SystemExit):
        asyncio.run(sdk._async_main())
    assert "--recover" in capsys.readouterr().out
