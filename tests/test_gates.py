"""Tests for hybrid_critic_loop.gates / rubrics (Worker C).

Uses a FakeStore stub implementing the Store API subset gates.py codes
against -- deliberately NOT Worker A's state.py, so these tests stand
alone. Commands run through the real shellrun (shlex-split, no shell),
so command strings are built with shlex.quote.
"""

import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from hybrid_critic_loop.errors import GateError
from hybrid_critic_loop.gates import (
    check_provenance,
    compare,
    evaluate_candidate,
    run_hard_items,
    run_measured_items,
)
from hybrid_critic_loop.rubrics import RUBRICS, get_rubric

PY = shlex.quote(sys.executable)


def py_cmd(code: str) -> str:
    return f"{PY} -c {shlex.quote(code)}"


class FakeStore:
    """In-memory stub of the Store API subset used by gates/judges."""

    def __init__(self, experts=None):
        self.measurements = []
        self.scores = []
        self._experts = experts or {}

    def add_measurement(self, candidate_id, sensor, metric, value, op, threshold, passed):
        row = {
            "candidate_id": candidate_id,
            "sensor": sensor,
            "metric": metric,
            "value": value,
            "op": op,
            "threshold": threshold,
            "passed": passed,
        }
        self.measurements.append(row)
        return row

    def add_score(self, candidate_id, gate, rubric, judge_id, judge_family,
                  blind, position_shown, items, mean):
        row = {
            "candidate_id": candidate_id,
            "gate": gate,
            "rubric": rubric,
            "judge_id": judge_id,
            "judge_family": judge_family,
            "blind": blind,
            "position_shown": position_shown,
            "items": items,
            "mean": mean,
        }
        self.scores.append(row)
        return row

    def measurements_for(self, candidate_id):
        return [m for m in self.measurements if m["candidate_id"] == candidate_id]

    def scores_for(self, candidate_id):
        return [s for s in self.scores if s["candidate_id"] == candidate_id]

    def get_expert(self, expert_id):
        return self._experts[expert_id]


def generated_candidate(cid="cand-1"):
    return {
        "id": cid,
        "provenance": "generated",
        "evidence": {"log_hash": "sha256:abc123"},
        "family": "fam-alpha",
        "expert_id": "expert-alpha",
    }


class TestRubrics(unittest.TestCase):
    def test_rubrics_table(self):
        self.assertEqual(RUBRICS["code_quality_v1"]["version"], 1)
        self.assertEqual(RUBRICS["code_quality_v1"]["scale"], 10)
        self.assertEqual(
            RUBRICS["code_quality_v1"]["items"],
            ["correctness", "idiom", "readability"],
        )
        self.assertEqual(
            RUBRICS["intent_v1"]["items"],
            ["conformance", "completeness", "constraint_respect"],
        )

    def test_get_rubric_unknown_raises(self):
        with self.assertRaises(GateError):
            get_rubric("nope_v9")

    def test_get_rubric_returns_items(self):
        rubric = get_rubric("intent_v1")
        self.assertEqual(rubric["scale"], 10)
        self.assertIn("conformance", rubric["items"])


class TestCompare(unittest.TestCase):
    def test_all_ops(self):
        self.assertTrue(compare(5, "==", 5))
        self.assertFalse(compare(5, "==", 6))
        self.assertTrue(compare(5, "!=", 6))
        self.assertFalse(compare(5, "!=", 5))
        self.assertTrue(compare(4, "<", 5))
        self.assertFalse(compare(5, "<", 5))
        self.assertTrue(compare(5, "<=", 5))
        self.assertFalse(compare(6, "<=", 5))
        self.assertTrue(compare(6, ">", 5))
        self.assertFalse(compare(5, ">", 5))
        self.assertTrue(compare(5, ">=", 5))
        self.assertFalse(compare(4, ">=", 5))

    def test_unknown_op_raises(self):
        with self.assertRaises(GateError):
            compare(1, "=~", 1)
        with self.assertRaises(GateError):
            compare(1, "equals", 1)


class TestProvenance(unittest.TestCase):
    def test_generated_with_log_hash_passes(self):
        self.assertTrue(check_provenance(generated_candidate()))

    def test_mock_refused(self):
        cand = generated_candidate()
        cand["provenance"] = "mock"
        self.assertFalse(check_provenance(cand))

    def test_human_refused(self):
        cand = generated_candidate()
        cand["provenance"] = "human"
        self.assertFalse(check_provenance(cand))

    def test_missing_log_hash_refused(self):
        cand = generated_candidate()
        cand["evidence"] = {}
        self.assertFalse(check_provenance(cand))
        cand["evidence"] = {"log_hash": ""}
        self.assertFalse(check_provenance(cand))
        cand["evidence"] = None
        self.assertFalse(check_provenance(cand))
        del cand["evidence"]
        self.assertFalse(check_provenance(cand))


class TestHardGate(unittest.TestCase):
    def test_exit_match_and_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            contract = {"acceptance": [
                {"class": "hard", "cmd": py_cmd("import sys; sys.exit(0)"),
                 "expect": {"exit": 0}},
                {"class": "hard", "cmd": py_cmd("import sys; print('oops'); sys.exit(3)"),
                 "expect": {"exit": 0}},
            ]}
            results = run_hard_items(contract, td)
            self.assertEqual(len(results), 2)
            self.assertTrue(results[0].passed)
            self.assertEqual(results[0].exit_code, 0)
            self.assertFalse(results[1].passed)
            self.assertEqual(results[1].exit_code, 3)
            self.assertEqual(results[1].expected_exit, 0)

    def test_stdout_tail_capped(self):
        with tempfile.TemporaryDirectory() as td:
            contract = {"acceptance": [
                {"class": "hard", "cmd": py_cmd("print('x' * 5000)"),
                 "expect": {"exit": 0}},
            ]}
            (result,) = run_hard_items(contract, td)
            self.assertLessEqual(len(result.stdout_tail), 2000)


class TestMeasuredGate(unittest.TestCase):
    def _contract(self, threshold):
        return {"acceptance": [
            {"class": "measured", "sensor": "coverage-sensor",
             "sensor_cmd": py_cmd("import json; print(json.dumps({'coverage': 55, 'bugs': 0}))"),
             "metrics": [
                 {"name": "coverage", "op": ">=", "value": threshold},
                 {"name": "bugs", "op": "==", "value": 0},
             ]},
        ]}

    def test_stores_rows_and_fails_on_threshold(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory() as td:
            results = run_measured_items(store, self._contract(80), "cand-1", td)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].passed)  # coverage 55 < 80
        rows = store.measurements_for("cand-1")
        self.assertEqual(len(rows), 2)
        cov = [r for r in rows if r["metric"] == "coverage"][0]
        self.assertEqual(cov["value"], 55)
        self.assertEqual(cov["sensor"], "coverage-sensor")
        self.assertFalse(cov["passed"])
        self.assertEqual(len(results[0].metrics), 2)

    def test_passes_when_thresholds_met(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory() as td:
            results = run_measured_items(store, self._contract(50), "cand-1", td)
        self.assertTrue(results[0].passed)
        self.assertTrue(all(r["passed"] for r in store.measurements))

    def test_unparseable_sensor_output_raises_with_tail(self):
        store = FakeStore()
        contract = {"acceptance": [
            {"class": "measured", "sensor_cmd": py_cmd("print('not json at all')"),
             "metrics": [{"name": "x", "op": ">=", "value": 1}]},
        ]}
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(GateError) as ctx:
                run_measured_items(store, contract, "cand-1", td)
        self.assertIn("not json at all", str(ctx.exception))

    def test_missing_metric_raises(self):
        store = FakeStore()
        contract = {"acceptance": [
            {"class": "measured",
             "sensor_cmd": py_cmd("import json; print(json.dumps({'other': 1}))"),
             "metrics": [{"name": "coverage", "op": ">=", "value": 1}]},
        ]}
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(GateError):
                run_measured_items(store, contract, "cand-1", td)


class TestEvaluateCandidate(unittest.TestCase):
    def test_provenance_failure_runs_no_commands(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "hard_ran.txt"
            contract = {"acceptance": [
                {"class": "hard",
                 "cmd": py_cmd("open('hard_ran.txt', 'w').write('x')"),
                 "expect": {"exit": 0}},
            ]}
            cand = generated_candidate()
            cand["provenance"] = "mock"
            result = evaluate_candidate(store, contract, cand, td)
            self.assertFalse(result.provenance_ok)
            self.assertEqual(result.eliminated_at, "provenance")
            self.assertFalse(marker.exists())  # no command ever ran
            self.assertIsNone(result.measured_pass)

    def test_hard_fail_skips_measured_sensor(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "sensor_ran.txt"
            sensor_code = (
                "open('sensor_ran.txt', 'w').write('x'); "
                "import json; print(json.dumps({'coverage': 99}))"
            )
            contract = {"acceptance": [
                {"class": "hard", "cmd": py_cmd("import sys; sys.exit(1)"),
                 "expect": {"exit": 0}},
                {"class": "measured", "sensor_cmd": py_cmd(sensor_code),
                 "metrics": [{"name": "coverage", "op": ">=", "value": 80}]},
            ]}
            result = evaluate_candidate(store, contract, generated_candidate(), td)
            self.assertTrue(result.provenance_ok)
            self.assertEqual(result.eliminated_at, "hard")
            self.assertFalse(result.hard_pass)
            self.assertIsNone(result.measured_pass)
            self.assertEqual(result.measured, [])
            self.assertFalse(marker.exists())  # sensor never ran
            self.assertEqual(store.measurements, [])

    def test_full_pass(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory() as td:
            contract = {"acceptance": [
                {"class": "hard", "cmd": py_cmd("pass"), "expect": {"exit": 0}},
                {"class": "measured",
                 "sensor_cmd": py_cmd("import json; print(json.dumps({'coverage': 90}))"),
                 "metrics": [{"name": "coverage", "op": ">=", "value": 80}]},
            ]}
            result = evaluate_candidate(store, contract, generated_candidate(), td)
            self.assertIsNone(result.eliminated_at)
            self.assertTrue(result.hard_pass)
            self.assertTrue(result.measured_pass)
            self.assertEqual(len(store.measurements_for("cand-1")), 1)

    def test_measured_fail_eliminates_at_measured(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory() as td:
            contract = {"acceptance": [
                {"class": "hard", "cmd": py_cmd("pass"), "expect": {"exit": 0}},
                {"class": "measured",
                 "sensor_cmd": py_cmd("import json; print(json.dumps({'coverage': 10}))"),
                 "metrics": [{"name": "coverage", "op": ">=", "value": 80}]},
            ]}
            result = evaluate_candidate(store, contract, generated_candidate(), td)
            self.assertEqual(result.eliminated_at, "measured")
            self.assertTrue(result.hard_pass)
            self.assertFalse(result.measured_pass)

    def test_no_measured_items_measured_pass_is_none(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory() as td:
            contract = {"acceptance": [
                {"class": "hard", "cmd": py_cmd("pass"), "expect": {"exit": 0}},
            ]}
            result = evaluate_candidate(store, contract, generated_candidate(), td)
            self.assertIsNone(result.eliminated_at)
            self.assertIsNone(result.measured_pass)

    def test_contract_fixture_shape(self):
        # json round-trip: contracts arrive as plain JSON dicts.
        contract = json.loads(json.dumps({"acceptance": [
            {"class": "hard", "cmd": py_cmd("pass"), "expect": {"exit": 0}},
        ]}))
        store = FakeStore()
        with tempfile.TemporaryDirectory() as td:
            result = evaluate_candidate(store, contract, generated_candidate(), td)
        self.assertTrue(result.hard_pass)


if __name__ == "__main__":
    unittest.main()
