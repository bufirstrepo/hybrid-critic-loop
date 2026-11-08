"""Tests for hybrid_critic_loop.matrix (Workstream W2).

Stub peer executors (objects with .family and .judge(packet, rubric))
identify candidates inside the blinded packet by a unique marker token
planted in each candidate's diff, so the per-peer seeded label shuffle
cannot change the outcome. The store is an in-memory FakeStore in the
test_judges.py pattern, extended with the confidence/details kwargs
that the v0.3 add_score carries.
"""

import re
import tempfile
import unittest
from pathlib import Path

from hybrid_critic_loop.gates import CandidateGateResult, HardResult, MeasureResult
from hybrid_critic_loop.matrix import (
    VALIDITY_ROWS,
    VALIDITY_RUBRIC,
    build_matrix_cells,
    compute_calibration,
    run_matrix,
    validity_rubric,
    weighted_median,
)

ROWS = list(VALIDITY_ROWS)
MARKERS = {"cand-a": "TOKAQX", "cand-b": "TOKBQX", "cand-c": "TOKCQX",
           "cand-d": "TOKDQX"}


# ------------------------------------------------------------------ store

class FakeStore:
    """FakeStore pattern from test_judges.py + v0.3 score kwargs."""

    def __init__(self, candidates=None, verdicts=None):
        self.measurements = []
        self.scores = []
        self.candidates = list(candidates or [])
        self._verdicts = dict(verdicts or {})
        self.calibration_updates = []

    def add_measurement(self, candidate_id, sensor, metric, value, op,
                        threshold, passed):
        row = {"candidate_id": candidate_id, "sensor": sensor, "metric": metric,
               "value": value, "op": op, "threshold": threshold, "passed": passed}
        self.measurements.append(row)
        return row

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

    def candidates_for(self, contract_id):
        return [c for c in self.candidates
                if c.get("contract_id") == contract_id]

    def get_candidate(self, candidate_id):
        for cand in self.candidates:
            if cand["id"] == candidate_id:
                return cand
        return None

    def latest_verdict(self, contract_id):
        return self._verdicts.get(contract_id)

    def update_expert_calibration(self, expert_id, value, n):
        self.calibration_updates.append((expert_id, value, n))


# ------------------------------------------------------------------ peers

def cell_entry(scores, confidence=0.9, evidence="src/impl.py:42",
               rationale="addresses the contract item"):
    """Build one label entry: {items: {row: {score, confidence, ...}}}.

    ``scores`` maps row -> score, or row -> a full per-row dict.
    """
    items = {}
    for row, spec in scores.items():
        if isinstance(spec, dict):
            items[row] = dict(spec)
        else:
            items[row] = {"score": spec, "confidence": confidence,
                          "evidence": evidence, "rationale": rationale}
    return {"items": items}


def uniform(score, **kw):
    return {row: score for row in ROWS}


class StubPeer:
    """Executor stub: .family + .judge(packet, rubric).

    ``plan`` maps candidate_id -> row-score spec accepted by
    cell_entry, or candidate_id -> a ready-made entry dict with an
    "items" key (used for malformed cells).
    """

    def __init__(self, family, plan, **cell_kw):
        self.family = family
        self.plan = plan
        self.cell_kw = cell_kw
        self.packets = []

    def judge(self, packet, rubric):
        self.packets.append(packet)
        result = {}
        sections = re.split(r"=== CANDIDATE ([A-Z]+) ===", packet)
        # sections: [prefix, label, body, label, body, ...]
        for i in range(1, len(sections) - 1, 2):
            label, body = sections[i], sections[i + 1]
            for cid, marker in MARKERS.items():
                if marker not in body or cid not in self.plan:
                    continue
                spec = self.plan[cid]
                if isinstance(spec, dict) and "items" in spec:
                    result[label] = spec
                else:
                    result[label] = cell_entry(spec, **self.cell_kw)
        return result


# ------------------------------------------------------------------ fixtures

def contract():
    return {
        "id": "ctr-1",
        "task": "Implement the sampler correctly",
        "domain": "code",
        "acceptance": [
            {"class": "hard", "cmd": "python3 -m unittest tests.test_smoke",
             "expect": {"exit": 0}},
            {"class": "measured", "sensor": "coverage-sensor",
             "sensor_cmd": "python3 sensor.py",
             "metrics": [{"name": "coverage", "op": ">=", "value": 80}]},
            {"class": "judged", "rubric": "validity_matrix_v1", "min": 7},
        ],
    }


def cand(cid, family, expert):
    return {"id": cid, "family": family, "expert_id": expert,
            "contract_id": "ctr-1", "provenance": "generated",
            "evidence": {"log_hash": f"sha256:{cid}"}}


def candidates3():
    return [cand("cand-a", "fam-a", "expert-a"),
            cand("cand-b", "fam-b", "expert-b"),
            cand("cand-c", "fam-c", "expert-c")]


def candidates4():
    return candidates3() + [cand("cand-d", "fam-d", "expert-d")]


def diffs_for(cands):
    return {c["id"]: f"+implementation {MARKERS[c['id']]}\n+more code"
            for c in cands}


def gate_results_for(cands):
    out = {}
    for c in cands:
        cid = c["id"]
        hard = [HardResult(cmd="python3 -m unittest tests.test_smoke",
                           expected_exit=0, exit_code=0, passed=True)]
        measured = [MeasureResult(
            sensor="coverage-sensor",
            metrics=[{"candidate_id": cid, "sensor": "coverage-sensor",
                      "metric": "coverage", "value": 90, "op": ">=",
                      "threshold": 80, "passed": True}],
            passed=True)]
        out[cid] = CandidateGateResult(cid, True, None, hard, measured,
                                       True, True)
    return out


def peers_for(cands, plans, **cell_kw):
    """One peer executor per candidate family, in candidate order."""
    peers = {}
    for c in cands:
        fam = c["family"]
        peers[c["expert_id"]] = StubPeer(
            fam, plans.get(fam, {}), **cell_kw)
    return peers


def all_uniform_plans(score=8, **kw):
    plans = {}
    for fam in ("fam-a", "fam-b", "fam-c", "fam-d"):
        plans[fam] = {cid: uniform(score) for cid in MARKERS}
    return plans


# ------------------------------------------------------------------ rubric / cells

class TestRubricAndCells(unittest.TestCase):
    def test_validity_rubric_shape_and_copy(self):
        rubric = validity_rubric()
        self.assertEqual(rubric["id"], "validity_matrix_v1")
        self.assertEqual(VALIDITY_RUBRIC["id"], "validity_matrix_v1")
        self.assertEqual(rubric["version"], 1)
        self.assertEqual(rubric["scale"], 10)
        self.assertEqual(rubric["items"], ["question_fidelity", "completeness",
                                           "evidence", "test_agreement"])
        rubric["items"].append("mutant")
        self.assertEqual(len(validity_rubric()["items"]), 4)

    def test_build_matrix_cells_strips_self_cells(self):
        cands = candidates3()
        cells = build_matrix_cells(["fam-a", "fam-b", "fam-c"], cands)
        self.assertEqual(len(cells), 3 * (3 - 1))
        pairs = {(c["judge_family_slot"], c["candidate_id"]) for c in cells}
        self.assertEqual(pairs, {
            ("fam-a", "cand-b"), ("fam-a", "cand-c"),
            ("fam-b", "cand-a"), ("fam-b", "cand-c"),
            ("fam-c", "cand-a"), ("fam-c", "cand-b"),
        })
        family_of = {c["id"]: c["family"] for c in cands}
        for cell in cells:
            self.assertNotEqual(cell["judge_family_slot"],
                                family_of[cell["candidate_id"]])

    def test_build_matrix_cells_count_formula(self):
        cands = candidates4()
        cells = build_matrix_cells(
            ["fam-a", "fam-b", "fam-c", "fam-d"], cands)
        self.assertEqual(len(cells), 4 * (4 - 1))

    def test_weighted_median_lower(self):
        # sorted [1(w1), 2(w1), 10(w1)]; half = 1.5 -> first cum >= 1.5 is 2
        self.assertEqual(weighted_median([(10, 1), (1, 1), (2, 1)]), 2.0)
        # heavy weight on the low value pulls the median down to it
        self.assertEqual(weighted_median([(4, 0.9), (8, 0.1)]), 4.0)
        self.assertEqual(weighted_median({4: 0.9, 8: 0.1}), 4.0)
        self.assertEqual(weighted_median([4, 8], [0.9, 0.1]), 4.0)
        self.assertIsNone(weighted_median([]))
        self.assertIsNone(weighted_median([(5, 0.0)]))


# ------------------------------------------------------------------ run matrix

class TestRunMatrix(unittest.TestCase):
    def _run(self, store, cands, peers, **kw):
        kw.setdefault("min_score", 7)
        return run_matrix(store, contract(), cands, gate_results_for(cands),
                          diffs_for(cands), peers, **kw)

    def test_full_run_aggregation_weighted_by_calibration(self):
        store = FakeStore()
        cands = candidates3()
        plans = all_uniform_plans(8)
        # cand-a: peer-b scores 4, peer-c scores 8 (all rows, conf 1.0)
        plans["fam-b"]["cand-a"] = uniform(4)
        plans["fam-c"]["cand-a"] = uniform(8)
        peers = peers_for(cands, plans, confidence=1.0)
        calibrations = {
            "fam-a": {"calibration": 0.5, "n": 10, "active": True},
            "fam-b": {"calibration": 0.9, "n": 10, "active": True},
            "fam-c": {"calibration": 0.1, "n": 10, "active": True},
        }
        outcome = self._run(store, cands, peers, min_score=4,
                            spread_threshold=99.0,
                            calibrations=calibrations)
        self.assertFalse(outcome.escalated)
        self.assertEqual(outcome.panel_status, "full")
        self.assertEqual(sorted(outcome.peers_used),
                         ["expert-a", "expert-b", "expert-c"])
        entry = outcome.per_candidate["cand-a"]
        # Hand-computed: row weights are 0.9*1.0 (score 4) and 0.1*1.0
        # (score 8); sorted cumulative reaches half (0.5) at score 4.
        # The unweighted median of {4, 8} would be 6 -- calibration
        # weighting is what produces 4.0 here.
        for row in ROWS:
            self.assertEqual(entry["rows"][row], 4.0)
        self.assertEqual(entry["median_mean"], 4.0)
        self.assertEqual(entry["min_row"], 4.0)
        self.assertTrue(entry["pass"])
        self.assertEqual(entry["panel_status"], "full")
        self.assertEqual(sorted(entry["peers"]), ["fam-b", "fam-c"])
        for cid in ("cand-b", "cand-c"):
            self.assertEqual(outcome.per_candidate[cid]["median_mean"], 8.0)
        # Persistence: 3 candidates x 2 peers = 6 matrix cells stored.
        self.assertEqual(len(store.scores), 6)
        for row in store.scores:
            self.assertEqual(row["gate"], "matrix")
            self.assertEqual(row["rubric"], "validity_matrix_v1")
            self.assertTrue(row["blind"])
            self.assertEqual(row["confidence"], 1.0)
            self.assertEqual(set(row["items"]), set(ROWS))
            for detail in row["details"].values():
                self.assertTrue(detail["evidence"].strip())
                self.assertTrue(detail["rationale"].strip())

    def test_packet_blinding_and_contents(self):
        store = FakeStore()
        cands = candidates3()
        peers = peers_for(cands, all_uniform_plans(8))
        self._run(store, cands, peers)
        packet_a = peers["expert-a"].packets[0]
        # Peer's own candidate is stripped from its packet (self-cell).
        self.assertNotIn(MARKERS["cand-a"], packet_a)
        self.assertIn(MARKERS["cand-b"], packet_a)
        self.assertIn(MARKERS["cand-c"], packet_a)
        # Original contract text + acceptance + measured results shown.
        self.assertIn("Implement the sampler correctly", packet_a)
        self.assertIn("python3 -m unittest tests.test_smoke", packet_a)
        self.assertIn("coverage=90", packet_a)
        self.assertIn("question_fidelity", packet_a)
        # No identities.
        for term in ("expert-a", "expert-b", "fam-a", "fam-b", "fam-c"):
            self.assertNotIn(term, packet_a)

    def test_peer_below_five_cells_uses_prior(self):
        store = FakeStore()
        cands = candidates3()
        plans = all_uniform_plans(8)
        plans["fam-b"]["cand-a"] = uniform(8)
        plans["fam-c"]["cand-a"] = uniform(4)
        # Per-row confidence differs per peer via ready-made entries:
        plans["fam-b"]["cand-a"] = cell_entry(uniform(8), confidence=0.6)
        plans["fam-c"]["cand-a"] = cell_entry(uniform(4), confidence=1.0)
        peers = peers_for(cands, plans)
        calibrations = {
            # Stored 0.95 but only n=3 cells -> effective weight is prior 0.5.
            "fam-b": {"calibration": 0.95, "n": 3, "active": False},
            "fam-c": {"calibration": 0.4, "n": 10, "active": True},
        }
        outcome = self._run(store, cands, peers, spread_threshold=99.0,
                            calibrations=calibrations)
        self.assertEqual(outcome.effective_calibrations["fam-b"]["calibration"],
                         0.5)
        # Weights: fam-b 0.5*0.6=0.30 (score 8), fam-c 0.4*1.0=0.40
        # (score 4). Total 0.70, half 0.35; sorted cum reaches 0.40 at
        # score 4 -> median 4. Had the stored 0.95 applied, fam-b's
        # weight would be 0.57 and the median would be 8.
        entry = outcome.per_candidate["cand-a"]
        self.assertEqual(entry["rows"]["question_fidelity"], 4.0)
        self.assertEqual(entry["median_mean"], 4.0)

    def test_pass_rule_boundary(self):
        store = FakeStore()
        cands = candidates3()
        plans = all_uniform_plans(9)
        at_boundary = {"question_fidelity": 5, "completeness": 9,
                       "evidence": 9, "test_agreement": 9}
        below = {"question_fidelity": 4.9, "completeness": 9,
                 "evidence": 9, "test_agreement": 9}
        for fam in ("fam-b", "fam-c"):
            plans[fam]["cand-a"] = dict(at_boundary)
        for fam in ("fam-a", "fam-c"):
            plans[fam]["cand-b"] = dict(below)
        peers = peers_for(cands, plans, confidence=1.0)
        outcome = self._run(store, cands, peers, min_score=7,
                            spread_threshold=99.0)
        a = outcome.per_candidate["cand-a"]
        self.assertEqual(a["min_row"], 5.0)          # exactly min - 2
        self.assertEqual(a["median_mean"], 8.0)
        self.assertTrue(a["pass"])
        b = outcome.per_candidate["cand-b"]
        self.assertAlmostEqual(b["min_row"], 4.9)    # min - 2.1
        self.assertAlmostEqual(b["median_mean"], 7.975)
        self.assertFalse(b["pass"])

    def test_dissent_flags_and_gate_citations_rerun(self):
        store = FakeStore()
        cands = candidates4()
        plans = all_uniform_plans(8)
        # cand-a: peer-d dissents on question_fidelity, citing coverage.
        plans["fam-d"]["cand-a"] = cell_entry(
            {"question_fidelity": 2, "completeness": 8, "evidence": 8,
             "test_agreement": 8}, confidence=1.0)
        plans["fam-d"]["cand-a"]["items"]["question_fidelity"] = {
            "score": 2, "confidence": 1.0,
            "evidence": "coverage reading at sensor.py:12",
            "rationale": "the coverage metric contradicts the majority"}
        # cand-b: peer-c dissents on completeness, citing the hard cmd.
        plans["fam-c"]["cand-b"] = cell_entry(
            {"question_fidelity": 8, "completeness": 2, "evidence": 8,
             "test_agreement": 8}, confidence=1.0)
        plans["fam-c"]["cand-b"]["items"]["completeness"] = {
            "score": 2, "confidence": 1.0,
            "evidence": "ran python3 -m unittest tests.test_smoke locally",
            "rationale": "that test command fails on this candidate"}
        peers = peers_for(cands, plans)
        outcome = self._run(store, cands, peers)
        self.assertFalse(outcome.escalated)
        by_key = {(d["candidate_id"], d["row"]): d for d in outcome.dissent}
        self.assertIn(("cand-a", "question_fidelity"), by_key)
        entry = by_key[("cand-a", "question_fidelity")]
        self.assertEqual(entry["judge_family"], "fam-d")
        self.assertEqual(entry["score"], 2.0)
        self.assertEqual(entry["row_median"], 8.0)
        self.assertEqual(entry["cites_gate"], "measured")
        hard_entry = by_key[("cand-b", "completeness")]
        self.assertEqual(hard_entry["cites_gate"], "hard")
        self.assertEqual(outcome.rerun_requests,
                         {"cand-a": "measured", "cand-b": "hard"})

    def test_low_confidence_escalates(self):
        store = FakeStore()
        cands = candidates3()
        peers = peers_for(cands, all_uniform_plans(8), confidence=0.3)
        outcome = self._run(store, cands, peers)
        self.assertTrue(outcome.escalated)
        self.assertIn("cand-a", outcome.escalation_reason)
        self.assertIn("confidence", outcome.escalation_reason)
        self.assertIn("0.3", outcome.escalation_reason)
        self.assertAlmostEqual(
            outcome.per_candidate["cand-a"]["mean_confidence"], 0.3)
        # Scores themselves still pass; escalation is separate.
        self.assertTrue(outcome.per_candidate["cand-a"]["pass"])

    def test_two_family_contest_is_degraded(self):
        store = FakeStore()
        cands = candidates3()[:2]
        peers = peers_for(cands, all_uniform_plans(8))
        outcome = self._run(store, cands, peers)
        self.assertEqual(outcome.panel_status, "degraded")
        for cid in ("cand-a", "cand-b"):
            entry = outcome.per_candidate[cid]
            self.assertEqual(entry["panel_status"], "degraded")
            self.assertEqual(len(entry["peers"]), 1)

    def test_spread_above_threshold_escalates(self):
        store = FakeStore()
        cands = candidates3()
        plans = all_uniform_plans(7)
        plans["fam-b"]["cand-a"] = uniform(9)
        plans["fam-c"]["cand-a"] = uniform(5)
        peers = peers_for(cands, plans, confidence=1.0)
        outcome = self._run(store, cands, peers)
        self.assertTrue(outcome.escalated)
        self.assertIn("spread", outcome.escalation_reason)
        self.assertEqual(outcome.per_candidate["cand-a"]["spread"], 4.0)

    def test_invalid_cell_missing_rationale_discarded(self):
        store = FakeStore()
        cands = candidates3()
        plans = all_uniform_plans(8)
        bad = {"items": {row: {"score": 9, "confidence": 0.9,
                               "evidence": "src/impl.py:1", "rationale": ""}
                         for row in ROWS}}
        plans["fam-b"]["cand-a"] = bad
        peers = peers_for(cands, plans)
        outcome = self._run(store, cands, peers)
        rows_a = store.scores_for("cand-a")
        self.assertEqual(len(rows_a), 1)
        self.assertEqual(rows_a[0]["judge_id"], "expert-c")
        entry = outcome.per_candidate["cand-a"]
        self.assertEqual(entry["peers"], ["fam-c"])
        self.assertEqual(entry["panel_status"], "degraded")
        # The same peer's valid cells for other candidates still count.
        self.assertEqual(len(store.scores_for("cand-c")), 2)
        self.assertIn("expert-b", outcome.peers_used)


# ------------------------------------------------------------------ calibration

class TestComputeCalibration(unittest.TestCase):
    def test_agreement_laplace_active_and_writeback(self):
        cands = [
            {"id": "cand-pos", "family": "fam-a", "contract_id": "ctr-1"},
            {"id": "cand-neg", "family": "fam-b", "contract_id": "ctr-1"},
            {"id": "cand-unknown", "family": "fam-a", "contract_id": "ctr-2"},
        ]
        verdicts = {"ctr-1": {"contract_id": "ctr-1", "winner": "cand-pos",
                              "eliminated": ["cand-neg"], "escalated": False,
                              "judged": {}}}
        store = FakeStore(candidates=cands, verdicts=verdicts)

        def seed(cid, judge, family, mean):
            store.add_score(cid, "judged", "code_quality_v1", judge, family,
                            True, 1, {}, mean)

        seed("cand-pos", "judge-1", "fam-x", 8.0)   # agree (pos, >= 5)
        seed("cand-neg", "judge-1", "fam-x", 3.0)   # agree (neg, < 5)
        seed("cand-pos", "judge-2", "fam-y", 2.0)   # disagree
        seed("cand-neg", "judge-2", "fam-y", 8.0)   # disagree
        seed("cand-unknown", "judge-1", "fam-x", 9.0)  # no outcome: ignored
        seed("cand-pos", "judge-1", "fam-x", 9.0)   # gate filter check below
        store.scores[-1]["gate"] = "other"          # not matrix/judged: ignored

        experts = {"judge-1": {"family": "fam-x"},
                   "judge-2": {"family": "fam-y"}}
        result = compute_calibration(store, experts)
        # fam-x: (2 agree + 1) / (2 + 2) = 0.75, n=2, not yet active.
        self.assertAlmostEqual(result["fam-x"]["calibration"], 0.75)
        self.assertEqual(result["fam-x"]["n"], 2)
        self.assertFalse(result["fam-x"]["active"])
        # fam-y: (0 + 1) / (2 + 2) = 0.25.
        self.assertAlmostEqual(result["fam-y"]["calibration"], 0.25)
        self.assertEqual(result["fam-y"]["n"], 2)
        # Neither judge family had a candidate in ctr-1: no bias sample.
        self.assertIsNone(result["fam-x"]["bias"])
        # Write-back happened per expert with (value, n).
        updates = {eid: (v, n) for eid, v, n in store.calibration_updates}
        self.assertEqual(updates["judge-1"], (0.75, 2))
        self.assertEqual(updates["judge-2"], (0.25, 2))

    def test_bias_measured_but_penalty_not_applied_named_gap(self):
        # GAP-STYLE-PREFERENCE-PENALTY: the proxy quantity is reported,
        # but calibration stays agreement-only (see matrix docstring).
        cands = [
            {"id": "c1", "family": "fam-a", "contract_id": "ctr-9"},
            {"id": "c2", "family": "fam-b", "contract_id": "ctr-9"},
        ]
        verdicts = {"ctr-9": {"contract_id": "ctr-9", "winner": "c2",
                              "eliminated": ["c1"], "escalated": False,
                              "judged": {}}}
        store = FakeStore(candidates=cands, verdicts=verdicts)
        store.add_score("c2", "matrix", "validity_matrix_v1", "e-a",
                        "fam-a", True, 1, {}, 9.0)
        store.add_score("c2", "matrix", "validity_matrix_v1", "e-c",
                        "fam-c", True, 1, {}, 5.0)
        store.add_score("c1", "matrix", "validity_matrix_v1", "e-b",
                        "fam-b", True, 1, {}, 5.0)
        store.add_score("c1", "matrix", "validity_matrix_v1", "e-c",
                        "fam-c", True, 1, {}, 5.0)
        experts = {"e-a": {"family": "fam-a"}, "e-b": {"family": "fam-b"},
                   "e-c": {"family": "fam-c"}}
        result = compute_calibration(store, experts)
        # fam-a scored c2 at 9 vs panel median median(9, 5) = 7 -> bias 2.0,
        # and fam-a did have a candidate (c1) in ctr-9.
        self.assertAlmostEqual(result["fam-a"]["bias"], 2.0)
        # Agreement-only calibration: 1 agreeing cell -> (1+1)/(1+2).
        # A 0.9-per-full-point penalty would give 0.6667 * 0.9**2 = 0.54.
        self.assertAlmostEqual(result["fam-a"]["calibration"], 2 / 3)
        self.assertAlmostEqual(result["fam-b"]["bias"], 0.0)

    def test_compute_calibration_against_real_store(self):
        from hybrid_critic_loop.state import Store

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.db")
            try:
                store.upsert_expert("judge-1", "fam-x", "cli", "free")
                store.upsert_expert("judge-2", "fam-y", "cli", "free")
                target = store.create_target("do it", "code")
                contract_id = store.create_contract(
                    target, "implement", "fix it", "code", ["python"],
                    [{"class": "hard", "cmd": "true",
                      "expect": {"exit": 0}}])
                pos = store.create_candidate(
                    contract_id, 1, "implement", "exp-a", "fam-a", "A",
                    "/tmp/wt-a", "base")
                neg = store.create_candidate(
                    contract_id, 1, "implement", "exp-b", "fam-b", "B",
                    "/tmp/wt-b", "base")
                store.add_score(pos, "judged", "code_quality_v1", "judge-1",
                                "fam-x", True, 1, {"correctness": 8}, 8.0)
                store.add_score(neg, "judged", "code_quality_v1", "judge-1",
                                "fam-x", True, 1, {"correctness": 3}, 3.0)
                store.add_score(pos, "judged", "code_quality_v1", "judge-2",
                                "fam-y", True, 1, {"correctness": 2}, 2.0)
                store.create_verdict(
                    contract_id, 1, pos, [neg], "pass", "pass", {},
                    False, None, None, "pending", None)
                result = compute_calibration(store, store.list_experts())
                self.assertAlmostEqual(result["fam-x"]["calibration"], 0.75)
                self.assertEqual(result["fam-x"]["n"], 2)
                self.assertAlmostEqual(result["fam-y"]["calibration"],
                                       (0 + 1) / (1 + 2))
                # Real store write-back landed.
                self.assertAlmostEqual(
                    store.get_expert("judge-1")["judge_calibration"], 0.75)
            finally:
                store.close()


class TestGateCitation(unittest.TestCase):
    """Fix pass Wave 2.10 (review 1B-17): a hard-gate citation needs the
    command itself or a path-like token of it — plain words such as 'tests'
    or 'discover' never trigger a paid re-run."""

    CMD = "python3 -m unittest discover -s tests -p test_speed_sampler.py"

    def test_generic_words_do_not_cite(self):
        from hybrid_critic_loop.matrix import _cites_gate

        for text in ("the tests look fine to me", "I would discover more with unittest",
                     "all tests pass as far as I can see"):
            self.assertIsNone(_cites_gate(text, [], [self.CMD]), text)

    def test_paths_and_the_full_command_cite(self):
        from hybrid_critic_loop.matrix import _cites_gate

        self.assertEqual(_cites_gate("test_speed_sampler.py fails on this candidate", [], [self.CMD]), "hard")
        self.assertEqual(_cites_gate(f"ran `{self.CMD}` locally and it failed", [], [self.CMD]), "hard")
        # A module path is path-like too (the existing dissent test's shape).
        self.assertEqual(_cites_gate("tests.test_smoke failed for me", [],
                                     ["python3 -m unittest tests.test_smoke"]), "hard")
        self.assertEqual(_cites_gate("ops_per_tick looks misread", ["ops_per_tick"], [self.CMD]), "measured")


if __name__ == "__main__":
    unittest.main()
