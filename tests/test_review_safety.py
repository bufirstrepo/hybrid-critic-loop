"""Regression tests for the submission review's safety boundaries."""

import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hybrid_critic_loop import cli, contracts, file_edits, gates, loop, plugin
from hybrid_critic_loop.errors import GateError
from hybrid_critic_loop.shellrun import RunResult
from tests.test_fix_round4_loop import RealGitBase, _contract


class SensorFailures(unittest.TestCase):
    def test_failed_or_timed_out_sensor_cannot_pass(self):
        item = {"class": "measured", "sensor_cmd": "sensor", "metrics": [
            {"name": "latency", "op": "<=", "value": 5}]}
        store = mock.Mock()
        for exit_code, timed_out in ((1, False), (None, True)):
            result = RunResult("sensor", exit_code, '{"latency":1}', "", 0, timed_out)
            with self.subTest(exit_code=exit_code), mock.patch.object(gates, "run_gate_command", return_value=result):
                with self.assertRaises(GateError):
                    gates.run_measured_items(store, {"acceptance": [item]}, "candidate", ".")
        store.add_measurement.assert_not_called()

    def test_baseline_does_not_accept_failed_sensor_output(self):
        result = RunResult("sensor", 1, '{"latency":1}', "", 0, False)
        store, mgr = mock.Mock(), mock.Mock()
        mgr.create.return_value = "."
        contract = {"id": "x", "mode": "improve", "acceptance": [{
            "class": "measured", "sensor_cmd": "sensor", "metrics": [
                {"name": "latency", "op": "<=", "value": 5}]}]}
        with mock.patch.object(contracts, "run_gate_command", return_value=result):
            proceed, detail = contracts.verify_baseline(store, contract, ".", mgr)
        self.assertFalse(proceed)
        self.assertIn("sensor failed", detail["measured"][0]["error"])


class FileBoundaries(unittest.TestCase):
    def test_redaction_failure_never_returns_original(self):
        with mock.patch.dict(sys.modules, {"agent.redact": None}):
            with self.assertRaisesRegex(RuntimeError, "redaction unavailable"):
                file_edits.redact_for_llm("sensitive")

    def test_masked_and_oversized_files_are_not_used_for_full_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.py"
            path.write_text("value = 'secret'\n")
            contract = {"task": "fix", "target_file": "config.py"}
            with mock.patch.object(file_edits, "redact_for_llm", return_value="value = '[redacted]'\n"):
                with self.assertRaisesRegex(ValueError, "redacted"):
                    file_edits.build_prompt_with_files(Path(td), contract, "")
            path.write_text("x" * 65537)
            with self.assertRaisesRegex(ValueError, "limit"):
                file_edits.build_prompt_with_files(Path(td), contract, "")

    def test_python_test_runner_must_be_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "tests").mkdir()
            with self.assertRaisesRegex(ValueError, "test_cmd"):
                plugin._infer_test_cmd(td)

    def test_noninteractive_cli_cannot_decide_gate(self):
        with mock.patch.object(cli.sys, "stdin") as stdin, mock.patch.object(cli, "_open") as open_store:
            stdin.isatty.return_value = False
            self.assertEqual(cli._cmd_gate("ctr", "approved", None, "."), 1)
            open_store.assert_not_called()


class SnapshotBoundaries(RealGitBase):
    def test_gate_mutation_cannot_become_a_winner(self):
        code = "from pathlib import Path; p=Path('solution.txt'); p.write_text('changed by test') if p.exists() else None; raise SystemExit(0 if p.exists() else 1)"
        cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
        result = loop.run_contract(self.store, _contract("ctr_mutating_gate", hard_cmd=cmd), self.repo, self._executors())
        self.assertEqual(result["status"], "escalated", result)
        self.assertIsNone(self.store.latest_verdict("ctr_mutating_gate").get("winner"))

    def test_missing_snapshot_never_falls_back_to_live_worktree(self):
        result = self._run_to_gate("ctr_missing_snap")
        winner = self.store.get_candidate(result["winner"])
        from hybrid_critic_loop.worktrees import WorktreeManager
        mgr = WorktreeManager(self.repo)
        mgr.delete_snapshot_ref("ctr_missing_snap", winner["id"])
        self.store.set_candidate_payload_hash(winner["id"], "missing")
        done = loop.record_human_gate(self.store, "ctr_missing_snap", "approved")
        self.assertEqual(done["status"], "error", done)
        self.assertIn("refusing live-worktree merge", done["detail"])

    def test_binary_mutation_is_detected_even_when_text_diff_is_unchanged(self):
        code = "from pathlib import Path; p=Path('blob.bin'); p.write_bytes(b'\\0b') if p.exists() else None; raise SystemExit(0 if p.exists() else 1)"
        cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
        seats = self._executors()
        for seat in seats.values():
            original = seat.generate
            def generate(wt, contract, context, original=original):
                result = original(wt, contract, context)
                (Path(wt) / "blob.bin").write_bytes(b"\0a")
                return result
            seat.generate = generate
        result = loop.run_contract(self.store, _contract("ctr_binary", hard_cmd=cmd), self.repo, seats)
        self.assertEqual(result["status"], "escalated", result)

    def test_budgeted_run_stops_before_model_dispatch(self):
        contract = _contract("ctr_budget")
        contract["budget"] = {"usd": 1}
        seats = self._executors()
        for seat in seats.values():
            seat.generate = mock.Mock(wraps=seat.generate)
        result = loop.run_contract(self.store, contract, self.repo, seats)
        self.assertEqual(result["status"], "escalated", result)
        for seat in seats.values():
            seat.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
