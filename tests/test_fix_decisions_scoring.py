"""Owner-decisions fix pass, Unit 2 (scoring rules) — Item 2 = B/B.

Dino's rulings (2026-11-09, FIX-SPEC-DECISIONS-2026-11-09.md):

* D2a — a measured-only regression is NOT a valid contract. A
  candidate that passes the hard gates but regresses on the measured
  gate FAILS: the run's failure machinery (retries, then escalation)
  decides it, and no waiver is offered or carried for it. The waiver
  mechanism at the human gate survives for verdicts that carry an
  offered waiver (pinned by the round-4/5/7 gate fixtures, whose
  reaches were re-based onto verdict-row construction when this
  ruling landed); what ended is the RUN producing the offer.
* D2b — bracket-eliminated passers are NOT calibration negatives.
  In matrix.compute_calibration a candidate that passed its gates
  but was eliminated by the bracket (cut/step-down) is not a
  negative example for judge scoring; only genuine gate failures
  count. The exclusion is absolute: it vetoes even a negative dealt
  by the verdict's judged block, which the decidability pass reads
  without consulting the elimination kind.

The D2b fixture grades a bracket-cut passer and a gate-failed
candidate side by side on a real Store, in the loop's persisted
shapes, and re-pins the round-6/7 fam-x seam numbers for the TRUE
negative (n = 2, calibration = 0.5): they must not move.
"""

import tempfile
import unittest
from pathlib import Path

from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.contracts import validate_contract
from hybrid_critic_loop.matrix import _decidable_outcomes, compute_calibration
from hybrid_critic_loop.state import Store

from tests.test_fix_round4_loop import RealGitBase


def _contract(cid, ops=None, hard_cmd="test -f solution.txt"):
    """The round-4 contract shape with a choosable measured outcome:
    ops <= 8 passes the measured gate; anything above regresses."""
    acceptance = [{"class": "hard", "cmd": hard_cmd, "expect": {"exit": 0}}]
    if ops is not None:
        acceptance.append({
            "class": "measured",
            "metrics": [{"name": "ops", "op": "<=", "value": 8}],
            "sensor": "ops_bench",
            "sensor_cmd": (
                "python3 -c \"import json; "
                f"print(json.dumps({{'ops': {ops}}}))\""
            ),
        })
    return validate_contract({
        "id": cid, "stage": "implement", "task": "Write solution.txt.",
        "domain": "code.game", "tags": ["fix-decisions"],
        "acceptance": acceptance,
    })


class TestMeasuredRegressionFailsTheContract(RealGitBase):
    """D2a: the run-level pins."""

    def test_measured_regression_ends_failed_with_no_waiver(self):
        # Every candidate passes hard, every candidate regresses on
        # the measured gate (ops 99 against <= 8).
        result = loop_mod.run_contract(
            self.store, _contract("ctr_reg", ops=99), self.repo,
            self._executors(), human_decider=None)
        self.assertEqual(result["status"], "escalated", result)
        verdict = self.store.latest_verdict("ctr_reg")
        self.assertTrue(verdict["escalated"], verdict)
        self.assertIsNone(verdict["winner"], verdict)
        # No waiver is offered or carried for a measured regression.
        self.assertIsNone(verdict["waiver"], verdict)
        self.assertEqual(verdict["measured"], "fail")
        self.assertEqual(verdict["human_gate"], "not_reached")
        self.assertEqual(
            self.store.get_contract("ctr_reg")["status"], "escalated")
        # And there is nothing at the gate to waive afterwards.
        waived = loop_mod.record_human_gate(
            self.store, "ctr_reg", "waived", "accept the regression")
        self.assertEqual(waived["status"], "error", waived)
        self.assertIn("no pending human gate", waived["detail"])

    def test_measured_pass_still_completes_normally(self):
        result = loop_mod.run_contract(
            self.store, _contract("ctr_pass", ops=3), self.repo,
            self._executors(), human_decider=None)
        self.assertEqual(result["status"], "awaiting_human_gate", result)
        verdict = self.store.latest_verdict("ctr_pass")
        self.assertEqual(verdict["measured"], "pass")
        self.assertIsNone(verdict["waiver"], verdict)
        done = loop_mod.record_human_gate(self.store, "ctr_pass", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertIsNone(done["waiver"])
        self.assertEqual(
            self.store.get_contract("ctr_pass")["status"], "completed")

    def test_hard_gate_failure_behaves_as_before(self):
        # Hard failures were never waivable and still are not: the
        # field fails, retries, escalates — no waiver anywhere.
        result = loop_mod.run_contract(
            self.store, _contract("ctr_hard", hard_cmd="test -f nope.txt"),
            self.repo, self._executors(), human_decider=None)
        self.assertEqual(result["status"], "escalated", result)
        verdict = self.store.latest_verdict("ctr_hard")
        self.assertTrue(verdict["escalated"], verdict)
        self.assertEqual(verdict["hard"], "fail")
        self.assertIsNone(verdict["waiver"], verdict)
        self.assertEqual(verdict["human_gate"], "not_reached")


JUDGE = "judge-1"
FAMILY = "fam-x"


def _mark_eliminated_like_the_loop(store, candidate_id, kind):
    """The loop's exact write shape (loop._mark_eliminated): the kind
    rides inside the candidate's evidence, written via
    update_candidate — the round-6 seam's persisted shape."""
    row = store.get_candidate(candidate_id) or {}
    evidence = dict(row.get("evidence") or {})
    evidence["eliminated_kind"] = kind
    store.update_candidate(candidate_id, evidence=evidence)


class TestBracketCutPassersAreNotCalibrationNegatives(unittest.TestCase):
    """D2b: a bracket-cut passer beside a gate-failed candidate on a
    real Store — only the failure moves the judge's grade."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(str(Path(self._tmp.name) / "data.db"))
        self.addCleanup(self.store.close)
        self.store.upsert_expert(JUDGE, FAMILY, access="api",
                                 cost_tier="api")
        self.contract = self.store.create_contract(
            target_id="tgt", stage="implement", task="do the thing",
            domain="code", tags=[], acceptance=["it works"],
            contract_id="ctr-d2b")
        # Slots: 0 winner, 1 gate_error (a TRUE negative), 2 a
        # bracket-cut passer whose judged-block entry reads as a
        # failure, 3 a bracket-cut passer with a passing judged entry.
        self.ids = {}
        for slot, family in ((0, "fam-a"), (1, "fam-b"), (2, "fam-c"),
                             (3, "fam-d")):
            self.ids[slot] = self.store.create_candidate(
                self.contract, attempt=1, stage="implement",
                expert_id=f"expert-{slot}", family=family, slot=slot,
                worktree=f"/tmp/wt-{slot}", baseline_hash="base")
        _mark_eliminated_like_the_loop(self.store, self.ids[1], "gate_error")
        _mark_eliminated_like_the_loop(self.store, self.ids[2], "bracket_cut")
        _mark_eliminated_like_the_loop(self.store, self.ids[3], "bracket_cut")
        self.store.create_verdict(
            contract_id=self.contract, attempt=1, winner=self.ids[0],
            eliminated=[self.ids[1], self.ids[2], self.ids[3]],
            hard="pass", measured="pass",
            judged={self.ids[2]: {"pass": False},
                    self.ids[3]: {"pass": True}},
            escalated=False, escalation_reason=None, waiver=None,
            human_gate="pending", tie_break=None)
        # The judge scored every candidate 9.0: agreement on the
        # winner, disagreement on the gate-killed negative — and two
        # cut passers whose cells must NOT be graded at all.
        for slot in (0, 1, 2, 3):
            self.store.add_score(
                self.ids[slot], "matrix", "validity_matrix_v1", JUDGE,
                FAMILY, True, 1, {}, 9.0)
        self.experts = {JUDGE: {"family": FAMILY}}

    def test_decidable_outcomes_exclude_both_cut_passers(self):
        by_id = {c["id"]: c
                 for c in self.store.candidates_for(self.contract)}
        verdicts = {self.contract: self.store.latest_verdict(self.contract)}
        outcomes = _decidable_outcomes(by_id, verdicts)
        self.assertIs(outcomes[self.ids[0]], True)       # winner
        self.assertIs(outcomes[self.ids[1]], False)      # gate_error
        # The judged block reads slot 2 as a failure, yet its
        # bracket_cut kind vetoes the negative (Item 2(b) = B).
        self.assertNotIn(self.ids[2], outcomes)
        self.assertNotIn(self.ids[3], outcomes)

    def test_only_the_gate_failure_moves_the_grade(self):
        result = compute_calibration(self.store, self.experts)
        # Exactly two graded cells — winner (agree) + gate_error
        # (disagree) — the round-6/7 fam-x seam numbers for a true
        # negative, unmoved by the cut passers beside it.
        self.assertEqual(result[FAMILY]["n"], 2)
        # (1 agreement + 2*0.5 prior) / (2 + 2).
        self.assertAlmostEqual(result[FAMILY]["calibration"], 0.5)


class TestBracketCutVetoDictLevel(unittest.TestCase):
    """D2b resolver controls over _decidable_outcomes' documented
    dict input: the veto holds for every judged-map shape that can
    carry a failure and for both kind positions."""

    def _outcomes(self, candidate, judged_entry):
        by_id = {"win": {"id": "win", "contract_id": "ctr"},
                 candidate["id"]: candidate}
        verdicts = {"ctr": {"winner": "win", "escalated": False,
                            "judged": {candidate["id"]: judged_entry}}}
        return _decidable_outcomes(by_id, verdicts)

    def test_top_level_kind_vetoes_a_bare_false_entry(self):
        cand = {"id": "cut", "contract_id": "ctr",
                "eliminated_kind": "bracket_cut"}
        outcomes = self._outcomes(cand, False)
        self.assertNotIn("cut", outcomes)
        self.assertIs(outcomes["win"], True)

    def test_evidence_kind_vetoes_a_nested_per_candidate_failure(self):
        cand = {"id": "cut", "contract_id": "ctr",
                "evidence": {"eliminated_kind": "bracket_cut"}}
        outcomes = self._outcomes(
            cand, {"per_candidate": {"cut": {"pass": False}}})
        # The nested entry is not a top-level judged negative in the
        # first place; the candidate stays undecidable either way.
        self.assertNotIn("cut", outcomes)

    def test_a_judged_kind_keeps_its_negative(self):
        # Control: the veto is bracket_cut's alone — a genuine judged
        # failure (kind "judged") still grades as a negative.
        cand = {"id": "jf", "contract_id": "ctr",
                "evidence": {"eliminated_kind": "judged"}}
        outcomes = self._outcomes(cand, {"pass": False})
        self.assertIs(outcomes["jf"], False)


if __name__ == "__main__":
    unittest.main()
