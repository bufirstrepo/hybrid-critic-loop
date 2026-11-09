"""Round-9 fix pass, Unit C — M4 (route seat-set invariant).

FIX-SPEC-ROUND9-2026-11-09 M4 (lane-4 F1/F2/F3/F4/F5): EVERY
demotion mode preserves the route's seat set — each seat present
before the demotion is present after, exactly once. A demotion
reorders; it never duplicates or deletes. Concretely:

* legacy promotion of a fallback member MOVES it (lane-4 F1: the
  promoted seat used to hold its fallback slot too —
  ``[F1, P, F1, F2]``);
* legacy promotion of anyone other than the old ``compete_with``
  on a primary demotion displaces the old compete seat to the head
  of the fallback, named in the result's ``displaced`` and in the
  router's line/ledger (lane-4 F2: a non-route promote used to
  evict ``compete_with`` from the route entirely, silently);
* the passed-mode rebuild keeps the same invariant, with a
  set-based (linear-time) dedupe;
* the router's line names where the demotee actually landed
  (lane-4 F4: a passed-mode fallback demotee placed ON the compete
  seat was described as demoted "within the fallback order"), and
  the honest-record branch covers the fallback-demote /
  zero-passer case (the demotee DOES hold a route position — no
  false "holds no route position" line, and the ledger records the
  unmovable demotion);
* ``Store.upsert_route`` refuses ``primary == compete_with``
  (lane-4 F5: the write boundary had no defect-2 guard).
"""

import itertools
import unittest

from hybrid_critic_loop import router
from hybrid_critic_loop.state import Store

SEATS = ["P", "C", "F1", "F2"]


def _order(route):
    order = [route["primary"], route["compete_with"]]
    order += list(route["fallback"])
    return [seat for seat in order if seat is not None]


class RouteCase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.upsert_route(
            "code", "P", "C", ["F1", "F2"], ["J1"], "rule-x")

    def tearDown(self):
        self.store.close()

    def assert_seat_set_preserved(self, result, extra=()):
        order = _order(result)
        self.assertEqual(
            len(order), len(set(order)),
            f"a seat holds two places: {order}")
        self.assertEqual(
            set(order), set(SEATS) | set(extra),
            f"seat set not preserved: {order}")
        if result["compete_with"] is not None:
            self.assertNotEqual(result["primary"], result["compete_with"])


class TestLegacySeatSet(RouteCase):
    """The legacy branches (no gate results): a promoted seat moves;
    a displaced seat keeps a fallback place."""

    def test_promote_fallback_member_from_primary_moves_it(self):
        result = self.store.apply_route_demotion("code", "P", "F1", "t")
        self.assertEqual(_order(result), ["F1", "P", "C", "F2"])
        self.assertEqual(result["promoted"], "F1")
        self.assertEqual(result["displaced"], "C")
        self.assert_seat_set_preserved(result)

    def test_promote_non_route_expert_keeps_old_compete(self):
        result = self.store.apply_route_demotion("code", "P", "XX", "t")
        self.assertEqual(_order(result), ["XX", "P", "C", "F1", "F2"])
        self.assertEqual(result["promoted"], "XX")
        self.assertEqual(result["displaced"], "C")
        self.assert_seat_set_preserved(result, extra={"XX"})

    def test_promote_compete_from_primary_displaces_nobody(self):
        result = self.store.apply_route_demotion("code", "P", "C", "t")
        self.assertEqual(_order(result), ["C", "P", "F1", "F2"])
        self.assertEqual(result["promoted"], "C")
        self.assertIsNone(result["displaced"])
        self.assert_seat_set_preserved(result)

    def test_promote_fallback_member_from_compete_moves_it(self):
        result = self.store.apply_route_demotion("code", "C", "F1", "t")
        self.assertEqual(_order(result), ["P", "F1", "C", "F2"])
        self.assertEqual(result["promoted"], "F1")
        self.assert_seat_set_preserved(result)

    def test_promote_non_route_expert_from_compete_keeps_everyone(self):
        result = self.store.apply_route_demotion("code", "C", "XX", "t")
        self.assertEqual(_order(result), ["P", "XX", "C", "F1", "F2"])
        self.assertEqual(result["promoted"], "XX")
        self.assert_seat_set_preserved(result, extra={"XX"})

    def test_promote_primary_from_compete_vacates_without_loss(self):
        result = self.store.apply_route_demotion("code", "C", "P", "t")
        self.assertEqual(result["primary"], "P")
        self.assertIsNone(result["compete_with"])
        self.assertIsNone(result["promoted"])
        self.assert_seat_set_preserved(result)

    def test_legacy_sweep_preserves_the_seat_set(self):
        promotes = [None, "P", "C", "F1", "F2", "XX"]
        for demote in SEATS + ["ZZ"]:
            for promote in promotes:
                store = Store(":memory:")
                try:
                    store.upsert_route(
                        "code", "P", "C", ["F1", "F2"], ["J1"], "rule-x")
                    result = store.apply_route_demotion(
                        "code", demote, promote, "t")
                    order = _order(result)
                    self.assertEqual(
                        len(order), len(set(order)),
                        f"demote={demote} promote={promote}: {order}")
                    # No seat is ever LOST, whatever the shape.
                    self.assertLessEqual(
                        set(SEATS), set(order),
                        f"demote={demote} promote={promote}: {order}")
                    # A non-route promote may join only by taking a
                    # top slot on a positioned demotion.
                    self.assertLessEqual(
                        set(order), set(SEATS) | {"XX"},
                        f"demote={demote} promote={promote}: {order}")
                    if result["compete_with"] is not None:
                        self.assertNotEqual(
                            result["primary"], result["compete_with"])
                finally:
                    store.close()


class TestPassedSeatSet(RouteCase):
    """The passed-mode rebuild: same invariant, set-based dedupe."""

    def test_passed_sweep_preserves_the_seat_set(self):
        subsets = [
            set(combo)
            for size in range(0, len(SEATS) + 1)
            for combo in itertools.combinations(SEATS, size)
        ]
        for demote in SEATS + ["ZZ"]:
            for passed in subsets:
                store = Store(":memory:")
                try:
                    store.upsert_route(
                        "code", "P", "C", ["F1", "F2"], ["J1"], "rule-x")
                    result = store.apply_route_demotion(
                        "code", demote, None, "t", passed_experts=passed)
                    order = _order(result)
                    self.assertEqual(
                        set(order), set(SEATS),
                        f"demote={demote} passed={passed}: {order}")
                    self.assertEqual(
                        len(order), len(set(order)),
                        f"demote={demote} passed={passed}: {order}")
                    if result["compete_with"] is not None:
                        self.assertNotEqual(
                            result["primary"], result["compete_with"])
                finally:
                    store.close()

    def test_duplicate_fallback_entries_normalize_to_the_set(self):
        # Lane-4 F3 shape: the rebuild emits each seat exactly once
        # (the invariant is on the seat SET).
        store = Store(":memory:")
        try:
            store.upsert_route(
                "code", "P", "C", ["F1", "F1", "F2"], ["J1"], "rule-x")
            result = store.apply_route_demotion(
                "code", "F2", None, "t", passed_experts={"P"})
            self.assertEqual(_order(result), ["P", "F2", "C", "F1"])
        finally:
            store.close()

    def test_wide_route_rebuild_preserves_every_seat(self):
        fallback = [f"exp-{i}" for i in range(400)]
        passed = {"P"} | set(fallback[::2])
        store = Store(":memory:")
        try:
            store.upsert_route("code", "P", "C", fallback, [], "rule-x")
            result = store.apply_route_demotion(
                "code", "P", None, "t", passed_experts=passed)
            order = _order(result)
            self.assertEqual(len(order), 402)
            self.assertEqual(len(set(order)), 402)
            self.assertEqual(
                set(order), {"P", "C"} | set(fallback))
            # The loser sits behind every gate-passing seat.
            loser_at = order.index("P")
            for seat in passed - {"P"}:
                self.assertLess(order.index(seat), loser_at)
        finally:
            store.close()


class TestUpsertRefusesPrimaryEqualsCompete(unittest.TestCase):
    def test_upsert_refuses_and_writes_nothing(self):
        store = Store(":memory:")
        try:
            with self.assertRaises(ValueError):
                store.upsert_route("code", "A", "A", [], [], "r")
            self.assertIsNone(store.get_route("code"))
        finally:
            store.close()

    def test_refused_upsert_leaves_the_existing_route_intact(self):
        store = Store(":memory:")
        try:
            store.upsert_route("code", "P", "C", ["F1"], ["J1"], "rule-x")
            before = store.get_route("code")
            with self.assertRaises(ValueError):
                store.upsert_route("code", "P", "P", ["F2"], [], "rule-y")
            self.assertEqual(store.get_route("code"), before)
        finally:
            store.close()

    def test_upsert_accepts_distinct_seats_and_no_compete(self):
        store = Store(":memory:")
        try:
            store.upsert_route("code", "P", "C", ["F1"], [], "r")
            store.upsert_route("code", "P", None, ["F1"], [], "r")
            route = store.get_route("code")
            self.assertEqual(route["primary"], "P")
            self.assertIsNone(route["compete_with"])
        finally:
            store.close()


class TestRouterRecordsRound9(RouteCase):
    """The router's line (and ledger) name what actually happened."""

    def test_fallback_demotee_on_the_compete_seat_is_named(self):
        # Lane-4 F4: demote F1 (fallback head), only F2 passed —
        # the rebuild seats F2 primary and F1 ON the compete seat.
        line = router.apply_lesson_route_change(
            self.store, "code", {"demote": "F1", "reason": "lost"},
            "ctr9", gate_passed={"F2"})
        route = self.store.get_route("code")
        self.assertEqual(route["compete_with"], "F1")
        self.assertIn("compete seat", line)
        self.assertNotIn("within the fallback order", line)

    def test_fallback_demote_with_zero_passers_gets_the_honest_record(self):
        # The R5 shape: the demotee holds a fallback position but no
        # gate-passing seat exists — the honest-record branch, not
        # the false "holds no route position" line, and a ledger row.
        before = self.store.get_route("code")
        line = router.apply_lesson_route_change(
            self.store, "code", {"demote": "F1", "reason": "lost"},
            "ctr9", gate_passed=set())
        self.assertEqual(self.store.get_route("code"), before)
        self.assertIn("no seat that passed the deciding gate", line)
        self.assertNotIn("holds no route position", line)
        rows = self.store.ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("no seat that passed the deciding gate",
                      rows[0]["note"])

    def test_displaced_seat_is_named_in_line_and_ledger(self):
        line = router.apply_lesson_route_change(
            self.store, "code",
            {"demote": "P", "promote": "XX", "reason": "lost"}, "ctr9")
        route = self.store.get_route("code")
        self.assertEqual(_order(route), ["XX", "P", "C", "F1", "F2"])
        self.assertIn("promoted XX", line)
        self.assertIn("C moved to the fallback order", line)
        rows = self.store.ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("C displaced to the fallback order",
                      rows[0]["note"])

    def test_legacy_noop_for_a_positioned_seat_is_truthful(self):
        # Demoting the fallback tail in legacy mode moves nothing;
        # the seat DOES hold a position, and the line says so.
        before = self.store.get_route("code")
        line = router.apply_lesson_route_change(
            self.store, "code", {"demote": "F2", "reason": "lost"},
            "ctr9")
        self.assertEqual(self.store.get_route("code"), before)
        self.assertIn("no route change", line)
        self.assertIn("holds a route position", line)
        self.assertNotIn("holds no route position", line)
        self.assertEqual(self.store.ledger_rows(), [])


if __name__ == "__main__":
    unittest.main()
