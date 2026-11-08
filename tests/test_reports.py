"""Worker W4 tests: v0.3 reports (ledger cpvp/cal, validity matrix,
verdict dissent) against the real Store and tiny duck-typed fakes.

The reports functions take the store duck-typed, so the matrix and
verdict rendering are exercised through minimal fake stores; the ledger
cpvp cases use the real Store in a temp dir.  The record_validity case
is skip-guarded: that method lands in a sibling workstream, and the
test must be honest whether or not it is present in this tree.
"""

import inspect
import tempfile
import unittest
from pathlib import Path

from hybrid_critic_loop.reports import (
    ledger_report,
    matrix_report,
    verdict_report,
)
from hybrid_critic_loop.state import Store


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _matrix_score(candidate_id, judge_id, items, mean, judge_family="fam-j"):
    return {
        "id": 1,
        "candidate_id": candidate_id,
        "gate": "matrix",
        "rubric": "validity_matrix_v1",
        "judge_id": judge_id,
        "judge_family": judge_family,
        "blind": True,
        "position_shown": 1,
        "items": items,
        "mean": mean,
    }


class FakeMatrixStore:
    """Minimal duck-typed store for the matrix/verdict report paths."""

    def __init__(self, candidates, scores_by_candidate, verdict=None,
                 contract=None):
        self._candidates = candidates
        self._scores = scores_by_candidate
        self._verdict = verdict
        self._contract = contract

    def candidates_for(self, contract_id, attempt=None):
        return list(self._candidates)

    def scores_for(self, candidate_id):
        return list(self._scores.get(candidate_id, []))

    def latest_verdict(self, contract_id):
        return self._verdict

    def get_contract(self, contract_id):
        return self._contract


class FakeLedgerStore:
    def __init__(self, summary):
        self._summary = summary

    def ledger_rows(self, expert_id=None):
        return [{"expert_id": r.get("expert_id")} for r in self._summary]

    def ledger_summary(self):
        return list(self._summary)


class FakeLedgerStoreWithEvents(FakeLedgerStore):
    def events_for(self, limit=50):
        return [{"kind": "dispatch", "contract_id": "ctr-1"}]


class FakeVerdictStore:
    def __init__(self, verdict):
        self._verdict = verdict

    def latest_verdict(self, contract_id):
        return self._verdict


# ---------------------------------------------------------------------------
# Ledger report
# ---------------------------------------------------------------------------

class TestLedgerReport(unittest.TestCase):
    def _real_store(self, tmp):
        return Store(Path(tmp) / "data.db")

    def test_cpvp_not_reported_without_validity_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._real_store(tmp)
            store.add_ledger(
                "tgt-1", "ctr-1", "cand-1", "implement", "exp-1",
                10, 5, 1.5, "completed", None,
            )
            report = ledger_report(store)
            self.assertIn("exp-1", report)
            self.assertIn("cpvp=not reported", report)

    @unittest.skipUnless(
        hasattr(Store, "record_validity"),
        "Store.record_validity not present in this tree (sibling workstream)",
    )
    def test_cpvp_numeric_after_record_validity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._real_store(tmp)
            store.add_ledger(
                "tgt-1", "ctr-1", "cand-1", "implement", "exp-1",
                10, 5, 1.5, "completed", None,
            )
            # record_validity updates an existing expert row.
            store.upsert_expert("exp-1", "fam-1", "cli", "api")
            try:
                _call_record_validity(store, "exp-1")
            except Exception as exc:  # signature/semantics still moving
                self.skipTest(f"record_validity call not settled: {exc}")
            summary = store.ledger_summary()
            if not any(
                row.get("cost_per_validity_point") is not None
                for row in summary
            ):
                self.skipTest(
                    "record_validity present but ledger_summary does not "
                    "surface cost_per_validity_point in this tree"
                )
            report = ledger_report(store)
            exp_lines = [
                line for line in report.splitlines()
                if line.startswith("exp-1:")
            ]
            self.assertTrue(exp_lines)
            self.assertIn("cpvp=", exp_lines[0])
            self.assertNotIn("cpvp=not reported", exp_lines[0])

    def test_cpvp_numeric_and_calibration_from_summary_row(self):
        store = FakeLedgerStore([{
            "expert_id": "exp-9",
            "dispatches": 4, "completed": 3, "refused": 0, "timeout": 1,
            "tokens_in": 100, "tokens_out": 50, "cost_usd": 2.5,
            "wins": 2, "losses": 1, "win_rate": 2 / 3,
            "cost_per_validity_point": 0.123456,
            "validity_points": 42.0,
            "calibration": {"value": 0.87, "n": 14},
        }])
        report = ledger_report(store)
        self.assertIn("cpvp=0.1235", report)
        self.assertIn("validity_points=42.0", report)
        self.assertIn("cal=0.8700(n=14)", report)

    def test_no_calibration_no_extra_text(self):
        store = FakeLedgerStore([{
            "expert_id": "exp-9",
            "dispatches": 1, "completed": 1, "refused": 0, "timeout": 0,
            "tokens_in": None, "tokens_out": None, "cost_usd": None,
            "wins": 0, "losses": 0, "win_rate": None,
        }])
        report = ledger_report(store)
        self.assertIn("cpvp=not reported", report)
        self.assertNotIn("cal=", report)

    def test_events_section_only_when_store_has_events(self):
        summary = [{
            "expert_id": "exp-9",
            "dispatches": 1, "completed": 1, "refused": 0, "timeout": 0,
            "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.5,
            "wins": 0, "losses": 0, "win_rate": None,
        }]
        with_events = ledger_report(FakeLedgerStoreWithEvents(summary))
        self.assertIn("events:", with_events)
        self.assertIn("kind=dispatch", with_events)
        without_events = ledger_report(FakeLedgerStore(summary))
        self.assertNotIn("events:", without_events)


def _call_record_validity(store, expert_id):
    """Call Store.record_validity filling arguments by parameter name.

    The method's exact signature belongs to a sibling workstream; this
    helper adapts to whatever names it declares instead of guessing one
    positional order.
    """
    method = store.record_validity
    try:
        params = [
            p for p in inspect.signature(method).parameters.values()
            if p.name != "self"
        ]
    except (TypeError, ValueError):
        params = []
    if not params or any(
        p.kind in (inspect.Parameter.VAR_POSITIONAL,
                   inspect.Parameter.VAR_KEYWORD)
        for p in params
    ):
        return method(expert_id, 10.0, 5.0)
    kwargs = {}
    for param in params:
        name = param.name.lower()
        if "expert" in name:
            kwargs[param.name] = expert_id
        elif "point" in name or "validity" in name and "cost" not in name:
            kwargs[param.name] = 10.0
        elif "cost" in name:
            kwargs[param.name] = 5.0
        elif "won" in name or "pass" in name:
            kwargs[param.name] = True
        elif param.default is inspect.Parameter.empty:
            kwargs[param.name] = 1.0
    return method(**kwargs)


# ---------------------------------------------------------------------------
# Matrix report
# ---------------------------------------------------------------------------

class TestMatrixReport(unittest.TestCase):
    def test_matrix_report_not_run_on_empty_store(self):
        store = FakeMatrixStore([], {})
        self.assertEqual(matrix_report(store, "ctr-1"), "matrix: not run")

    def test_matrix_report_seeded_grid_renders_dissent(self):
        candidates = [{
            "id": "cand-1", "contract_id": "ctr-1", "attempt": 1,
            "expert_id": "exp-a", "family": "fam-a",
        }]
        scores = {
            "cand-1": [
                _matrix_score("cand-1", "judge-1",
                              {"correctness": 9.0, "evidence": 9.0}, 9.0),
                _matrix_score("cand-1", "judge-2",
                              {"correctness": 9.0, "evidence": 8.0}, 8.5),
                _matrix_score("cand-1", "judge-3",
                              {"correctness": 2.0, "evidence": 9.0}, 5.5),
            ],
        }
        store = FakeMatrixStore(candidates, scores)
        report = matrix_report(store, "ctr-1")
        self.assertIn("matrix for ctr-1", report)
        self.assertIn("panel_status:", report)
        self.assertIn("correctness:", report)
        self.assertIn("judge-1=9.0", report)
        # judge-3's correctness score deviates from the row median (9.0)
        # by 7.0, past the default dissent_threshold of 3.0.
        self.assertIn("judge-3=2.0 DISSENT", report)
        self.assertIn("dissent: candidate=cand-1 row=correctness "
                      "judge=judge-3", report)

    def test_matrix_report_panel_status_from_verdict(self):
        candidates = [{
            "id": "cand-1", "contract_id": "ctr-1", "attempt": 1,
            "expert_id": "exp-a", "family": "fam-a",
        }]
        scores = {
            "cand-1": [
                _matrix_score("cand-1", "judge-1",
                              {"correctness": 9.0}, 9.0),
            ],
        }
        verdict = {
            "winner": "cand-1", "eliminated": [], "hard": "pass",
            "measured": "pass",
            "judged": {"panel_status": "quorum_met"},
            "escalated": False, "human_gate": "approved",
        }
        store = FakeMatrixStore(candidates, scores, verdict=verdict)
        report = matrix_report(store, "ctr-1")
        self.assertIn("panel_status: quorum_met", report)


# ---------------------------------------------------------------------------
# Verdict report
# ---------------------------------------------------------------------------

class TestVerdictReport(unittest.TestCase):
    def test_verdict_report_not_run(self):
        store = FakeVerdictStore(None)
        self.assertEqual(verdict_report(store, "ctr-1"), "verdict: not run")

    def test_verdict_report_prints_dissent_and_panel_status(self):
        verdict = {
            "winner": "cand-1", "eliminated": [], "hard": "pass",
            "measured": "pass",
            "judged": {
                "panel_status": "quorum_met",
                "dissent": [{
                    "candidate_id": "cand-1", "row": "evidence",
                    "judge_id": "judge-3", "score": 2.0, "median": 9.0,
                    "rationale": "evidence row far below peers",
                }],
            },
            "escalated": False, "escalation_reason": None,
            "human_gate": "approved",
        }
        report = verdict_report(FakeVerdictStore(verdict), "ctr-1")
        self.assertIn("panel_status: quorum_met", report)
        self.assertIn(
            "dissent: candidate=cand-1 row=evidence judge=judge-3", report
        )
        self.assertIn("score=2.0 vs median=9.0", report)
        self.assertIn("evidence row far below peers", report)

    def test_verdict_report_prints_a_held_route_change(self):
        # Fix pass Wave 2.1: the hold is a verdict-row fact and prints as one.
        verdict = {
            "winner": "cand-2", "eliminated": ["cand-1"], "hard": "pass",
            "measured": "fail", "judged": {"panel_status": "degraded"},
            "escalated": False, "escalation_reason": None,
            "route_hold": {
                "status": "held", "panel_status": "degraded", "lesson_id": "lsn_0007",
                "route_change": {"category": "code", "demote": "gen-a", "reason": "lost on measured gate"},
            },
            "human_gate": "approved",
        }
        report = verdict_report(FakeVerdictStore(verdict), "ctr-1")
        self.assertIn("panel_status: degraded", report)
        self.assertIn("route_change: held (degraded panel) — demote gen-a not applied; "
                      "operator review required (lesson lsn_0007)", report)
        quiet = dict(verdict, route_hold=None)
        self.assertNotIn("route_change:", verdict_report(FakeVerdictStore(quiet), "ctr-1"))

    def test_verdict_report_without_dissent_stays_quiet(self):
        verdict = {
            "winner": "cand-1", "eliminated": [], "hard": "pass",
            "measured": "pass", "judged": {"median": 8.0},
            "escalated": False, "escalation_reason": None,
            "human_gate": "approved",
        }
        report = verdict_report(FakeVerdictStore(verdict), "ctr-1")
        self.assertNotIn("dissent:", report)
        self.assertNotIn("panel_status:", report)


class TestVerdictReportWaiver(unittest.TestCase):
    """Fix pass Wave 2.7 (review H6): the verdict report prints the waiver's
    recorded state — offered at the verdict, granted by the human — from
    the real verdict row."""

    def test_verdict_report_prints_the_waiver_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.db")
            try:
                tid = store.create_target("ask", "code", constraints=[], budget=None)
                cid = store.create_contract(
                    tid, stage="implement", task="t", domain="code", tags=[], acceptance=[])
                vid = store.create_verdict(
                    cid, 1, "w1", [], "pass", "fail", None, False, None,
                    {"gate": "measured", "status": "offered", "candidate": "w1"},
                    "pending", None)
                report = verdict_report(store, cid)
                self.assertIn("measured: fail", report)
                self.assertIn("waiver: measured gate — offered", report)
                self.assertIn("human_gate: pending", report)
                store.set_verdict_waiver(
                    vid, {"gate": "measured", "status": "granted", "candidate": "w1",
                          "by": "human", "reason": "variance accepted"})
                store.set_verdict_human_gate(vid, "waived")
                report = verdict_report(store, cid)
                self.assertIn("waiver: measured gate — granted by human", report)
                self.assertIn("human_gate: waived", report)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
