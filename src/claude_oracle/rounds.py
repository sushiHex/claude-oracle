"""Durable research rounds managed by the calling session."""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile


DEFAULT_ROUNDS = 1
SCHEMA_VERSION = 2

_OUTCOMES = {"complete", "partial", "failed", "unknown"}


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
    state.setdefault("reported_usage", {"input_tokens": 0, "output_tokens": 0, "quota_units": 0.0})
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
def _session_lock(path: Path):
    """One round writer per session; the OS releases the lock after a crash."""
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("This session already has a round running") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("This session already has a round running") from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


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
            "reported_usage": {"input_tokens": 0, "output_tokens": 0, "quota_units": 0.0},
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

    def status(self) -> dict:
        """Read the latest atomic snapshot without waiting for a running round."""
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
            or not isinstance(state.get("reported_usage"), dict)
        ):
            raise ValueError("Invalid Oracle round session state")
        return state

    def checkpoint(self, *, revision: str, through_round: int, path: str = "canonical.md") -> dict:
        """Record the caller's editorial checkpoint; never advances automatically."""
        state = self.status()
        if not isinstance(revision, str) or not revision.strip():
            raise ValueError("Checkpoint revision is required")
        if not isinstance(through_round, int) or not 0 <= through_round <= state["completed_rounds"]:
            raise ValueError("Checkpoint round must be between zero and completed rounds")
        state["checkpoint"] = {"path": path, "revision": revision, "through_round": through_round}
        state["last_updated_at"] = _now()
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
            for previous in state["attempts"]:
                if previous["status"] == "running":
                    previous.update(status="interrupted", finished_at=_now())
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
            state["progress"] = {"scouts": {"completed": 0, "total": len(prompts or [])}, "organizers": {"completed": 0, "total": len({p["chain"] for p in prompts or []})}}
            state["last_updated_at"] = _now()
            state["next_action"] = "Wait for research results"
            _write_json(self.path / "session.json", state)
            oracle = OracleSDK(
                chains=state["chains"], verbose=verbose,
                local_tools=state["local_tools"], show_dollars=state["show_dollars"],
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
                errors = oracle.metrics.scout_errors + oracle.metrics.compressor_errors
                state["research_outcome"] = "complete" if errors == 0 else "partial"
                state["current_phase"] = "complete"
                state["active_attempt"] = None
                state["progress"] = {"scouts": {"completed": oracle.metrics.scout_count, "total": oracle.scouts_total}, "organizers": {"completed": oracle.metrics.compressor_count, "total": oracle.metrics.chain_count}}
                usage = asdict(oracle.metrics.total_usage)
                total_usage = state["reported_usage"]
                for key in ("input_tokens", "output_tokens", "quota_units"):
                    total_usage[key] = total_usage.get(key, 0) + usage.get(key, 0)
                state["reported_usage"] = total_usage
                state["artifact_paths"] = [
                    (self.path / relative / name).as_posix()
                    for name in ("prompts.json", "report.md", "metrics.json")
                    if (self.path / relative / name).exists()
                ]
                state["last_updated_at"] = _now()
                state["next_action"] = "Revise canonical.md and record a checkpoint" if state["status"] == "rounds_complete" else "Prepare a fresh plan for the next round"
                _write_json(self.path / "session.json", state)
            except BaseException as exc:
                failed_usage = asdict(oracle.metrics.total_usage)
                attempt.update(status="failed", error=str(exc) or type(exc).__name__, finished_at=_now(), usage=failed_usage)
                total_usage = state["reported_usage"]
                for key in ("input_tokens", "output_tokens", "quota_units"):
                    total_usage[key] = total_usage.get(key, 0) + failed_usage.get(key, 0)
                state["reported_usage"] = total_usage
                state["completed_rounds"] = number - 1
                state["status"] = "failed"
                state["research_outcome"] = "failed"
                state["current_phase"] = "failed"
                state["active_attempt"] = None
                state["last_updated_at"] = _now()
                state["next_action"] = "Retry the failed round with a fresh plan"
                # Keep the original failure if recording it also fails (e.g. disk full).
                try:
                    if oracle.planned_prompts:
                        _write_json(folder / "prompts.json", oracle.planned_prompts)
                    _write_json(self.path / "session.json", state)
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
