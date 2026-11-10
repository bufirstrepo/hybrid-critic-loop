"""Live run progress tracking and event broadcast for The Millionaire Matrix.

Writes atomic snapshots to ``<data_dir>/run_status.json`` during a run so CLI
(``/mm status``, ``hermes critic status``), tools (``critic_ledger``), and the
Desktop/Webapp UI (``dashboard/plugin_api.py`` + ``desktop/plugin.js``) can
inspect live execution without contending on SQLite write locks. When running
inside Hermes, also pushes ``plugin.the-millionaire-matrix.run.progress`` events
over the public ``hermes_cli.plugin_events`` surface.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_ID = "the-millionaire-matrix"
STATUS_FILENAME = "run_status.json"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def store_data_dir(store) -> Path | None:
    """Return the filesystem directory backing ``store``, or None for in-memory stores."""
    db_path = getattr(store, "db_path", None)
    if not db_path:
        return None
    text = str(db_path)
    if text == ":memory:" or text.startswith("file::memory:"):
        return None
    try:
        parent = Path(text).parent
        return parent if str(parent) else None
    except Exception:
        return None


def _broadcast(event: str, payload: dict) -> None:
    """Fire-and-forget broadcast to connected Hermes Desktop/TUI clients."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(PLUGIN_ID, event, payload)
    except Exception:
        pass


def write_run_status(data_dir: Path | None, payload: dict) -> None:
    """Atomically persist ``payload`` to ``<data_dir>/run_status.json`` and broadcast."""
    if data_dir is not None:
        try:
            root = Path(data_dir)
            root.mkdir(parents=True, exist_ok=True)
            dest = root / STATUS_FILENAME
            fd, tmp_name = tempfile.mkstemp(
                prefix=".run_status_", suffix=".tmp", dir=str(root)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2, sort_keys=True, default=str)
                os.replace(tmp_name, dest)
            finally:
                if os.path.exists(tmp_name):
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
        except Exception:
            pass
    _broadcast("run.progress", payload)


def read_run_status(data_dir: Path | None) -> dict:
    """Read ``<data_dir>/run_status.json``, returning an idle envelope when absent."""
    if data_dir is not None:
        path = Path(data_dir) / STATUS_FILENAME
        try:
            if path.is_file():
                obj = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    # Detect stale 'active: True' from a crashed process (>30m stale).
                    if obj.get("active"):
                        pid = obj.get("pid")
                        if isinstance(pid, int) and pid > 0 and not _pid_alive(pid):
                            obj = dict(obj)
                            obj["active"] = False
                            obj["phase"] = "interrupted"
                            obj["message"] = (
                                f"Previous run (pid {pid}) exited unexpectedly during "
                                f"{obj.get('phase', 'execution')}."
                            )
                    return obj
        except Exception:
            pass
    return {
        "active": False,
        "phase": "idle",
        "contract_id": None,
        "mode": None,
        "message": "Idle — no Millionaire Matrix run in progress.",
        "seats": {},
        "bracket": {"rounds": [], "ladder": []},
        "updated_at": None,
    }


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


class RunProgressTracker:
    """Thread-safe progress tracker used inside ``loop.py`` during a contract run."""

    def __init__(self, store, contract: dict, repo_path: str | Path, contestants: list[str], executors: dict):
        self.data_dir = store_data_dir(store)
        self._lock = threading.Lock()
        self._start_mono = time.monotonic()
        self.started_at = _utc_iso()
        self.contract_id = contract.get("id")
        self.mode = contract.get("mode", "fix")
        self.task = str(contract.get("task") or "")[:240]
        self.repo_path = str(repo_path)
        self.attempt = int(contract.get("attempt", 1))
        self.phase = "starting"
        self.active = True
        self.message = f"Starting Millionaire Matrix ({self.mode} mode) for {self.contract_id}"
        self.seats: dict[str, dict] = {}
        for eid in contestants:
            ex = executors.get(eid)
            self.seats[eid] = {
                "expert_id": eid,
                "family": getattr(ex, "family", None),
                "status": "pending",
                "model": getattr(ex, "default_model", None) or eid,
                "duration_ms": None,
                "eliminated_at": None,
                "score": None,
            }
        self.bracket: dict = {
            "current_round": 0,
            "field_in": len(contestants),
            "field_out": None,
            "ladder": [],
            "rounds": [],
        }
        self.matrix_progress: dict = {
            "round": 0,
            "judges_total": 0,
            "judges_done": 0,
        }
        self.winner: dict | None = None
        self.certificate_stamp: str | None = None

    def _snapshot_unlocked(self) -> dict:
        elapsed = round(max(0.0, time.monotonic() - self._start_mono), 1)
        counts: dict[str, int] = {}
        for info in self.seats.values():
            st = info.get("status") or "pending"
            counts[st] = counts.get(st, 0) + 1
        return {
            "active": self.active,
            "pid": os.getpid(),
            "contract_id": self.contract_id,
            "mode": self.mode,
            "attempt": self.attempt,
            "task": self.task,
            "repo_path": self.repo_path,
            "phase": self.phase,
            "message": self.message,
            "started_at": self.started_at,
            "updated_at": _utc_iso(),
            "elapsed_s": elapsed,
            "seat_counts": counts,
            "seats_total": len(self.seats),
            "seats": {k: dict(v) for k, v in self.seats.items()},
            "matrix_progress": dict(self.matrix_progress),
            "bracket": {
                "current_round": self.bracket["current_round"],
                "field_in": self.bracket["field_in"],
                "field_out": self.bracket["field_out"],
                "ladder": list(self.bracket["ladder"]),
                "rounds": list(self.bracket["rounds"]),
            },
            "winner": dict(self.winner) if self.winner else None,
            "certificate_stamp": self.certificate_stamp,
        }

    def emit(self, phase: str | None = None, message: str | None = None) -> None:
        with self._lock:
            if phase is not None:
                self.phase = phase
            if message is not None:
                self.message = message
            snap = self._snapshot_unlocked()
        write_run_status(self.data_dir, snap)

    def seat_started(self, expert_id: str, attempt: int) -> None:
        with self._lock:
            self.attempt = attempt
            if expert_id in self.seats:
                self.seats[expert_id]["status"] = "running"
            running = [k for k, v in self.seats.items() if v["status"] == "running"]
            done = sum(
                1 for v in self.seats.values()
                if v["status"] not in ("pending", "running")
            )
            self.phase = "generating"
            self.message = (
                f"Generating candidates ({done}/{len(self.seats)} finished; "
                f"active: {', '.join(running) or expert_id})"
            )
            snap = self._snapshot_unlocked()
        write_run_status(self.data_dir, snap)

    def seat_finished(self, expert_id: str, status: str, duration_ms: int = 0, model: str | None = None) -> None:
        with self._lock:
            entry = self.seats.setdefault(expert_id, {"expert_id": expert_id})
            entry["status"] = status
            entry["duration_ms"] = duration_ms
            if model:
                entry["model"] = model
            done = sum(
                1 for v in self.seats.values()
                if v.get("status") not in ("pending", "running")
            )
            completed = sum(1 for v in self.seats.values() if v.get("status") == "completed")
            self.phase = "generating"
            self.message = (
                f"Generated {done}/{len(self.seats)} seats "
                f"({completed} produced candidates; latest: {expert_id} → {status})"
            )
            snap = self._snapshot_unlocked()
        write_run_status(self.data_dir, snap)

    def gate_result(self, expert_id: str, eliminated_at: str | None) -> None:
        with self._lock:
            if expert_id in self.seats:
                self.seats[expert_id]["eliminated_at"] = eliminated_at
            self.phase = "gates"
            survivors = sum(
                1 for v in self.seats.values()
                if v.get("status") == "completed" and v.get("eliminated_at") is None
            )
            self.message = f"Evaluating hard & measured gates ({survivors} surviving candidates)"
            snap = self._snapshot_unlocked()
        write_run_status(self.data_dir, snap)

    def matrix_round_start(self, round_no: int, field_count: int, judges_total: int) -> None:
        with self._lock:
            self.phase = "matrix"
            self.bracket["current_round"] = round_no
            if not self.bracket["ladder"]:
                self.bracket["ladder"].append(field_count)
            self.matrix_progress = {
                "round": round_no,
                "judges_total": judges_total,
                "judges_done": 0,
            }
            self.message = (
                f"Round {round_no}: {judges_total} peer families blind-scoring "
                f"{field_count} surviving candidates"
            )
            snap = self._snapshot_unlocked()
        write_run_status(self.data_dir, snap)

    def matrix_judge_done(self, round_no: int, judge_id: str) -> None:
        with self._lock:
            if self.matrix_progress.get("round") == round_no:
                self.matrix_progress["judges_done"] = int(self.matrix_progress.get("judges_done", 0)) + 1
            done = self.matrix_progress.get("judges_done", 0)
            total = self.matrix_progress.get("judges_total", 0)
            self.message = f"Round {round_no} peer matrix: {done}/{total} judges scored (latest: {judge_id})"
            snap = self._snapshot_unlocked()
        write_run_status(self.data_dir, snap)

    def bracket_step(self, round_no: int, field_in: int, field_out: int, scores_by_expert: dict[str, float | None], cut_experts: list[str]) -> None:
        with self._lock:
            self.phase = "bracket"
            self.bracket["current_round"] = round_no
            self.bracket["field_in"] = field_in
            self.bracket["field_out"] = field_out
            if not self.bracket["ladder"] or self.bracket["ladder"][-1] != field_in:
                self.bracket["ladder"].append(field_in)
            if self.bracket["ladder"][-1] != field_out:
                self.bracket["ladder"].append(field_out)
            self.bracket["rounds"].append({
                "round": round_no,
                "field_in": field_in,
                "field_out": field_out,
                "cut_experts": list(cut_experts),
            })
            for eid, sc in scores_by_expert.items():
                if eid in self.seats and sc is not None:
                    self.seats[eid]["score"] = round(float(sc), 2)
            for eid in cut_experts:
                if eid in self.seats and self.seats[eid].get("eliminated_at") is None:
                    self.seats[eid]["eliminated_at"] = f"bracket_r{round_no}"
            ladder_str = " → ".join(str(n) for n in self.bracket["ladder"])
            self.message = f"Bracket round {round_no}: {field_in} → {field_out} (ladder: {ladder_str})"
            snap = self._snapshot_unlocked()
        write_run_status(self.data_dir, snap)

    def finish(self, phase: str, message: str, winner_expert: str | None = None, winner_family: str | None = None, certificate_stamp: str | None = None) -> None:
        with self._lock:
            self.active = False
            self.phase = phase
            self.message = message
            if winner_expert:
                self.winner = {"expert_id": winner_expert, "family": winner_family}
            if certificate_stamp:
                self.certificate_stamp = certificate_stamp
            snap = self._snapshot_unlocked()
        write_run_status(self.data_dir, snap)


def format_status_report(data_dir: Path | None, panel_executors: dict | None = None) -> str:
    """Human-readable status report for ``/mm status`` and ``hermes critic status``."""
    st = read_run_status(data_dir)
    lines: list[str] = []
    state_label = "RUNNING" if st.get("active") else str(st.get("phase") or "idle").upper()
    lines.append(f"Millionaire Matrix status: {state_label}")
    if st.get("contract_id"):
        mode = st.get("mode") or "fix"
        elapsed = st.get("elapsed_s")
        el_str = f" · {elapsed}s" if elapsed is not None else ""
        lines.append(f"  Contract: {st['contract_id']} ({mode} mode){el_str}")
    if st.get("message"):
        lines.append(f"  Detail:   {st['message']}")
    ladder = (st.get("bracket") or {}).get("ladder") or []
    if ladder:
        lines.append(f"  Bracket:  {' -> '.join(str(x) for x in ladder)}")
    if st.get("winner"):
        w = st["winner"]
        lines.append(f"  Winner:   {w.get('expert_id')} ({w.get('family')})")
    if st.get("certificate_stamp"):
        lines.append(f"  Receipt:  {st['certificate_stamp']}")
    seats = st.get("seats") or {}
    if seats:
        lines.append("  Run seats:")
        for eid, info in seats.items():
            fam = info.get("family") or "?"
            s_state = info.get("status") or "pending"
            elim = info.get("eliminated_at")
            score = info.get("score")
            model = info.get("model")
            extra = []
            if model and model != eid:
                extra.append(str(model))
            if score is not None:
                extra.append(f"score={score}")
            if elim:
                extra.append(f"out={elim}")
            tail = f" ({', '.join(extra)})" if extra else ""
            lines.append(f"    - {eid:<14} [{fam:<10}] {s_state}{tail}")
    if panel_executors:
        lines.append("  Configured 8-family panel readiness:")
        ready = 0
        for eid, ex in panel_executors.items():
            try:
                ok, detail = ex.available()
            except Exception as exc:
                ok, detail = False, str(exc)
            if ok:
                ready += 1
            mark = "READY" if ok else "UNAVAIL"
            lines.append(f"    - {eid:<14} [{getattr(ex, 'family', '?'):<10}] {mark}: {detail}")
        lines.append(f"  Panel ratio: {ready}/{len(panel_executors)} families ready -> 1 winner")
    return "\n".join(lines)
