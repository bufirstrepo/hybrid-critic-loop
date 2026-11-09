"""Round-6 fix pass, Unit 2 (matrix) — spec J2 (round-6 gate-core F2).

The seam this file pins: the loop persists a candidate's elimination
kind INSIDE its evidence (``loop._mark_eliminated`` writes
``evidence["eliminated_kind"]`` via ``update_candidate``), and the
real Store's candidate dict carries ``evidence`` as a nested dict
with NO top-level ``eliminated_kind``. Round 5's calibration read
site looked only at the top level, so on the production wiring a
gate_error candidate was invisible to calibration instead of grading
as a negative — the two halves were each tested against different
shapes and never met.

The seam test below persists rows through the REAL write shape —
candidates created on a real Store, kinds set via ``update_candidate``
with the loop's evidence payload, scores via ``add_score``, the
verdict via ``create_verdict`` — and grades with
``compute_calibration`` against that store. No fake-store dicts.
The dict-level resolver controls (evidence as a JSON string,
top-level precedence, the pre-fix ``eliminated_at`` shape) live in
the second class: they exercise ``_decidable_outcomes``' documented
input, not a store stand-in.
"""

import json
import tempfile
import unittest
from pathlib import Path

from hybrid_critic_loop.matrix import (
    _candidate_eliminated_kind, _decidable_outcomes, compute_calibration)
from hybrid_critic_loop.state import Store

JUDGE = "judge-1"
FAMILY = "fam-x"


def _mark_eliminated_like_the_loop(store, candidate_id, kind):
    """The loop's exact write shape (loop._mark_eliminated): read the
    candidate's current evidence, set the kind inside it, write the
    evidence back with update_candidate."""
    row = store.get_candidate(candidate_id) or {}
    evidence = dict(row.get("evidence") or {})
    evidence["eliminated_kind"] = kind
    store.update_candidate(candidate_id, evidence=evidence)


class TestCalibrationSeamRealStore(unittest.TestCase):
    """J2 seam: real Store enumeration -> _decidable_outcomes ->
    compute_calibration, all over the loop's persisted shape."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(str(Path(self._tmp.name) / "data.db"))
        self.addCleanup(self.store.close)
        # compute_calibration writes back per expert on a real Store,
        # so the judge must be a registered expert row.
        self.store.upsert_expert(JUDGE, FAMILY, access="api",
                                 cost_tier="api")
        self.contract = self.store.create_contract(
            target_id="tgt", stage="implement", task="do the thing",
            domain="code", tags=[], acceptance=["it works"],
            contract_id="ctr-seam")
        # Slots: 0 winner, 1 gate_error, 2 bracket_cut, 3 control
        # (no kind anywhere — the pre-fix persisted shape).
        self.ids = {}
        for slot, family in ((0, "fam-a"), (1, "fam-b"), (2, "fam-c"),
                             (3, "fam-d")):
            self.ids[slot] = self.store.create_candidate(
                self.contract, attempt=1, stage="implement",
                expert_id=f"expert-{slot}", family=family, slot=slot,
                worktree=f"/tmp/wt-{slot}", baseline_hash="base")
        _mark_eliminated_like_the_loop(self.store, self.ids[1], "gate_error")
        _mark_eliminated_like_the_loop(self.store, self.ids[2], "bracket_cut")
        self.store.create_verdict(
            contract_id=self.contract, attempt=1, winner=self.ids[0],
            eliminated=[self.ids[1], self.ids[2], self.ids[3]],
            hard="pass", measured="pass", judged={}, escalated=False,
            escalation_reason=None, waiver=None, human_gate="pending",
            tie_break=None)
        # The judge scored every candidate 9.0 in the matrix gate:
        # agreement on the winner, disagreement on a gate-killed
        # negative, and two cells that must NOT be graded at all.
        for slot in (0, 1, 2, 3):
            self.store.add_score(
                self.ids[slot], "matrix", "validity_matrix_v1", JUDGE,
                FAMILY, True, 1, {}, 9.0)
        self.experts = {JUDGE: {"family": FAMILY}}

    def test_persisted_shape_is_evidence_only(self):
        # The write shape itself: no top-level kind on a real row,
        # the kind rides in the evidence dict.
        row = self.store.get_candidate(self.ids[1])
        self.assertNotIn("eliminated_kind", row)
        self.assertEqual(row["evidence"], {"eliminated_kind": "gate_error"})

    def test_decidable_outcomes_over_real_enumeration(self):
        by_id = {c["id"]: c
                 for c in self.store.candidates_for(self.contract)}
        verdicts = {self.contract: self.store.latest_verdict(self.contract)}
        outcomes = _decidable_outcomes(by_id, verdicts)
        self.assertIs(outcomes[self.ids[0]], True)          # winner
        self.assertIs(outcomes[self.ids[1]], False)         # gate_error
        self.assertNotIn(self.ids[2], outcomes)             # bracket_cut
        self.assertNotIn(self.ids[3], outcomes)             # no kind

    def test_gate_error_grades_as_negative_bracket_cut_excluded(self):
        result = compute_calibration(self.store, self.experts)
        # Exactly two graded cells: winner (agree) + gate_error
        # (disagree). The bracket_cut and kind-less cells vanish.
        self.assertEqual(result[FAMILY]["n"], 2)
        # (1 agreement + 2*0.5 prior) / (2 + 2).
        self.assertAlmostEqual(result[FAMILY]["calibration"], 0.5)

    def test_escalated_verdict_excludes_evidence_kind_whole(self):
        contract2 = self.store.create_contract(
            target_id="tgt", stage="implement", task="another",
            domain="code", tags=[], acceptance=["x"],
            contract_id="ctr-seam-esc")
        cid = self.store.create_candidate(
            contract2, attempt=1, stage="implement", expert_id="e9",
            family="fam-z", slot=0, worktree="/tmp/wt-9",
            baseline_hash="base")
        _mark_eliminated_like_the_loop(self.store, cid, "gate_error")
        self.store.create_verdict(
            contract_id=contract2, attempt=1, winner=None, eliminated=[],
            hard="fail", measured=None, judged={}, escalated=True,
            escalation_reason="no survivor", waiver=None,
            human_gate="pending", tie_break=None)
        self.store.add_score(cid, "matrix", "validity_matrix_v1", JUDGE,
                             FAMILY, True, 1, {}, 9.0)
        result = compute_calibration(self.store, self.experts)
        # Still just the first contract's two cells.
        self.assertEqual(result[FAMILY]["n"], 2)


class TestEliminatedKindResolution(unittest.TestCase):
    """J2 resolver controls: the shapes _decidable_outcomes accepts."""

    def _outcomes(self, candidates, verdicts=None):
        by_id = {c["id"]: c for c in candidates}
        return _decidable_outcomes(by_id, verdicts or {})

    def test_evidence_as_json_string_resolves(self):
        row = {"id": "c1", "contract_id": "k",
               "evidence": json.dumps({"eliminated_kind": "gate_error"})}
        self.assertEqual(_candidate_eliminated_kind(row), "gate_error")
        self.assertIs(self._outcomes([row])["c1"], False)

    def test_top_level_kind_wins_over_evidence(self):
        # Hand-built rows (the round-5 unit-test shape) keep working,
        # and the top level is authoritative when both are present.
        cut = {"id": "c-cut", "contract_id": "k",
               "eliminated_kind": "bracket_cut",
               "evidence": {"eliminated_kind": "gate_error"}}
        self.assertNotIn("c-cut", self._outcomes([cut]))
        killed = {"id": "c-kill", "contract_id": "k",
                  "eliminated_kind": "gate_error",
                  "evidence": {"eliminated_kind": "bracket_cut"}}
        self.assertIs(self._outcomes([killed])["c-kill"], False)

    def test_pre_fix_eliminated_at_shape_behaves_as_before(self):
        # Control: no kind in either place — the round-4
        # eliminated_at leg decides exactly as it did pre-fix.
        hard = {"id": "c-old-hard", "contract_id": "k",
                "eliminated_at": "hard", "evidence": {}}
        gerr = {"id": "c-old-gerr", "contract_id": "k",
                "eliminated_at": "gate_error", "evidence": {}}
        outcomes = self._outcomes([hard, gerr])
        self.assertIs(outcomes["c-old-hard"], False)
        # "gate_error" was never in the eliminated_at set.
        self.assertNotIn("c-old-gerr", outcomes)

    def test_unreadable_evidence_falls_back_without_crashing(self):
        broken = {"id": "c-broken", "contract_id": "k",
                  "evidence": "{not json", "eliminated_at": "measured"}
        scalar = {"id": "c-scalar", "contract_id": "k",
                  "evidence": json.dumps(7)}
        outcomes = self._outcomes([broken, scalar])
        self.assertIsNone(_candidate_eliminated_kind(broken))
        self.assertIs(outcomes["c-broken"], False)  # via eliminated_at
        self.assertNotIn("c-scalar", outcomes)


if __name__ == "__main__":
    unittest.main()
