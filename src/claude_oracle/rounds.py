"""Durable research rounds managed by the calling session."""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile


DEFAULT_ROUNDS = 1
SCHEMA_VERSION = 2

EVIDENCE_DIR = "scouts"
EVIDENCE_SCHEMA = "claude-oracle/scout-evidence"
EVIDENCE_SCHEMA_VERSION = 1
_RECORD_NAME = re.compile(r"scout-(\d+)-attempt-(\d+)\.json")
_ATTEMPT_NAME = re.compile(r"round-(\d{3})-attempt-\d{3}")

_OUTCOMES = {"complete", "partial", "failed", "unknown"}
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cost_usd",
    "quota_units",
)


def _empty_reported_usage() -> dict:
    return {**{key: None for key in _USAGE_FIELDS}, "available": False}


def _status_defaults(state: dict) -> dict:
    """Apply v1-compatible defaults for the observable handoff contract."""
    state.setdefault("research_outcome", "unknown")
    state.setdefault("current_phase", "idle")
    state.setdefault("progress", {"scouts": {"completed": 0, "total": 0}, "organizers": {"completed": 0, "total": 0}})
    state.setdefault("active_attempt", None)
    state.setdefault("last_updated_at", state.get("created_at", _now()))
    state.setdefault("next_action", "Run the first research round")
    state.setdefault("checkpoint", {"path": "canonical.md", "revision": None, "through_round": 0})
    state.setdefault("artifact_paths", [])
    state.setdefault("recoveries", [])
    usage = state.setdefault("reported_usage", _empty_reported_usage())
    if not isinstance(usage, dict):
        usage = _empty_reported_usage()
        state["reported_usage"] = usage
    usage.setdefault("available", False)
    for key in _USAGE_FIELDS:
        usage.setdefault(key, None)
    if usage["available"] and any(usage[key] is None for key in _USAGE_FIELDS):
        usage["available"] = False
    if not usage["available"]:
        for key in _USAGE_FIELDS:
            usage[key] = None
    return state


def _positive_integer(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    """Publish complete files so a caller can read progress while a round runs."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_json(path: Path, value: dict | list) -> None:
    _atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


@contextmanager
def _file_lock(path: Path, *, blocking: bool, error_message: str | None = None):
    """Lock one byte in a file; the OS releases the lock after a crash."""
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(stream.fileno(), mode, 1)
            except OSError as exc:
                if error_message is None:
                    raise
                raise RuntimeError(error_message) from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                mode = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
                fcntl.flock(stream.fileno(), mode)
            except OSError as exc:
                if error_message is None:
                    raise
                raise RuntimeError(error_message) from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def _session_lock(path: Path):
    """One round writer per session; the OS releases the lock after a crash."""
    with _file_lock(
        path,
        blocking=False,
        # Windows releases a killed process's locks asynchronously, so a resume or
        # recovery launched right after a crash can briefly see the old owner.
        error_message=("This session already has a round running "
                       "(if its process just exited, retry in a few seconds)"),
    ):
        yield


@contextmanager
def _state_lock(path: Path):
    """Serialize read-modify-write updates to session.json."""
    with _file_lock(path, blocking=True):
        yield


def _usage_record(usage) -> dict:
    if not getattr(usage, "usage_observed", False) or not getattr(usage, "usage_available", False):
        return _empty_reported_usage()
    return {**{key: getattr(usage, key, None) for key in _USAGE_FIELDS}, "available": True}


class ScoutEvidenceWriter:
    """OracleSDK evidence sink: persist every scout attempt the moment it finishes.

    Layout inside an attempt directory:
      scouts/scout-<id>-attempt-<k>.json   one record per attempt (k=2 is the retry)
      scouts/manifest.json                 written once scouting finishes; names the
                                           effective record per scout. No manifest
                                           means the set is incomplete.
    """

    def __init__(self, folder: Path, on_retained=None):
        self.folder = folder
        self.on_retained = on_retained
        self.effective: dict[int, dict] = {}

    @property
    def retained(self) -> int:
        """Scouts whose effective result is a successful finding saved on disk."""
        return sum(1 for entry in self.effective.values()
                   if entry["status"] == "succeeded" and entry["record"] is not None)

    def plan_ready(self, prompts: list[dict]) -> None:
        """Save the effective plan before dispatch, so a hard kill during an
        Architect-planned round still identifies every scout (and chain) planned."""
        _write_json(self.folder.parent / "prompts.json", prompts)

    def scout_finished(self, prompt: dict, result, scout_attempt: int) -> None:
        scout_id = prompt["id"]
        name = f"scout-{scout_id:03d}-attempt-{scout_attempt}.json"
        status = "failed" if result.error else "succeeded"
        # The organizer receives this attempt whether or not it can be saved; an
        # unsaved effective result must never fall back to an earlier record.
        self.effective[scout_id] = {"record": None, "status": status}
        self._write_record(self.folder / name, {
            "schema": EVIDENCE_SCHEMA,
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "scout_id": scout_id,
            "chain": prompt["chain"],
            "dimension": prompt.get("dimension", ""),
            "prompt": prompt["prompt"],
            "scout_attempt": scout_attempt,
            "status": status,
            "result_text": result.result_text or "",
            "error": result.error,
            "duration_ms": result.duration_ms,
            "recorded_at": _now(),
            "usage": _usage_record(result.usage),
        })
        self.effective[scout_id]["record"] = name
        if self.on_retained is not None:
            self.on_retained(self.retained)

    def scouting_finished(self, prompts: list[dict], results) -> None:
        planned = [p["id"] for p in prompts]
        _write_json(self.folder / "manifest.json", {
            "schema": EVIDENCE_SCHEMA,
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "complete": all(self.effective.get(scout_id, {}).get("record") for scout_id in planned),
            "planned_scouts": planned,
            "effective": {str(k): v for k, v in sorted(self.effective.items())},
            "recorded_at": _now(),
        })

    def _write_record(self, path: Path, record: dict) -> None:
        self.folder.mkdir(exist_ok=True)
        _write_json(path, record)


def load_scout_evidence(attempt: Path):
    """Rebuild the organizer input for one attempt from its saved evidence.

    Returns (scout_results, complete, missing_ids, plan_known). Without a manifest
    the latest saved attempt per scout is used and the set is incomplete. Planned
    scouts with no usable record become explicit errors, so the organizer reports
    the gap instead of silently narrowing coverage.
    """
    from .sdk import ScoutResult, UsageStats

    def read(path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    evidence = attempt / EVIDENCE_DIR
    prompts = read(attempt / "prompts.json")
    plan_known = isinstance(prompts, list)
    planned = {p["id"]: p for p in prompts or [] if isinstance(p, dict) and "id" in p}
    manifest = read(evidence / "manifest.json")
    if not (isinstance(manifest, dict) and isinstance(manifest.get("effective"), dict)):
        manifest = None  # unreadable manifest: fall back to scanning, and the set is incomplete
    # Why a planned scout has no usable record; each becomes an explicit organizer error.
    gaps: dict[int, str] = {}
    if manifest is not None:
        chosen = {int(k): v["record"] for k, v in manifest["effective"].items() if v.get("record")}
        gaps.update({int(k): "Evidence not saved (the effective result could not be written)"
                     for k, v in manifest["effective"].items() if not v.get("record")})
    else:
        latest: dict[int, tuple[int, str]] = {}
        for path in evidence.glob("scout-*-attempt-*.json"):
            match = _RECORD_NAME.fullmatch(path.name)
            if match:
                scout_id, scout_attempt = int(match[1]), int(match[2])
                if scout_attempt > latest.get(scout_id, (0, ""))[0]:
                    latest[scout_id] = (scout_attempt, path.name)
        chosen = {scout_id: name for scout_id, (_, name) in latest.items()}

    results = []
    for scout_id, name in chosen.items():
        record = read(evidence / name)
        try:
            if record.get("schema") != EVIDENCE_SCHEMA or record.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
                raise ValueError("unsupported format")
            results.append(ScoutResult(
                scout_id=record["scout_id"], chain=record["chain"], dimension=record["dimension"],
                result_text=record["result_text"], error=record["error"],
                duration_ms=record["duration_ms"], usage=UsageStats(),
            ))
        except (AttributeError, KeyError, TypeError, ValueError):
            gaps[scout_id] = f"Evidence unreadable ({name}); the other saved findings are unaffected"
    loaded = {r.scout_id for r in results}
    missing = sorted((set(planned) | set(gaps)) - loaded)
    for scout_id in missing:
        prompt = planned.get(scout_id)
        if prompt is None:
            continue  # no plan to say which chain it belonged to; still listed as missing
        results.append(ScoutResult(
            scout_id=scout_id, chain=prompt["chain"], dimension=prompt.get("dimension", ""),
            result_text="", usage=UsageStats(),
            error=gaps.get(scout_id, "Evidence not retained (scouting stopped before this scout finished)"),
        ))
    complete = bool(manifest and manifest.get("complete")) and plan_known and not missing
    return sorted(results, key=lambda r: (r.chain, r.scout_id)), complete, missing, plan_known


class RoundSession:
    """Persist rounds while the caller plans research and edits canonical.md.

    Each run_round() executes one round and returns control to the orchestrator.
    The orchestrator supplies a fresh plan for each subsequent round and owns
    the canonical report. Oracle never rewrites that report after creation.
    """

    def __init__(self, directory: str | os.PathLike):
        self.path = Path(directory).resolve()

    @classmethod
    def create(
        cls,
        question: str,
        *,
        rounds: int = DEFAULT_ROUNDS,
        chains: int = 1,
        directory: str | os.PathLike | None = None,
        local_tools: bool = False,
        show_dollars: bool = False,
    ) -> "RoundSession":
        from .sdk import MAX_CHAINS

        _positive_integer(rounds, "Rounds")
        _positive_integer(chains, "Chains")
        if chains > MAX_CHAINS:
            raise ValueError(f"Chains must be 1-{MAX_CHAINS}")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("A research question is required for a round session")
        if directory is None:
            parent = Path("research")
            parent.mkdir(exist_ok=True)
            prefix = "oracle-" + datetime.now().strftime("%Y%m%d-%H%M%S-")
            directory = tempfile.mkdtemp(prefix=prefix, dir=parent)
        else:
            directory = Path(directory)
            # Never adopt or overwrite an existing directory or report.
            directory.mkdir(parents=True, exist_ok=False)
        session = cls(directory)
        (session.path / "rounds").mkdir()
        (session.path / ".gitignore").write_text("*\n", encoding="utf-8")
        (session.path / "canonical.md").write_text(
            "# Canonical research report\n\n"
            f"## Research question\n\n{question.strip()}\n\n"
            "## Findings\n\nResearch has not returned yet.\n\n"
            "## Open questions\n\nAwaiting the first round.\n\n"
            "## Sources\n\nNo sources collected yet.\n",
            encoding="utf-8",
        )
        _write_json(session.path / "session.json", {
            "schema_version": SCHEMA_VERSION,
            "research_outcome": "unknown",
            "current_phase": "idle",
            "progress": {"scouts": {"completed": 0, "total": 0}, "organizers": {"completed": 0, "total": 0}},
            "active_attempt": None,
            "last_updated_at": _now(),
            "next_action": "Run the first research round",
            "checkpoint": {"path": "canonical.md", "revision": None, "through_round": 0},
            "artifact_paths": [],
            "reported_usage": _empty_reported_usage(),
            "question": question.strip(),
            "rounds": rounds,
            "chains": chains,
            "local_tools": bool(local_tools),
            "show_dollars": bool(show_dollars),
            "canonical_report": "canonical.md",
            "completed_rounds": 0,
            "status": "awaiting_plan",
            "created_at": _now(),
            "attempts": [],
        })
        return session

    @classmethod
    def open(cls, directory: str | os.PathLike) -> "RoundSession":
        session = cls(directory)
        session.status()
        return session

    def _read_state(self) -> dict:
        from .sdk import MAX_CHAINS

        state = json.loads((self.path / "session.json").read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("schema_version") not in {1, SCHEMA_VERSION}:
            raise ValueError("Unsupported Oracle round session format")
        state = _status_defaults(state)
        _positive_integer(state.get("rounds"), "Rounds")
        _positive_integer(state.get("chains"), "Chains")
        completed = state.get("completed_rounds")
        if (
            state["chains"] > MAX_CHAINS
            or isinstance(completed, bool)
            or not isinstance(completed, int)
            or not 0 <= completed <= state["rounds"]
            or not isinstance(state.get("question"), str)
            or not isinstance(state.get("attempts"), list)
            or state.get("status") not in {
                "awaiting_plan", "running", "rounds_complete", "failed"
            }
            or not isinstance(state.get("local_tools"), bool)
            or not isinstance(state.get("show_dollars"), bool)
            or state.get("research_outcome") not in _OUTCOMES
            or not isinstance(state.get("progress"), dict)
            or not isinstance(state.get("checkpoint"), dict)
            or not isinstance(state["checkpoint"].get("through_round"), int)
            or not 0 <= state["checkpoint"]["through_round"] <= completed
            or not isinstance(state.get("artifact_paths"), list)
            or not isinstance(state.get("recoveries"), list)
            or not isinstance(state.get("reported_usage"), dict)
        ):
            raise ValueError("Invalid Oracle round session state")
        return state

    def _persist_state(self, state: dict) -> None:
        """Write state while preserving a checkpoint recorded by another process."""
        state["schema_version"] = SCHEMA_VERSION
        with _state_lock(self.path / ".session.lock"):
            latest = self._read_state()
            state["checkpoint"] = latest["checkpoint"]
            _write_json(self.path / "session.json", state)

    @staticmethod
    def _artifact_paths(base: Path, existing: list, current: Path,
                        names: tuple = ("prompts.json", "report.md", "metrics.json")) -> list[str]:
        """Keep session artifacts relative and retain previous rounds."""
        paths: list[str] = []
        for raw in existing:
            if not isinstance(raw, str):
                continue
            path = Path(raw)
            if path.is_absolute():
                try:
                    path = path.relative_to(base)
                except ValueError:
                    continue
            normalized = path.as_posix()
            if normalized not in paths:
                paths.append(normalized)
        for name in names:
            path = (current / name).relative_to(base).as_posix()
            if path not in paths:
                paths.append(path)
        return paths

    _usage_snapshot = staticmethod(_usage_record)

    @staticmethod
    def _record_usage(state: dict, usage, had_prior_attempts: bool) -> dict:
        snapshot = RoundSession._usage_snapshot(usage)
        reported = state["reported_usage"]
        if not snapshot["available"]:
            state["reported_usage"] = _empty_reported_usage()
            return state["reported_usage"]
        if not had_prior_attempts and not reported.get("available", False):
            state["reported_usage"] = snapshot
            return snapshot
        if not reported.get("available", False):
            state["reported_usage"] = _empty_reported_usage()
            return state["reported_usage"]
        for key in _USAGE_FIELDS:
            reported[key] += snapshot[key]
        reported["available"] = True
        return reported

    @staticmethod
    def _interrupt_stale(state: dict) -> bool:
        """Mark work that lost its process as interrupted; True if a research
        attempt died. Only call while holding .round.lock: holding it proves no
        live process owns anything still marked 'running'.

        Dead work recorded no usage, so a session total that omits it would be a
        silent undercount: make it unknown. A dead recovery is not a failed round:
        it restores the lifecycle phase it displaced instead of failing the session.
        """
        def interrupt(entries: list) -> list:
            dead = [entry for entry in entries if entry["status"] == "running"]
            for entry in dead:
                entry.update(status="interrupted", finished_at=_now())
                if "usage" not in entry:
                    state["reported_usage"] = _empty_reported_usage()
            return dead

        for recovery in interrupt(state["recoveries"]):
            if state["active_attempt"] == recovery["directory"]:
                state.update(current_phase=recovery.get("previous_phase", "idle"), active_attempt=None)
        return bool(interrupt(state["attempts"]))

    @staticmethod
    def _research_outcome(metrics) -> str:
        if metrics.scout_count and metrics.scout_errors >= metrics.scout_count:
            return "failed"
        if metrics.scout_errors or metrics.compressor_errors:
            return "partial"
        return "complete"

    def status(self) -> dict:
        """Read the latest atomic snapshot without waiting for a running round."""
        return self._read_state()

    def checkpoint(self, *, revision: str, through_round: int, path: str = "canonical.md") -> dict:
        """Record the caller's editorial checkpoint; never advances automatically."""
        if not isinstance(revision, str) or not revision.strip():
            raise ValueError("Checkpoint revision is required")
        with _state_lock(self.path / ".session.lock"):
            state = self._read_state()
            if not isinstance(through_round, int) or not 0 <= through_round <= state["completed_rounds"]:
                raise ValueError("Checkpoint round must be between zero and completed rounds")
            state["checkpoint"] = {"path": path, "revision": revision, "through_round": through_round}
            state["last_updated_at"] = _now()
            state["schema_version"] = SCHEMA_VERSION
            _write_json(self.path / "session.json", state)
            return state["checkpoint"]

    async def run_round(self, prompts: list[dict] | None = None, *, verbose: bool = False) -> str:
        """Execute one research round; later rounds require the caller's new plan.

        Failed or interrupted attempts are retained. Resuming retries the same
        numbered round in a new attempt directory, without consuming a round.
        The lock covers model work but never locks the canonical report.
        """
        from .sdk import MAX_CHAINS, OracleSDK, _normalize_prompts

        with _session_lock(self.path / ".round.lock"):
            state = self.status()
            number = state["completed_rounds"] + 1
            had_prior_attempts = bool(state["attempts"])
            if number > state["rounds"]:
                raise ValueError("All requested rounds have already finished")
            if prompts is None and (number > 1 or state["attempts"]):
                raise ValueError("Supply a fresh prompt array to continue or retry a session")
            if prompts is not None:
                prompts = _normalize_prompts(prompts)
                if len({p["chain"] for p in prompts}) > MAX_CHAINS:
                    raise ValueError(f"Prompts may span at most {MAX_CHAINS} chains")
                ids = [p["id"] for p in prompts]
                if any(isinstance(i, bool) or not isinstance(i, int) or i < 1 for i in ids):
                    raise ValueError("Scout IDs must be positive integers")
                if len(set(ids)) != len(ids):
                    raise ValueError("Scout IDs must be unique within a round")
            self._interrupt_stale(state)
            attempt_number = 1
            while True:
                relative = Path("rounds") / f"round-{number:03d}-attempt-{attempt_number:03d}"
                folder = self.path / relative
                try:
                    folder.mkdir()
                    break
                except FileExistsError:
                    attempt_number += 1
            if prompts is not None:
                _write_json(folder / "prompts.json", prompts)
            attempt = {
                "round": number,
                "attempt": attempt_number,
                "directory": relative.as_posix(),
                "status": "running",
                "started_at": _now(),
            }
            state["attempts"].append(attempt)
            state["status"] = "running"
            state["research_outcome"] = "unknown"
            state["current_phase"] = "research"
            state["active_attempt"] = relative.as_posix()
            state["progress"] = {"scouts": {"completed": 0, "total": len(prompts or []), "retained": 0}, "organizers": {"completed": 0, "total": len({p["chain"] for p in prompts or []})}}
            state["last_updated_at"] = _now()
            state["next_action"] = "Wait for research results"

            # "completed" counts scouts that ran; "retained" counts findings saved
            # on disk, which is what survives if this process dies mid-round.
            def on_retained(count: int) -> None:
                state["progress"]["scouts"]["retained"] = count
                state["last_updated_at"] = _now()
                self._persist_state(state)

            evidence = ScoutEvidenceWriter(folder / EVIDENCE_DIR, on_retained=on_retained)

            def on_progress(update: dict) -> None:
                state["current_phase"] = update["phase"]
                state["progress"] = update["progress"]
                state["progress"]["scouts"]["retained"] = evidence.retained
                state["last_updated_at"] = _now()
                self._persist_state(state)

            self._persist_state(state)
            oracle = OracleSDK(
                chains=state["chains"], verbose=verbose,
                local_tools=state["local_tools"], show_dollars=state["show_dollars"],
                progress_callback=on_progress, evidence_sink=evidence,
            )
            oracle.status(f"Round {number}/{state['rounds']} | session: {self.path}")
            oracle.status(f"Canonical report: {self.path / 'canonical.md'} (maintained by caller)")
            try:
                report = await oracle.run(state["question"], prompts=prompts)
                if oracle.planned_prompts:
                    _write_json(folder / "prompts.json", oracle.planned_prompts)
                report = f"# Oracle round {number} of {state['rounds']}\n\n{report}"
                _atomic_write(folder / "report.md", report)
                metrics = asdict(oracle.metrics)
                metrics["elapsed_seconds"] = oracle.metrics.total_time
                metrics["total_usage"] = asdict(oracle.metrics.total_usage)
                _write_json(folder / "metrics.json", metrics)
                attempt.update(status="completed", finished_at=_now())
                state["completed_rounds"] = number
                state["status"] = "rounds_complete" if number == state["rounds"] else "awaiting_plan"
                state["research_outcome"] = self._research_outcome(oracle.metrics)
                state["current_phase"] = "complete"
                state["active_attempt"] = None
                state["progress"] = {"scouts": {"completed": oracle.metrics.scout_count, "total": oracle.scouts_total, "retained": evidence.retained}, "organizers": {"completed": oracle.metrics.compressor_count, "total": oracle.metrics.chain_count}}
                state["reported_usage"] = self._record_usage(
                    state, oracle.metrics.total_usage, had_prior_attempts
                )
                attempt["usage"] = self._usage_snapshot(oracle.metrics.total_usage)
                names = ("prompts.json", "report.md", "metrics.json")
                if (folder / EVIDENCE_DIR / "manifest.json").exists():
                    names += (f"{EVIDENCE_DIR}/manifest.json",)
                state["artifact_paths"] = self._artifact_paths(
                    self.path, state.get("artifact_paths", []), self.path / relative, names
                )
                state["last_updated_at"] = _now()
                state["next_action"] = "Revise canonical.md and record a checkpoint" if state["status"] == "rounds_complete" else "Prepare a fresh plan for the next round"
                self._persist_state(state)
            except BaseException as exc:
                attempt.update(
                    status="failed",
                    error=str(exc) or type(exc).__name__,
                    finished_at=_now(),
                    usage=self._usage_snapshot(oracle.metrics.total_usage),
                )
                state["reported_usage"] = self._record_usage(
                    state, oracle.metrics.total_usage, had_prior_attempts
                )
                state["completed_rounds"] = number - 1
                state["status"] = "failed"
                state["research_outcome"] = "failed"
                state["current_phase"] = "failed"
                state["active_attempt"] = None
                state["progress"] = {
                    "scouts": {
                        "completed": min(oracle.metrics.scout_count, oracle.scouts_total),
                        "total": oracle.scouts_total,
                        "retained": evidence.retained,
                    },
                    "organizers": {
                        "completed": min(oracle.metrics.compressor_count, oracle.metrics.chain_count),
                        "total": oracle.metrics.chain_count,
                    },
                }
                state["last_updated_at"] = _now()
                state["next_action"] = (
                    "Recover organizers from the retained scout evidence, or retry the round with a fresh plan"
                    if evidence.retained else "Retry the failed round with a fresh plan"
                )
                # Keep the original failure if recording it also fails (e.g. disk full).
                try:
                    if oracle.planned_prompts:
                        _write_json(folder / "prompts.json", oracle.planned_prompts)
                    self._persist_state(state)
                except OSError:
                    pass
                raise
            if state["status"] == "awaiting_plan":
                oracle.status(
                    f"Round {number} returned. Read its findings, prepare the next plan, "
                    "and resume this session. Strengthen canonical.md while the next round runs."
                )
            else:
                oracle.status("All research rounds returned. Finish the canonical report before presenting it.")
            return report

    def _recovery_target(self, state: dict, attempt: str | None):
        """Pick the attempt to replay and load its evidence.

        By default, the newest attempt with at least one saved successful finding:
        a newer attempt holding only failures must not strand older usable evidence.
        """
        if attempt is None:
            for previous in reversed(state["attempts"]):
                folder = self.path / previous["directory"]
                if not (folder / EVIDENCE_DIR).is_dir():
                    continue
                try:
                    evidence = load_scout_evidence(folder)
                except (OSError, ValueError, KeyError, TypeError):
                    continue  # unreadable evidence: fall through to an older attempt
                if any(not r.error for r in evidence[0]):
                    return folder, previous["directory"], evidence
            raise ValueError("No retained scout evidence in this session")
        folder = (self.path / attempt).resolve()
        if (folder.parent != (self.path / "rounds").resolve()
                or not _ATTEMPT_NAME.fullmatch(folder.name) or not folder.is_dir()):
            raise ValueError("Recovery target must be an attempt directory under rounds/")
        relative = folder.relative_to(self.path).as_posix()
        evidence = load_scout_evidence(folder)
        if not any(not r.error for r in evidence[0]):
            raise ValueError(f"No retained scout evidence to organize in {relative}")
        return folder, relative, evidence

    async def recover_organizers(self, attempt: str | None = None, *, verbose: bool = False) -> str:
        """Re-run only the organizers over an attempt's saved scout evidence.

        Bounded replay: no Architect and no scouts are launched, and each chain's
        organizer sees only that chain's saved findings. The result is written to
        a new rounds/<attempt>/recovery-NNN/ directory; canonical.md, earlier
        reports, and the round budget are never touched. Defaults to the newest
        attempt with usable evidence. Takes the round lock like run_round, so it
        cannot race a running round.
        """
        from .sdk import OracleSDK

        with _session_lock(self.path / ".round.lock"):
            state = self.status()
            before = json.dumps(state, sort_keys=True)
            if self._interrupt_stale(state):
                state.update(status="failed", research_outcome="failed", current_phase="failed",
                             active_attempt=None)
            try:
                folder, relative, (scout_results, complete, missing, plan_known) = \
                    self._recovery_target(state, attempt)
            except BaseException:
                if json.dumps(state, sort_keys=True) != before:  # keep the reconciliation
                    state["last_updated_at"] = _now()
                    self._persist_state(state)
                raise
            number = 1
            while True:
                target = folder / f"recovery-{number:03d}"
                try:
                    target.mkdir()
                    break
                except FileExistsError:
                    number += 1
            resume_phase = state["current_phase"]
            entry = {
                "attempt": relative,
                "directory": target.relative_to(self.path).as_posix(),
                "status": "running",
                "started_at": _now(),
                "complete_evidence": complete,
                "missing_scouts": missing,
                # Lets a later reconciliation restore the lifecycle if this process dies.
                "previous_phase": resume_phase,
            }
            state["recoveries"].append(entry)
            state.update(current_phase="recovery", active_attempt=entry["directory"],
                         last_updated_at=_now(), next_action="Wait for organizer recovery")
            self._persist_state(state)

            oracle = None
            usage_recorded = False
            try:
                oracle = OracleSDK(
                    chains=len({r.chain for r in scout_results}), verbose=verbose,
                    local_tools=state["local_tools"], show_dollars=state["show_dollars"],
                )
                oracle.status(f"Organizer recovery {number} for {relative} | session: {self.path}")
                body = await oracle.reorganize(scout_results)
                round_number = int(_ATTEMPT_NAME.fullmatch(folder.name)[1])
                header = f"# Oracle round {round_number}: organizer recovery {number} ({relative})\n\n"
                if not complete:
                    saved = sum(not r.error for r in scout_results)
                    if not plan_known:
                        gap = "The planned scouts are unknown (the plan was not saved), so missing coverage cannot be listed."
                    elif missing:
                        gap = "Not retained: Smith " + ", ".join(f"#{i}" for i in missing) + "."
                    else:
                        gap = "Scouting did not record its final effective set."
                    header += f"> **Incomplete evidence set.** Organized the {saved} saved findings. {gap}\n\n"
                report = header + body
                _atomic_write(target / "report.md", report)
                metrics = asdict(oracle.metrics)
                metrics["elapsed_seconds"] = oracle.metrics.total_time
                metrics["total_usage"] = asdict(oracle.metrics.total_usage)
                metrics["replayed_from"] = relative
                metrics["complete_evidence"] = complete
                _write_json(target / "metrics.json", metrics)

                usage = oracle.metrics.total_usage
                entry.update(status="completed", finished_at=_now(),
                             research_outcome=self._research_outcome(oracle.metrics),
                             usage=self._usage_snapshot(usage))
                # Organizer-only spend, added once; the replayed scouts are not re-counted.
                state["reported_usage"] = self._record_usage(state, usage, had_prior_attempts=True)
                usage_recorded = True
                state["artifact_paths"] = self._artifact_paths(
                    self.path, state["artifact_paths"], target, ("report.md", "metrics.json")
                )
                state.update(current_phase=resume_phase, active_attempt=None, last_updated_at=_now(),
                             next_action=f"Read {entry['directory']}/report.md; the round budget is "
                                         "unchanged, so continue with a fresh plan")
                self._persist_state(state)
            except BaseException as exc:
                entry.update(status="failed", error=str(exc) or type(exc).__name__, finished_at=_now())
                if oracle is not None:
                    entry["usage"] = self._usage_snapshot(oracle.metrics.total_usage)
                    if not usage_recorded:
                        # Organizer spend happened; if it was not observed, the total becomes unknown.
                        state["reported_usage"] = self._record_usage(
                            state, oracle.metrics.total_usage, had_prior_attempts=True
                        )
                state.update(current_phase=resume_phase, active_attempt=None, last_updated_at=_now(),
                             next_action="Organizer recovery failed; inspect the error, then recover again or resume")
                try:
                    self._persist_state(state)
                except OSError:
                    pass
                raise
            oracle.status(f"Recovered report: {target / 'report.md'}")
            return report
