"""Regression tests for fix pass round 4, Unit 4 (inputs) — spec G6/G7/G9.

Round-4 security F1/F3/F5/F6/F8, round-4 contest-core F5/F6/F11,
round-3 contest-core F1/F2/F3/F4/F6, round-3 regression F4. Each test
class names the finding it pins. Contracts/gates here run real commands
through the real shellrun in temp dirs; matrix/judges use the suites'
FakeStore patterns.
"""

import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hybrid_critic_loop.contracts import validate_contract, verify_baseline
from hybrid_critic_loop.errors import ContractError, GateError
from hybrid_critic_loop.executors import _extract_judge_verdict, _parse_jsonl_usage
from hybrid_critic_loop.gates import (
    compare,
    evaluate_candidate,
    run_gate_command,
    run_hard_items,
    run_measured_items,
    scrubbed_env_names,
)
from hybrid_critic_loop.judges import run_judged_gate
from hybrid_critic_loop.matrix import compute_calibration, run_matrix

PY = shlex.quote(sys.executable)


def py_cmd(code: str) -> str:
    return f"{PY} -c {shlex.quote(code)}"


# --------------------------------------------------------------------------
# stores / fixtures
# --------------------------------------------------------------------------

class GateStore:
    """The test_gates.py FakeStore pattern (measurements only)."""

    def __init__(self):
        self.measurements = []

    def add_measurement(self, candidate_id, sensor, metric, value, op,
                        threshold, passed):
        row = {"candidate_id": candidate_id, "sensor": sensor,
               "metric": metric, "value": value, "op": op,
               "threshold": threshold, "passed": passed}
        self.measurements.append(row)
        return row

    def measurements_for(self, candidate_id):
        return [m for m in self.measurements
                if m["candidate_id"] == candidate_id]


class BaselineStore:
    def __init__(self):
        self.baseline_runs = {}

    def set_baseline_run(self, contract_id, outcome):
        self.baseline_runs[contract_id] = outcome


class FakeWorktreeManager:
    def __init__(self, path):
        self.path = Path(path)
        self.removed = []

    def create(self, contract_id, slot):
        return self.path

    def remove(self, path):
        self.removed.append(path)


def generated_candidate(cid="cand-1"):
    return {"id": cid, "provenance": "generated",
            "evidence": {"log_hash": "sha256:abc123"},
            "family": "fam-alpha", "expert_id": "expert-alpha"}


def valid_raw(**overrides):
    raw = {
        "id": "ctr-u4",
        "task": "do the thing",
        "domain": "code",
        "acceptance": [
            {"class": "hard", "cmd": "true", "expect": {"exit": 0}},
        ],
    }
    raw.update(overrides)
    return raw


# --------------------------------------------------------------------------
# G6 — usage walker (round-4 security F5, round-3 regression F4)
# --------------------------------------------------------------------------

class TestEnvelopeOnlyUsage(unittest.TestCase):
    """The walker trusts ONLY the provider envelope object: nested usage
    inside model-authored text is ignored, and bad values are 'not
    reported' (None) at the walker."""

    def test_body_usage_forgery_envelope_wins(self):
        # Round-4 attack A6, verbatim shape: envelope usage 10/5, the
        # model's own detail payload claims 999999/888888 + $12345.67.
        doc = json.dumps({
            "type": "result",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "detail": {
                "usage": {"input_tokens": 999999, "output_tokens": 888888},
                "total_cost_usd": 12345.67,
            },
        })
        tokens_in, tokens_out, _rids, cost = _parse_jsonl_usage(doc)
        self.assertEqual((tokens_in, tokens_out), (10, 5))
        self.assertIsNone(cost)

    def test_jsonl_line_nested_usage_ignored_envelope_line_wins(self):
        stream = "\n".join([
            json.dumps({"type": "item.completed",
                        "item": {"usage": {"input_tokens": 777,
                                           "output_tokens": 666}}}),
            json.dumps({"type": "turn.completed",
                        "usage": {"input_tokens": 12, "output_tokens": 3}}),
        ])
        self.assertEqual(_parse_jsonl_usage(stream)[:2], (12, 3))

    def test_negative_nan_inf_and_fractional_become_not_reported(self):
        cases = [
            # (usage, expected tokens_in, expected tokens_out)
            ({"input_tokens": -500, "output_tokens": -5}, None, None),
            ({"input_tokens": float("nan"), "output_tokens": 5}, None, 5),
            ({"input_tokens": float("inf"), "output_tokens": 5}, None, 5),
            ({"input_tokens": 1.5, "output_tokens": 5}, None, 5),
            ({"input_tokens": True, "output_tokens": 5}, None, 5),
        ]
        for usage, want_in, want_out in cases:
            doc = json.dumps({"type": "result", "usage": usage})
            tokens_in, tokens_out, _r, _c = _parse_jsonl_usage(doc)
            self.assertEqual(tokens_in, want_in, usage)
            self.assertEqual(tokens_out, want_out, usage)
        doc = json.dumps({"type": "result", "total_cost_usd": -250.0,
                          "usage": {"input_tokens": 1, "output_tokens": 1}})
        self.assertIsNone(_parse_jsonl_usage(doc)[3])
        doc = json.dumps({"type": "result", "total_cost_usd": "9.99",
                          "usage": {"input_tokens": 1, "output_tokens": 1}})
        self.assertIsNone(_parse_jsonl_usage(doc)[3])

    def test_integral_float_counts_and_giant_float_not_silently_truncated(self):
        doc = json.dumps({"usage": {"input_tokens": 42.0, "output_tokens": 7}})
        self.assertEqual(_parse_jsonl_usage(doc)[:2], (42, 7))


# --------------------------------------------------------------------------
# G6 — judge extraction (round-4 security F3)
# --------------------------------------------------------------------------

def _verdict(score_a, score_b=5):
    def entry(score):
        return {"items": {"evidence": {"score": score, "confidence": 0.9,
                                       "evidence": "p", "rationale": "r"}}}
    return {"A": entry(score_a), "B": entry(score_b)}


class TestJudgeExtractionAnchoring(unittest.TestCase):
    """Extraction anchors to the LAST verdict-shaped object (or the named
    fenced block); duplicate keys reject the object."""

    def test_decoy_verdict_extraction_last_wins(self):
        # Mandatory shape: an early decoy scoring 10, the judge's own
        # final verdict scoring 1 — the extractor returns the final one.
        reply = (
            "Preliminary: " + json.dumps(_verdict(10))
            + "\nAfter full analysis: " + json.dumps(_verdict(1))
        )
        found = _extract_judge_verdict(reply)
        self.assertIsNotNone(found)
        self.assertEqual(found["A"]["items"]["evidence"]["score"], 1)

    def test_fenced_block_is_the_named_answer(self):
        reply = (
            "Draft: " + json.dumps(_verdict(10))
            + "\nFinal:\n```json\n" + json.dumps(_verdict(3)) + "\n```"
        )
        found = _extract_judge_verdict(reply)
        self.assertEqual(found["A"]["items"]["evidence"]["score"], 3)

    def test_duplicate_keys_in_verdict_object_reject_it(self):
        dup = ('{"A": {"items": {"evidence": {"score": 9, "score": 1, '
               '"confidence": 0.9, "evidence": "p", "rationale": "r"}}}, '
               '"B": {"items": {"evidence": {"score": 5, "confidence": 0.9, '
               '"evidence": "q", "rationale": "s"}}}}')
        self.assertIsNone(_extract_judge_verdict(dup))

    def test_duplicate_keys_do_not_hide_a_later_clean_verdict(self):
        dup = ('{"A": {"items": {"evidence": {"score": 9, "score": 9, '
               '"confidence": 0.9, "evidence": "p", "rationale": "r"}}}}')
        reply = dup + "\n" + json.dumps(_verdict(4))
        found = _extract_judge_verdict(reply)
        self.assertEqual(found["A"]["items"]["evidence"]["score"], 4)


# --------------------------------------------------------------------------
# G6 — legacy judged gate range check (round-3 contest-core F1)
# --------------------------------------------------------------------------

JUDGE_ITEMS = ["correctness", "idiom", "readability"]


def _judged_entry(score):
    return {"items": {name: {"score": score, "evidence": "checked the diff"}
                      for name in JUDGE_ITEMS}}


class _StubJudge:
    def __init__(self, family, responder):
        self.family = family
        self._responder = responder

    def judge(self, packet, rubric):
        if callable(self._responder):
            return self._responder(packet, rubric)
        return self._responder


class TestJudgedGateRangeCheck(unittest.TestCase):
    """judges.py's validator mirrors matrix.py's: a score outside the
    rubric [0, scale] discards the whole score."""

    def _run(self, store, executors, panel):
        cands = [
            {"id": "cand-1", "family": "fam-alpha",
             "expert_id": "expert-alpha", "provenance": "generated",
             "evidence": {"log_hash": "sha256:1"}},
            {"id": "cand-2", "family": "fam-beta",
             "expert_id": "expert-beta", "provenance": "generated",
             "evidence": {"log_hash": "sha256:2"}},
        ]
        item = {"class": "judged", "rubric": "code_quality_v1", "min": 7,
                "judges": 3, "spread_threshold": 2.0}
        return run_judged_gate(store, {"acceptance": []}, item, cands, {},
                               {"cand-1": "+x", "cand-2": "+y"},
                               executors, panel, seed=0)

    def test_off_scale_score_100_is_discarded(self):
        store = GateStore()
        store.scores = []

        def add_score(*args, **kwargs):
            store.scores.append(args)

        store.add_score = add_score
        canned = {label: _judged_entry(100) for label in ("A", "B")}
        executors = {
            "jg": _StubJudge("fam-gamma", canned),
            "jd": _StubJudge("fam-delta",
                             {label: _judged_entry(8) for label in ("A", "B")}),
            "je": _StubJudge("fam-epsilon",
                             {label: _judged_entry(8) for label in ("A", "B")}),
        }
        outcome = self._run(store, executors, ["jg", "jd", "je"])
        # The 100-scoring judge contributed nothing; the panel fell below
        # strength and the run escalated instead of passing on 100s.
        self.assertNotIn("jg", outcome.judges_used)
        self.assertTrue(outcome.escalated)
        for cid in ("cand-1", "cand-2"):
            self.assertLessEqual(
                outcome.per_candidate[cid]["median_mean"], 10.0)

    def test_negative_score_is_discarded(self):
        store = GateStore()
        store.scores = []
        store.add_score = lambda *a, **k: store.scores.append(a)
        canned = {label: _judged_entry(-50) for label in ("A", "B")}
        executors = {
            "jg": _StubJudge("fam-gamma", canned),
            "jd": _StubJudge("fam-delta",
                             {label: _judged_entry(8) for label in ("A", "B")}),
            "je": _StubJudge("fam-epsilon",
                             {label: _judged_entry(8) for label in ("A", "B")}),
        }
        outcome = self._run(store, executors, ["jg", "jd", "je"])
        self.assertNotIn("jg", outcome.judges_used)
        self.assertEqual(outcome.per_candidate["cand-1"]["min_item"], 8.0)


# --------------------------------------------------------------------------
# G7 — contract validation (round-4 security F6, contest-core F5/F11)
# --------------------------------------------------------------------------

class TestContractValidationRound4(unittest.TestCase):
    def test_id_is_required(self):
        raw = valid_raw()
        del raw["id"]
        with self.assertRaises(ContractError):
            validate_contract(raw)

    def test_id_charset_rejects_escapes_and_separators(self):
        for bad in ("../../escape", "..", "a/b", "a\\b", "a.b", ".hidden",
                    "-leading-dash", "", "has space", None, 42):
            with self.assertRaises(ContractError, msg=repr(bad)):
                validate_contract(valid_raw(id=bad))
        for good in ("ctr-1", "my_contract_2", "A", "x" * 128):
            self.assertEqual(validate_contract(valid_raw(id=good))["id"], good)

    def test_judges_must_be_at_least_one(self):
        judged = {"class": "judged", "rubric": "code_quality_v1", "min": 7}
        for bad in (0, -1, "three", 2.5, True):
            item = dict(judged, judges=bad)
            with self.assertRaises(ContractError, msg=repr(bad)):
                validate_contract(valid_raw(acceptance=[
                    {"class": "hard", "cmd": "true", "expect": {"exit": 0}},
                    item,
                ]))
        item = dict(judged, judges=1)
        out = validate_contract(valid_raw(acceptance=[
            {"class": "hard", "cmd": "true", "expect": {"exit": 0}}, item]))
        self.assertEqual(out["acceptance"][1]["judges"], 1)

    def test_expect_exit_strict_typing(self):
        # A bool stays a bool; an int stays an int; anything else is a
        # ContractError (not a bare ValueError, not a silent coercion).
        out = validate_contract(valid_raw(acceptance=[
            {"class": "hard", "cmd": "true", "expect": {"exit": True}}]))
        self.assertIs(out["acceptance"][0]["expect"]["exit"], True)
        out = validate_contract(valid_raw(acceptance=[
            {"class": "hard", "cmd": "true", "expect": {"exit": 3}}]))
        self.assertEqual(out["acceptance"][0]["expect"]["exit"], 3)
        self.assertIs(type(out["acceptance"][0]["expect"]["exit"]), int)
        for bad in ("0", 0.0, [0]):
            with self.assertRaises(ContractError, msg=repr(bad)):
                validate_contract(valid_raw(acceptance=[
                    {"class": "hard", "cmd": "true", "expect": {"exit": bad}}]))

    def test_exit_true_is_never_satisfied_by_exit_code_1(self):
        # The 1 == True trap, at the gate itself.
        with tempfile.TemporaryDirectory() as td:
            contract = {"acceptance": [
                {"class": "hard",
                 "cmd": py_cmd("import sys; sys.exit(1)"),
                 "expect": {"exit": True}},
                {"class": "hard",
                 "cmd": py_cmd("import sys; sys.exit(1)"),
                 "expect": {"exit": 1}},
            ]}
            results = run_hard_items(contract, td)
        self.assertFalse(results[0].passed)   # exit 1 does NOT satisfy true
        self.assertTrue(results[1].passed)    # exit 1 satisfies int 1

    def test_attempt_type_errors_are_contract_errors(self):
        for bad in ("two", None, 1.5, True):
            with self.assertRaises(ContractError, msg=repr(bad)):
                validate_contract(valid_raw(attempt=bad))
        self.assertEqual(validate_contract(valid_raw(attempt=2))["attempt"], 2)

    def test_timeout_s_carried_capped_and_validated(self):
        out = validate_contract(valid_raw(acceptance=[
            {"class": "hard", "cmd": "true", "expect": {"exit": 0},
             "timeout_s": 5},
            {"class": "measured", "sensor_cmd": "true",
             "metrics": [{"name": "m", "op": "<=", "value": 1}],
             "timeout_s": 99999},
        ]))
        self.assertEqual(out["acceptance"][0]["timeout_s"], 5)
        self.assertEqual(out["acceptance"][1]["timeout_s"], 3600)  # capped
        # No timeout_s in the source -> no key in the normalized item.
        out = validate_contract(valid_raw())
        self.assertNotIn("timeout_s", out["acceptance"][0])
        for bad in (0, -3, "1", float("nan")):
            with self.assertRaises(ContractError, msg=repr(bad)):
                validate_contract(valid_raw(acceptance=[
                    {"class": "hard", "cmd": "true", "expect": {"exit": 0},
                     "timeout_s": bad}]))


# --------------------------------------------------------------------------
# G7 — timeout honored at the baseline and at the gate (contest-core F5)
# --------------------------------------------------------------------------

class TestTimeoutHonored(unittest.TestCase):
    def test_baseline_hard_command_bounded_by_timeout_s(self):
        contract = validate_contract(valid_raw(acceptance=[
            {"class": "hard",
             "cmd": py_cmd("import time; time.sleep(3)"),
             "expect": {"exit": 0}, "timeout_s": 1},
        ]))
        store = BaselineStore()
        with tempfile.TemporaryDirectory() as td:
            mgr = FakeWorktreeManager(td)
            start = time.monotonic()
            proceed, detail = verify_baseline(store, contract, td, mgr)
            elapsed = time.monotonic() - start
        # Killed at ~1s (not the 3s sleep, not the 900s default); the
        # item records as failed on baseline, so the run proceeds.
        self.assertLess(elapsed, 2.5)
        self.assertTrue(proceed)
        self.assertFalse(detail["hard"][0]["passed"])
        self.assertIsNone(detail["hard"][0]["exit_code"])

    def test_gate_command_bounded_by_timeout_s(self):
        store = GateStore()
        contract = {"acceptance": [
            {"class": "hard",
             "cmd": py_cmd("import time; time.sleep(3)"),
             "expect": {"exit": 0}, "timeout_s": 1},
        ]}
        with tempfile.TemporaryDirectory() as td:
            start = time.monotonic()
            (result,) = run_hard_items(contract, td)
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.5)
        self.assertFalse(result.passed)
        self.assertIsNone(result.exit_code)


# --------------------------------------------------------------------------
# G7 — gate environment scrub (round-4 security F1)
# --------------------------------------------------------------------------

class TestGateEnvScrub(unittest.TestCase):
    FAKE_ENV = {
        "DEEPSEEK_API_KEY": "sk-test",
        "MY_TOKEN": "t",
        "APP_SECRET": "s",
        "DB_PASSWORD": "p",
        "KEYSTONE": "not-a-secret-suffix",   # no underscore: kept
        "PLAIN_VAR": "v",
        "PATH": os.environ.get("PATH", ""),
    }

    def test_scrubbed_names(self):
        names = scrubbed_env_names(self.FAKE_ENV)
        self.assertEqual(names, ["APP_SECRET", "DB_PASSWORD",
                                 "DEEPSEEK_API_KEY", "MY_TOKEN"])

    def test_gate_command_cannot_read_secrets_but_sees_plain_vars(self):
        store = GateStore()
        with mock.patch.dict(os.environ, self.FAKE_ENV, clear=False):
            with tempfile.TemporaryDirectory() as td:
                # Sensor prints the env view as its metrics instead:
                code2 = (
                    "import json, os; print(json.dumps({"
                    " 'leaked': 1 if os.environ.get('DEEPSEEK_API_KEY') else 0,"
                    " 'plain_ok': 1 if os.environ.get('PLAIN_VAR') == 'v' else 0}))"
                )
                contract = {"acceptance": [
                    {"class": "measured", "sensor_cmd": py_cmd(code2),
                     "metrics": [
                         {"name": "leaked", "op": "==", "value": 0},
                         {"name": "plain_ok", "op": "==", "value": 1},
                     ]},
                ]}
                results = run_measured_items(store, contract, "cand-1", td)
        self.assertTrue(results[0].passed)  # no leak, plain var visible

    def test_scrub_applies_to_baseline_commands(self):
        code = (
            "import json, os; print(json.dumps({"
            " 'leaked': 1 if os.environ.get('DEEPSEEK_API_KEY') else 0}))"
        )
        contract = validate_contract(valid_raw(acceptance=[
            {"class": "measured", "sensor_cmd": py_cmd(code),
             "metrics": [{"name": "leaked", "op": "==", "value": 0}]},
        ]))
        store = BaselineStore()
        with mock.patch.dict(os.environ, self.FAKE_ENV, clear=False):
            with tempfile.TemporaryDirectory() as td:
                proceed, detail = verify_baseline(
                    store, contract, td, FakeWorktreeManager(td))
        # Sensor saw no secret -> leaked == 0 -> metric PASSES on
        # baseline -> vacuous -> proceed False. (Had the secret leaked,
        # the metric would fail and the run would proceed.)
        self.assertFalse(proceed)


# --------------------------------------------------------------------------
# G7 — measured type discipline + atomic rows (security F8, contest F6)
# --------------------------------------------------------------------------

class TestMeasuredTypeDiscipline(unittest.TestCase):
    def test_string_metric_under_ordered_op_raises_gate_error(self):
        with self.assertRaises(GateError):
            compare("9", "<=", 8)

    def test_bool_metric_never_passes_as_one(self):
        with self.assertRaises(GateError):
            compare(True, "==", 1)
        with self.assertRaises(GateError):
            compare(1, "==", True)

    def test_mixed_equality_raises_and_string_equality_still_works(self):
        with self.assertRaises(GateError):
            compare("5", "==", 5)
        self.assertTrue(compare("fast", "==", "fast"))
        self.assertTrue(compare(5, "<=", 8))

    def test_string_reading_raises_gate_error_for_the_loop_to_convert(self):
        # gates raises GateError on an unjudgeable reading; converting it
        # to the candidate's elimination is the loop's half (spec G7,
        # round-4 contest-core F3). What gates guarantees: the error is
        # a GateError (not a TypeError escaping the gate path) and no
        # measurement rows are stored for the item.
        store = GateStore()
        contract = {"acceptance": [
            {"class": "measured",
             "sensor_cmd": py_cmd(
                 "import json; print(json.dumps({'ops': '9'}))"),
             "metrics": [{"name": "ops", "op": "<=", "value": 8}]},
        ]}
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(GateError):
                evaluate_candidate(store, contract, generated_candidate(), td)
        self.assertEqual(store.measurements, [])

    def test_multi_metric_item_records_all_rows_or_none(self):
        store = GateStore()
        contract = {"acceptance": [
            {"class": "measured",
             "sensor_cmd": py_cmd(
                 "import json; print(json.dumps({'ops': 5}))"),
             "metrics": [
                 {"name": "ops", "op": "<=", "value": 8},
                 {"name": "missing_metric", "op": "<=", "value": 1},
             ]},
        ]}
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(GateError):
                run_measured_items(store, contract, "cand-1", td)
        # The first metric compared fine, but the item errored: no rows.
        self.assertEqual(store.measurements_for("cand-1"), [])


# --------------------------------------------------------------------------
# G9 — baseline rule (round-3 contest-core F3)
# --------------------------------------------------------------------------

class TestBaselineRuleMixedContract(unittest.TestCase):
    def test_hard_passes_measured_fails_proceeds(self):
        contract = validate_contract(valid_raw(acceptance=[
            {"class": "hard", "cmd": py_cmd("pass"),
             "expect": {"exit": 0}},
            {"class": "measured",
             "sensor_cmd": py_cmd(
                 "import json; print(json.dumps({'ops': 99}))"),
             "metrics": [{"name": "ops", "op": "<=", "value": 8}]},
        ]))
        store = BaselineStore()
        with tempfile.TemporaryDirectory() as td:
            proceed, detail = verify_baseline(
                store, contract, td, FakeWorktreeManager(td))
        self.assertTrue(proceed)
        self.assertEqual(store.baseline_runs["ctr-u4"], "fail")
        self.assertTrue(detail["hard"][0]["passed"])
        self.assertFalse(detail["measured"][0]["metrics"][0]["passed"])

    def test_everything_passing_is_still_vacuous(self):
        contract = validate_contract(valid_raw(acceptance=[
            {"class": "hard", "cmd": py_cmd("pass"),
             "expect": {"exit": 0}},
            {"class": "measured",
             "sensor_cmd": py_cmd(
                 "import json; print(json.dumps({'ops': 3}))"),
             "metrics": [{"name": "ops", "op": "<=", "value": 8}]},
        ]))
        store = BaselineStore()
        with tempfile.TemporaryDirectory() as td:
            proceed, _detail = verify_baseline(
                store, contract, td, FakeWorktreeManager(td))
        self.assertFalse(proceed)
        self.assertEqual(store.baseline_runs["ctr-u4"], "pass")


# --------------------------------------------------------------------------
# G9 — matrix: per-candidate dissent, round supersede, decidability
# (round-3 contest-core F4 / F6 / F2)
# --------------------------------------------------------------------------

MATRIX_ROWS = ["question_fidelity", "completeness", "evidence",
               "test_agreement"]
MATRIX_MARKERS = {"cand-a": "TOKAQX", "cand-b": "TOKBQX",
                  "cand-c": "TOKCQX", "cand-d": "TOKDQX"}


class MatrixStore:
    """The test_matrix.py FakeStore pattern: candidates + verdicts."""

    def __init__(self, candidates=None, verdicts=None):
        self.scores = []
        self.candidates = list(candidates or [])
        self._verdicts = dict(verdicts or {})
        self.calibration_updates = []

    def add_score(self, candidate_id, gate, rubric, judge_id, judge_family,
                  blind, position_shown, items, mean, confidence=None,
                  details=None):
        row = {"candidate_id": candidate_id, "gate": gate, "rubric": rubric,
               "judge_id": judge_id, "judge_family": judge_family,
               "blind": blind, "position_shown": position_shown,
               "items": items, "mean": mean, "confidence": confidence,
               "details": details}
        self.scores.append(row)
        return row

    def scores_for(self, candidate_id):
        return [s for s in self.scores if s["candidate_id"] == candidate_id]

    def latest_verdict(self, contract_id):
        return self._verdicts.get(contract_id)

    def update_expert_calibration(self, expert_id, value, n):
        self.calibration_updates.append((expert_id, value, n))


def _cell(scores, confidence=0.9, evidence="src/impl.py:42",
          rationale="addresses the contract item"):
    items = {}
    for row, spec in scores.items():
        if isinstance(spec, dict) and "items" in spec:
            # A ready-made entry (e.g. built by an earlier _cell call):
            # hand the judge's reply the entry itself, not a score dict.
            items[row] = spec
        elif isinstance(spec, dict):
            items[row] = dict(spec)
        else:
            items[row] = {"score": spec, "confidence": confidence,
                          "evidence": evidence, "rationale": rationale}
    return {"items": items}


def _uniform(score):
    return {row: score for row in MATRIX_ROWS}


class _StubPeer:
    """plan maps candidate_id -> row-score spec (see _cell) or a
    ready-made entry dict with an "items" key, passed through as-is."""

    def __init__(self, family, plan):
        self.family = family
        self.plan = plan

    def judge(self, packet, rubric):
        import re as _re
        result = {}
        sections = _re.split(r"=== CANDIDATE ([A-Z]+) ===", packet)
        for i in range(1, len(sections) - 1, 2):
            label, body = sections[i], sections[i + 1]
            for cid, marker in MATRIX_MARKERS.items():
                if marker not in body or cid not in self.plan:
                    continue
                spec = self.plan[cid]
                if isinstance(spec, dict) and "items" in spec:
                    result[label] = spec
                else:
                    result[label] = _cell(spec)
        return result


def _matrix_candidates(ids=("cand-a", "cand-b", "cand-c")):
    fams = {"cand-a": ("fam-a", "expert-a"), "cand-b": ("fam-b", "expert-b"),
            "cand-c": ("fam-c", "expert-c"), "cand-d": ("fam-d", "expert-d")}
    return [{"id": cid, "family": fams[cid][0], "expert_id": fams[cid][1],
             "contract_id": "ctr-1", "provenance": "generated",
             "evidence": {"log_hash": f"sha256:{cid}"}} for cid in ids]


def _matrix_gate_results(cands):
    from hybrid_critic_loop.gates import (
        CandidateGateResult, HardResult, MeasureResult)
    out = {}
    for c in cands:
        cid = c["id"]
        out[cid] = CandidateGateResult(
            cid, True, None,
            [HardResult(cmd="python3 -m unittest tests.test_smoke",
                        expected_exit=0, exit_code=0, passed=True)],
            [MeasureResult(
                sensor="coverage-sensor",
                metrics=[{"candidate_id": cid, "sensor": "coverage-sensor",
                          "metric": "ops_per_tick", "value": 5, "op": "<=",
                          "threshold": 8, "passed": True}],
                passed=True)],
            True, True)
    return out


def _matrix_contract():
    return {"id": "ctr-1", "task": "Implement the sampler correctly",
            "domain": "code",
            "acceptance": [
                {"class": "hard",
                 "cmd": "python3 -m unittest tests.test_smoke",
                 "expect": {"exit": 0}},
                {"class": "measured", "sensor": "coverage-sensor",
                 "sensor_cmd": "python3 sensor.py",
                 "metrics": [{"name": "ops_per_tick", "op": "<=",
                              "value": 8}]},
            ]}


def _run_matrix(store, cands, plans, **kw):
    peers = {}
    for c in cands:
        peers[c["expert_id"]] = _StubPeer(c["family"],
                                          plans.get(c["family"], {}))
    kw.setdefault("min_score", 7)
    return run_matrix(store, _matrix_contract(), cands,
                      _matrix_gate_results(cands),
                      {c["id"]: f"+impl {MATRIX_MARKERS[c['id']]}\n"
                       for c in cands},
                      peers, **kw)


class TestDissentSecondPassPerCandidate(unittest.TestCase):
    """Round-3 contest-core F4's own scenario: candidate A has a citing
    cell sitting ON the weighted median (found only by the second pass);
    candidate C earns a first-pass re-run request. Before the fix, C's
    request suppressed A's second pass entirely."""

    def test_other_candidates_rerun_request_does_not_suppress(self):
        store = MatrixStore()
        cands = _matrix_candidates(("cand-a", "cand-b", "cand-c"))
        plans = {fam: {cid: _uniform(8) for cid in MATRIX_MARKERS}
                 for fam in ("fam-a", "fam-b", "fam-c")}
        # A (cand-a): peer fam-b scores 2 CITING the ops_per_tick metric
        # (equal calibration weights -> the citing cell's own score is
        # the lower weighted median -> its distance is 0, invisible to
        # pass 1); peer fam-c scores 9 without citing.
        cite2 = {"score": 2, "confidence": 1.0,
                 "evidence": "ops_per_tick reading is off",
                 "rationale": "the metric contradicts"}
        plans["fam-b"]["cand-a"] = _cell(
            {row: dict(cite2) for row in MATRIX_ROWS})
        plans["fam-c"]["cand-a"] = _cell(
            {row: {"score": 9, "confidence": 1.0,
                   "evidence": "src/impl.py:9", "rationale": "looks right"}
             for row in MATRIX_ROWS})
        # C (cand-c): peer fam-a dissents in pass 1 citing the metric.
        cand_c = _cell({
            row: {"score": 2, "confidence": 1.0,
                  "evidence": "ops_per_tick misread here",
                  "rationale": "sensor disagrees"}
            for row in MATRIX_ROWS})
        for row in ("completeness", "evidence", "test_agreement"):
            cand_c["items"][row] = {"score": 8, "confidence": 1.0,
                                    "evidence": "src/impl.py:3",
                                    "rationale": "fine"}
        plans["fam-a"]["cand-c"] = cand_c
        plans["fam-b"]["cand-c"] = _uniform(8)
        outcome = _run_matrix(store, cands, plans)
        self.assertEqual(outcome.rerun_requests.get("cand-c"), "measured")
        self.assertEqual(outcome.rerun_requests.get("cand-a"), "measured")


class TestCalibrationSupersedeAndDecidability(unittest.TestCase):
    """Round-3 contest-core F6 (supersede) and F2 (decidability)."""

    def _experts(self):
        return {"expert-a": {"family": "fam-a"},
                "expert-b": {"family": "fam-b"},
                "expert-c": {"family": "fam-c"}}

    def test_rerun_rows_supersede_within_the_round(self):
        cands = _matrix_candidates()
        verdicts = {"ctr-1": {
            "contract_id": "ctr-1", "winner": "cand-a",
            "eliminated": ["cand-b", "cand-c"], "escalated": False,
            "judged": {"cand-a": {"pass": True},
                       "cand-b": {"pass": False},
                       "cand-c": {"pass": False}}}}
        store = MatrixStore(candidates=cands, verdicts=verdicts)
        plans8 = {fam: {cid: _uniform(8) for cid in MATRIX_MARKERS}
                  for fam in ("fam-a", "fam-b", "fam-c")}
        _run_matrix(store, cands, plans8, round_no=1)
        rows_after_pass1 = len(store.scores)
        self.assertGreater(rows_after_pass1, 0)
        # The dissent re-run of round 1: fam-b now scores cand-a at 2
        # (disagreeing with the positive outcome).
        plans_rerun = {fam: dict(plan) for fam, plan in plans8.items()}
        plans_rerun["fam-b"] = dict(plans8["fam-b"])
        plans_rerun["fam-b"]["cand-a"] = _uniform(2)
        _run_matrix(store, cands, plans_rerun, round_no=1)
        self.assertEqual(len(store.scores), 2 * rows_after_pass1)
        result = compute_calibration(store, self._experts())
        # fam-b scores only cand-a and cand-c (never its own family's
        # cand-b). Round 1 latest rows: cand-a at 2 (vs positive:
        # disagree), cand-c at 8 (vs negative: disagree) -> n=2,
        # agree=0. Had the superseded pass-1 rows also counted, cand-a
        # would contribute a second, agreeing cell and n would be 3.
        self.assertEqual(result["fam-b"]["n"], 2)
        self.assertAlmostEqual(result["fam-b"]["calibration"], 1 / 4)
        # A genuinely different round still counts separately.
        _run_matrix(store, cands, plans8, round_no=2)
        result = compute_calibration(store, self._experts())
        self.assertEqual(result["fam-b"]["n"], 4)

    def test_bracket_cut_passer_is_not_a_negative(self):
        # Finding F2 case (a): fam-j scores both the winner and a
        # bracket-cut passer at 9. The passer (judged pass, listed in
        # eliminated) must be EXCLUDED — only the winner's cell counts.
        cands = _matrix_candidates(("cand-a", "cand-b"))
        for cand in cands:
            cand["family"] = "fam-x" if cand["id"] == "cand-a" else "fam-y"
        verdicts = {"ctr-1": {
            "contract_id": "ctr-1", "winner": "cand-a",
            "eliminated": ["cand-b"], "escalated": False,
            "judged": {"cand-a": {"pass": True},
                       "cand-b": {"pass": True}}}}
        store = MatrixStore(candidates=cands, verdicts=verdicts)
        store.add_score("cand-a", "matrix", "validity_matrix_v1",
                        "judge-j", "fam-j", True, 1, {}, 9.0)
        store.add_score("cand-b", "matrix", "validity_matrix_v1",
                        "judge-j", "fam-j", True, 1, {}, 9.0)
        result = compute_calibration(
            store, {"judge-j": {"family": "fam-j"}})
        self.assertEqual(result["fam-j"]["n"], 1)
        self.assertAlmostEqual(result["fam-j"]["calibration"], 2 / 3)

    def test_escalated_verdict_grades_nobody(self):
        # Finding F2 case (b): an escalated verdict (winner None) marks
        # no candidate negative — a judge who scored high is not wrong
        # on every cell by construction.
        cands = _matrix_candidates(("cand-a", "cand-b"))
        verdicts = {"ctr-1": {
            "contract_id": "ctr-1", "winner": None,
            "eliminated": ["cand-a", "cand-b"], "escalated": True,
            "judged": {"cand-a": {"pass": False},
                       "cand-b": {"pass": False}}}}
        store = MatrixStore(candidates=cands, verdicts=verdicts)
        store.add_score("cand-a", "matrix", "validity_matrix_v1",
                        "judge-j", "fam-j", True, 1, {}, 9.0)
        store.add_score("cand-b", "matrix", "validity_matrix_v1",
                        "judge-j", "fam-j", True, 1, {}, 9.0)
        result = compute_calibration(
            store, {"judge-j": {"family": "fam-j"}})
        self.assertEqual(result["fam-j"]["n"], 0)
        self.assertAlmostEqual(result["fam-j"]["calibration"], 0.5)

    def test_gate_eliminated_candidate_is_a_negative(self):
        cands = _matrix_candidates(("cand-a", "cand-b"))
        cands[1]["eliminated_at"] = "hard"
        verdicts = {"ctr-1": {
            "contract_id": "ctr-1", "winner": "cand-a",
            "eliminated": ["cand-b"], "escalated": False,
            "judged": {"cand-a": {"pass": True}}}}
        store = MatrixStore(candidates=cands, verdicts=verdicts)
        store.add_score("cand-b", "matrix", "validity_matrix_v1",
                        "judge-j", "fam-j", True, 1, {}, 9.0)
        result = compute_calibration(
            store, {"judge-j": {"family": "fam-j"}})
        # Scored 9 against a hard-gate failure: a disagreement, n=1.
        self.assertEqual(result["fam-j"]["n"], 1)
        self.assertAlmostEqual(result["fam-j"]["calibration"], 1 / 3)


if __name__ == "__main__":
    unittest.main()
