"""Owner-decisions fix pass, Unit 1 — Item 1 = Option A (2026-11-09).

The ruling (Claude's suggestion, adopted verbatim by the owner;
FIX-SPEC-DECISIONS-2026-11-09 D1): "Demote = move the loser behind
every seat that passed the deciding gate; never promote a seat that
failed it; a waiver demotes nobody — the human accepted the measured
failure."

Pins, per defect in OWNER-DECISIONS-MM Item 1:

* defect 1 — promotion never selects a seat that failed the deciding
  gate (the old code promoted ``compete_with`` unchecked), and the
  demoted loser lands behind every gate-passing seat in route order;
* defect 2 — the lineup can never end with ``primary ==
  compete_with``;
* defect 3 — demoting a seat with no lineup position is an honest
  no-op: the route row is not rewritten and no ledger row claims a
  change; and when a positioned seat cannot move because no
  gate-passing seat exists, the demotion is recorded honestly (a
  ledger row saying exactly that) instead of an unqualified
  promotion;
* defect 4 — a waived verdict produces NO route_change in
  ``lessons.derive_outcome_cause`` (suppression, option A — not a
  held row), so no demotion is applied downstream.
"""

import unittest

from hybrid_critic_loop import lessons, router
from hybrid_critic_loop.state import Store

CONTRACT = {"id": "c1", "domain": "code.game", "tags": ["perf"]}


def _route_order(route):
    order = [route["primary"], route["compete_with"]]
    order += list(route["fallback"])
    return [seat for seat in order if seat is not None]


class RouteStoreCase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.upsert_route(
            "code", "exp-a", "exp-b", ["exp-c", "exp-d", "exp-e"],
            ["j"], "r")

    def tearDown(self):
        self.store.close()


class TestGateCheckedPromotion(RouteStoreCase):
    """Defect 1: promotion is gate-checked; the loser lands behind
    every seat that passed the deciding gate."""

    def test_compete_with_that_failed_is_not_promoted(self):
        # exp-b (compete_with) failed the deciding gate; only exp-c
        # passed. The old code promoted exp-b unchecked.
        result = self.store.apply_route_demotion(
            "code", "exp-a", "exp-b", "exp-a lost",
            passed_experts={"exp-c"})
        self.assertTrue(result["changed"])
        self.assertEqual(result["primary"], "exp-c")
        self.assertEqual(result["promoted"], "exp-c")
        order = _route_order(result)
        self.assertLess(order.index("exp-c"), order.index("exp-a"))

    def test_loser_lands_behind_every_gate_passing_seat(self):
        result = self.store.apply_route_demotion(
            "code", "exp-a", None, "exp-a lost",
            passed_experts={"exp-b", "exp-d"})
        order = _route_order(result)
        self.assertLess(order.index("exp-b"), order.index("exp-a"))
        self.assertLess(order.index("exp-d"), order.index("exp-a"))
        self.assertEqual(result["primary"], "exp-b")

    def test_router_promotes_only_a_gate_passing_seat(self):
        line = router.apply_lesson_route_change(
            self.store, "code",
            {"demote": "exp-a", "reason": "lost on measured gate"},
            "c1", gate_passed={"exp-d"})
        route = self.store.get_route("code")
        self.assertEqual(route["primary"], "exp-d")
        self.assertIn("promoted exp-d", line)
        rows = self.store.ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("route_change demote exp-a", rows[0]["note"])

    def test_router_ignores_an_explicit_promote_that_failed(self):
        line = router.apply_lesson_route_change(
            self.store, "code",
            {"demote": "exp-a", "promote": "exp-b",
             "reason": "lost on measured gate"},
            "c1", gate_passed={"exp-c"})
        route = self.store.get_route("code")
        self.assertEqual(route["primary"], "exp-c")
        self.assertNotIn("promoted exp-b", line)

    def test_no_gate_passing_seat_records_the_demotion_honestly(self):
        # The only gate-passing seat holds no route position: the
        # route cannot move, no unqualified promotion happens, and
        # the demotion is recorded as exactly that.
        before = self.store.get_route("code")
        line = router.apply_lesson_route_change(
            self.store, "code",
            {"demote": "exp-a", "reason": "lost on measured gate"},
            "c1", gate_passed={"exp-outsider"})
        after = self.store.get_route("code")
        self.assertEqual(after, before)
        self.assertIn("no seat that passed the deciding gate", line)
        rows = self.store.ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("no seat that passed the deciding gate",
                      rows[0]["note"])


class TestPrimaryNeverEqualsCompeteWith(RouteStoreCase):
    """Defect 2: primary == compete_with is unreachable."""

    def test_legacy_promote_of_primary_into_compete_seat_is_refused(self):
        # The old router default named the primary as the promote for
        # the challenger's demotion; the store seated it in both
        # slots. Now the compete seat is vacated instead.
        result = self.store.apply_route_demotion(
            "code", "exp-b", "exp-a", "exp-b refused")
        self.assertEqual(result["primary"], "exp-a")
        self.assertIsNone(result["compete_with"])
        self.assertIsNone(result["promoted"])
        self.assertEqual(result["fallback"][0], "exp-b")

    def test_challenger_demotion_with_gate_results(self):
        result = self.store.apply_route_demotion(
            "code", "exp-b", None, "exp-b lost",
            passed_experts={"exp-a", "exp-c"})
        self.assertEqual(result["primary"], "exp-a")
        self.assertEqual(result["compete_with"], "exp-c")
        self.assertEqual(result["promoted"], "exp-c")
        self.assertNotEqual(result["primary"], result["compete_with"])


class TestHonestNoOp(RouteStoreCase):
    """Defect 3: demoting a seat with no lineup position writes
    nothing and claims nothing."""

    def test_unknown_seat_demotion_rewrites_nothing(self):
        before = self.store.get_route("code")
        result = self.store.apply_route_demotion(
            "code", "exp-ghost", "exp-c", "not seated")
        self.assertFalse(result["changed"])
        self.assertEqual(result["branch"], "none")
        # Route row untouched, updated_at included.
        self.assertEqual(self.store.get_route("code"), before)

    def test_router_unknown_seat_writes_no_ledger_row(self):
        before = self.store.get_route("code")
        for kwargs in ({}, {"gate_passed": {"exp-a", "exp-c"}}):
            line = router.apply_lesson_route_change(
                self.store, "code",
                {"demote": "exp-ghost", "reason": "not seated"},
                "c9", **kwargs)
            self.assertIn("no route change", line)
        self.assertEqual(self.store.get_route("code"), before)
        self.assertEqual(self.store.ledger_rows(), [])


class TestWaiverDemotesNobody(unittest.TestCase):
    """Defect 4 / ruling clause 3: a waived verdict emits no
    route_change, so no demotion is applied downstream."""

    def _measured_failure_details(self):
        return {
            "measurements": [{
                "candidate_id": "cand-a", "metric": "alloc_bytes_per_tick",
                "value": 512, "op": "<=", "threshold": 0, "pass": False,
            }],
            "candidate_experts": {"cand-a": "codex"},
            "hard_failures": [], "judged": None, "winner_expert": "codex",
            "escalation_reason": None,
        }

    def test_unwaived_measured_failure_still_demotes(self):
        # Control: the same failure without a waiver keeps its
        # route_change — the ruling suppresses only the waived case.
        _, _, _, route_change, _ = lessons.derive_outcome_cause(
            CONTRACT, {"contract_id": "c1"},
            self._measured_failure_details())
        self.assertEqual(
            route_change,
            {"category": "code", "demote": "codex",
             "reason": "lost on measured gate"})

    def test_waived_verdict_emits_no_route_change(self):
        verdict = {
            "contract_id": "c1",
            "waiver": {"gate": "measured", "status": "offered",
                       "candidate": "cand-a"},
        }
        outcome, _, _, route_change, _ = lessons.derive_outcome_cause(
            CONTRACT, verdict, self._measured_failure_details())
        self.assertIn("failed the measured gate", outcome)
        self.assertIsNone(route_change)

    def test_waiver_candidate_keys_no_longer_suppress(self):
        # Round-9 fix M5: _verdict_waived keeps ONLY the verdict's
        # ``waiver`` record — the one channel with a production
        # writer. The ``waiver_candidate`` legs (on the verdict and
        # on gate_details) had no production writer and are removed:
        # neither key suppresses a demotion any more. (This pin
        # replaces the decisions-pass pin that asserted suppression
        # via gate_details["waiver_candidate"].)
        details = self._measured_failure_details()
        details["waiver_candidate"] = {"id": "cand-a"}
        _, _, _, route_change, _ = lessons.derive_outcome_cause(
            CONTRACT, {"contract_id": "c1"}, details)
        self.assertEqual(
            route_change,
            {"category": "code", "demote": "codex",
             "reason": "lost on measured gate"})
        _, _, _, route_change, _ = lessons.derive_outcome_cause(
            CONTRACT,
            {"contract_id": "c1", "waiver_candidate": {"id": "cand-a"}},
            self._measured_failure_details())
        self.assertEqual(
            route_change,
            {"category": "code", "demote": "codex",
             "reason": "lost on measured gate"})

    def test_waived_lesson_stores_no_route_change(self):
        # Downstream: write_lesson stores the derived route_change and
        # the loop applies it when present — a waived verdict's stored
        # lesson carries none, so nothing is applied.
        store = Store(":memory:")
        try:
            verdict = {
                "contract_id": "c1",
                "waiver": {"gate": "measured", "status": "granted",
                           "candidate": "cand-a"},
            }
            lesson = lessons.write_lesson(
                store, None, CONTRACT, verdict,
                self._measured_failure_details())
            self.assertIsNone(lesson["route_change"])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
