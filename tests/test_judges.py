"""Tests for hybrid_critic_loop.judges (Worker C).

Stub judge executors (objects with .family and .judge(packet, rubric))
return canned per-label scores. The canned scores are identical for
labels A and B, so results are independent of the seeded per-judge
shuffle. The store is the same in-memory FakeStore pattern as
test_gates.py -- no dependency on Worker A's state.py.
"""

import unittest

from hybrid_critic_loop.errors import PanelError
from hybrid_critic_loop.gates import CandidateGateResult, MeasureResult
from hybrid_critic_loop.judges import (
    blind_text,
    build_packet,
    recompute_panel,
    run_judged_gate,
    select_panel,
)
from hybrid_critic_loop.rubrics import get_rubric

ITEMS = ["correctness", "idiom", "readability"]


class FakeStore:
    def __init__(self, experts=None):
        self.measurements = []
        self.scores = []
        self._experts = experts or {}

    def add_measurement(self, candidate_id, sensor, metric, value, op, threshold, passed):
        row = {"candidate_id": candidate_id, "sensor": sensor, "metric": metric,
               "value": value, "op": op, "threshold": threshold, "passed": passed}
        self.measurements.append(row)
        return row

    def add_score(self, candidate_id, gate, rubric, judge_id, judge_family,
                  blind, position_shown, items, mean):
        row = {"candidate_id": candidate_id, "gate": gate, "rubric": rubric,
               "judge_id": judge_id, "judge_family": judge_family, "blind": blind,
               "position_shown": position_shown, "items": items, "mean": mean}
        self.scores.append(row)
        return row

    def measurements_for(self, candidate_id):
        return [m for m in self.measurements if m["candidate_id"] == candidate_id]

    def scores_for(self, candidate_id):
        return [s for s in self.scores if s["candidate_id"] == candidate_id]

    def get_expert(self, expert_id):
        return self._experts[expert_id]


def scored_entry(scores, evidence="checked against the diff"):
    return {"items": {name: {"score": scores[name], "evidence": evidence}
                      for name in scores}}


class StubJudge:
    """Executor stub: .family + .judge(packet, rubric) -> canned dict."""

    def __init__(self, family, responder):
        self.family = family
        self._responder = responder
        self.packets = []

    def judge(self, packet, rubric):
        self.packets.append(packet)
        if callable(self._responder):
            return self._responder(packet, rubric)
        return self._responder


def uniform_judge(family, score):
    canned = {label: scored_entry({name: score for name in ITEMS})
              for label in ("A", "B")}
    return StubJudge(family, canned)


def candidates():
    return [
        {"id": "cand-1", "family": "fam-alpha", "expert_id": "expert-alpha",
         "provenance": "generated", "evidence": {"log_hash": "sha256:1"}},
        {"id": "cand-2", "family": "fam-beta", "expert_id": "expert-beta",
         "provenance": "generated", "evidence": {"log_hash": "sha256:2"}},
    ]


def gate_results():
    measured = [MeasureResult(
        sensor="coverage-sensor",
        metrics=[{"candidate_id": "cand-1", "sensor": "coverage-sensor",
                  "metric": "coverage", "value": 90, "op": ">=",
                  "threshold": 80, "passed": True}],
        passed=True,
    )]
    return {
        "cand-1": CandidateGateResult("cand-1", True, None, [], measured, True, True),
        "cand-2": CandidateGateResult("cand-2", True, None, [], [], True, None),
    }


def diffs():
    return {
        "cand-1": ("commit deadbeef\nAuthor: expert-alpha\n"
                   "diff --git a/wt/cand-1/x.py b/wt/cand-1/x.py\n"
                   "+return 1  # by expert-alpha of fam-alpha"),
        "cand-2": ("commit cafef00d\nAuthor: expert-beta\n"
                   "diff --git a/wt/cand-2/y.py b/wt/cand-2/y.py\n"
                   "+return 2  # by expert-beta of fam-beta"),
    }


def judged_item(**overrides):
    item = {"class": "judged", "rubric": "code_quality_v1", "min": 7,
            "judges": 3, "spread_threshold": 2.0}
    item.update(overrides)
    return item


class TestSelectPanel(unittest.TestCase):
    def setUp(self):
        self.experts = {
            "j1": {"family": "fam-a"},
            "j2": {"family": "fam-b"},
            "j3": {"family": "fam-c"},
            "j4": {"family": "fam-d"},
        }

    def test_family_exclusion_and_extension(self):
        panel = select_panel(["j1", "j2"], self.experts, {"fam-a"}, 2)
        self.assertEqual(panel, ["j2", "j3"])  # j1 excluded, extended sorted
        for judge_id in panel:
            self.assertNotIn(self.experts[judge_id]["family"], {"fam-a"})

    def test_route_order_preserved(self):
        panel = select_panel(["j4", "j3"], self.experts, set(), 2)
        self.assertEqual(panel, ["j4", "j3"])

    def test_available_filter(self):
        panel = select_panel(["j2", "j3"], self.experts, set(), 2,
                             available={"j3", "j4"})
        self.assertEqual(panel, ["j3", "j4"])

    def test_shortfall_raises_panel_error(self):
        with self.assertRaises(PanelError) as ctx:
            select_panel(["j1"], self.experts, {"fam-a", "fam-b", "fam-c"}, 2)
        self.assertIn("shortfall", str(ctx.exception))

    def test_recompute_panel_matches_select_panel(self):
        route = {"judges": ["j1", "j2"]}
        self.assertEqual(
            recompute_panel(route, self.experts, {"fam-a"}, 2, None),
            select_panel(["j1", "j2"], self.experts, {"fam-a"}, 2),
        )
        self.assertEqual(
            recompute_panel(["j1", "j2"], self.experts, {"fam-a"}, 2, None),
            ["j2", "j3"],
        )


class TestBlindText(unittest.TestCase):
    def test_strips_identity(self):
        text = (
            "commit abc123\n"
            "Author: expert-alpha <a@example.com>\n"
            "Date: Fri Nov 7 2026\n"
            "diff --git a/wt/cand-1/f.py b/wt/cand-1/f.py\n"
            "index 1234567..89abcde 100644\n"
            "+++ b/wt/cand-1/f.py\n"
            "path /home/dino/work/file.py changed\n"
            "written by EXPERT-ALPHA of Fam-Alpha\n"
            "+actual code line\n"
        )
        out = blind_text(text, ["expert-alpha", "fam-alpha"])
        self.assertNotIn("commit abc123", out)
        self.assertNotIn("Author:", out)
        self.assertNotIn("Date:", out)
        self.assertNotIn("diff --git", out)
        self.assertNotIn("index 1234567", out)
        self.assertNotIn("wt/cand-1", out)
        self.assertIn("wt/<candidate>", out)
        self.assertNotIn("/home/dino", out)
        self.assertIn("<path>", out)
        self.assertNotIn("expert-alpha", out.lower())
        self.assertNotIn("fam-alpha", out.lower())
        self.assertIn("<expert>", out)
        self.assertIn("+actual code line", out)


class TestBuildPacket(unittest.TestCase):
    def test_packet_structure_and_no_identities(self):
        packet = build_packet(
            {"A": "some diff A", "B": "some diff B"},
            "Candidate A: hard gates PASS",
            get_rubric("code_quality_v1"),
        )
        self.assertIn("code_quality_v1", packet)
        self.assertIn("correctness", packet)
        self.assertIn("SCALE: 0-10", packet)
        self.assertIn("=== CANDIDATE A ===", packet)
        self.assertIn("=== CANDIDATE B ===", packet)
        self.assertIn("hard gates PASS", packet)


class TestRunJudgedGate(unittest.TestCase):
    def _run(self, store, executors, panel, item=None, cands=None):
        return run_judged_gate(
            store, {"acceptance": []}, item or judged_item(),
            cands or candidates(), gate_results(), diffs(),
            executors, panel, seed=0,
        )

    def test_median_pass_rule_and_scores_stored(self):
        store = FakeStore()
        executors = {
            "jg": uniform_judge("fam-gamma", 8),
            "jd": uniform_judge("fam-delta", 8),
            "je": uniform_judge("fam-epsilon", 8),
        }
        outcome = self._run(store, executors, ["jg", "jd", "je"])
        self.assertFalse(outcome.escalated)
        self.assertEqual(sorted(outcome.judges_used), ["jd", "je", "jg"])
        for cid in ("cand-1", "cand-2"):
            entry = outcome.per_candidate[cid]
            self.assertEqual(entry["median_mean"], 8.0)
            self.assertEqual(entry["min_item"], 8.0)
            self.assertEqual(entry["spread"], 0.0)
            self.assertTrue(entry["pass"])
            rows = store.scores_for(cid)
            self.assertEqual(len(rows), 3)
            for row in rows:
                self.assertEqual(row["gate"], "judged")
                self.assertEqual(row["rubric"], "code_quality_v1")
                self.assertTrue(row["blind"])
                self.assertIn(row["position_shown"], (1, 2))
                self.assertEqual(row["mean"], 8.0)
        # Judges never saw an expert id or family name.
        for judge in executors.values():
            for packet in judge.packets:
                self.assertNotIn("expert-alpha", packet)
                self.assertNotIn("expert-beta", packet)
                self.assertNotIn("fam-alpha", packet)
                self.assertNotIn("fam-beta", packet)
                self.assertNotIn("wt/cand-1", packet)
                self.assertNotIn("wt/cand-2", packet)
                self.assertIn("coverage=90", packet)  # but DO see measurements

    def test_min_item_below_min_minus_2_fails_despite_high_median(self):
        store = FakeStore()
        scores = {"correctness": 10, "idiom": 10, "readability": 4}
        canned = {label: scored_entry(scores) for label in ("A", "B")}
        executors = {
            "jg": StubJudge("fam-gamma", canned),
            "jd": StubJudge("fam-delta", canned),
            "je": StubJudge("fam-epsilon", canned),
        }
        outcome = self._run(store, executors, ["jg", "jd", "je"])
        self.assertFalse(outcome.escalated)
        for cid in ("cand-1", "cand-2"):
            entry = outcome.per_candidate[cid]
            self.assertEqual(entry["median_mean"], 8.0)  # >= min 7
            self.assertEqual(entry["min_item"], 4.0)     # < min - 2 = 5
            self.assertFalse(entry["pass"])

    def test_spread_above_threshold_escalates(self):
        store = FakeStore()
        executors = {
            "jg": uniform_judge("fam-gamma", 9),
            "jd": uniform_judge("fam-delta", 5),
        }
        outcome = self._run(store, executors, ["jg", "jd"],
                            item=judged_item(judges=2, min=5))
        self.assertTrue(outcome.escalated)
        self.assertIn("judge spread", outcome.escalation_reason)
        self.assertIn("2.0", outcome.escalation_reason)
        self.assertEqual(outcome.per_candidate["cand-1"]["spread"], 4.0)

    def test_score_without_evidence_discarded(self):
        store = FakeStore()
        no_evidence = {label: {"items": {name: 9 for name in ITEMS}}
                       for label in ("A", "B")}
        executors = {
            "jg": uniform_judge("fam-gamma", 8),
            "jbad": StubJudge("fam-delta", no_evidence),
        }
        outcome = self._run(store, executors, ["jg", "jbad"],
                            item=judged_item(judges=1))
        self.assertFalse(outcome.escalated)
        self.assertEqual(outcome.judges_used, ["jg"])  # jbad dropped
        for cid in ("cand-1", "cand-2"):
            rows = store.scores_for(cid)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["judge_id"], "jg")

    def test_panel_collapse_escalates(self):
        store = FakeStore()
        no_evidence = {label: {"items": {name: 9 for name in ITEMS}}
                       for label in ("A", "B")}
        executors = {"jbad": StubJudge("fam-gamma", no_evidence)}
        outcome = self._run(store, executors, ["jbad"],
                            item=judged_item(judges=1))
        self.assertTrue(outcome.escalated)
        self.assertIn("judge panel fell below 1 after validation",
                      outcome.escalation_reason)
        self.assertEqual(outcome.judges_used, [])
        self.assertEqual(store.scores, [])

    def test_none_result_judge_dropped(self):
        store = FakeStore()
        executors = {
            "jg": uniform_judge("fam-gamma", 8),
            "jnone": StubJudge("fam-delta", None),
        }
        outcome = self._run(store, executors, ["jg", "jnone"],
                            item=judged_item(judges=1))
        self.assertEqual(outcome.judges_used, ["jg"])

    def test_contest_family_judge_raises_panel_error(self):
        store = FakeStore()
        executors = {
            "jself": uniform_judge("fam-alpha", 8),  # cand-1's family!
            "jg": uniform_judge("fam-gamma", 8),
        }
        with self.assertRaises(PanelError):
            self._run(store, executors, ["jself", "jg"],
                      item=judged_item(judges=1))
        self.assertEqual(store.scores, [])  # nothing was judged

    def test_missing_rubric_item_discarded(self):
        store = FakeStore()
        partial = {label: {"items": {
            "correctness": {"score": 9, "evidence": "ok"},
            "idiom": {"score": 9, "evidence": "ok"},
            # readability missing entirely
        }} for label in ("A", "B")}
        executors = {"jp": StubJudge("fam-gamma", partial)}
        outcome = self._run(store, executors, ["jp"],
                            item=judged_item(judges=1))
        self.assertTrue(outcome.escalated)  # judge dropped -> below needed
        self.assertEqual(store.scores, [])


if __name__ == "__main__":
    unittest.main()
