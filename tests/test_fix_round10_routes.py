"""Round-10 fix pass — Unit N3+N4 (state/router).

FIX-SPEC-ROUND10-2026-11-09:

* N3 (lane-6 latent perf): ``candidates_for(contract_id, attempt)``
  full-scanned the ``candidates`` table — no index on
  ``contract_id``. The store now creates
  ``idx_candidates_contract_attempt`` in the same schema script
  that fresh and pre-existing books run on open, so both gain it.
* N4 (lane-4 F-R10-1): on a legacy route whose ``primary ==
  compete_with`` (a shape ``upsert_route`` can no longer write, so
  the seed here is raw SQL), demoting the primary repaired the
  state correctly but the router's line claimed a fallback landing
  for a demotee who never moved. The line — and the ledger note —
  now name the repair: the duplicate compete seat was vacated and
  the demotee remains primary. Sibling shapes are pinned too: an
  unseated demotee on the same corrupted seed, and a fallback
  demotee whose move is real but whose book also had the duplicate
  vacated behind it.
"""

import os
import sqlite3
import tempfile
import unittest

from hybrid_critic_loop import router
from hybrid_critic_loop.state import Store, _dumps


def _seed_corrupt(store, category="code"):
    """Raw-seed the legacy corrupted shape: primary == compete_with.

    Only raw SQL can write it — ``upsert_route`` refuses it since
    the round-9 M4 fix — which is exactly why the lane-4 repro
    seeded it this way.
    """
    store._conn.execute(
        'INSERT INTO routes (category, "primary", compete_with,'
        ' fallback_json, judges_json, "rule", panel_json, scoring,'
        ' updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
        (category, "E", "E", _dumps(["A", "F", "D"]), _dumps([]),
         "legacy-book", None, None, "2026-01-01T00:00:00+00:00"))
    store._conn.commit()


class TestCandidatesIndexN3(unittest.TestCase):
    def test_index_exists_on_fresh_store(self):
        store = Store(":memory:")
        try:
            rows = store._conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='index'"
                " AND name='idx_candidates_contract_attempt'").fetchall()
            self.assertEqual(len(rows), 1)
            plan = store._conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM candidates"
                " WHERE contract_id=? AND attempt=?",
                ("ctr_1", 2)).fetchall()
            detail = " ".join(str(tuple(row)) for row in plan)
            self.assertTrue(
                "idx_candidates_contract_attempt" in detail,
                f"candidates_for query does not use the index: {detail}")
        finally:
            store.close()

    def test_index_added_when_reopening_pre_index_book(self):
        # A book whose schema predates the index: open it fresh,
        # drop the index, close, reopen — the index is back.
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = Store(path)
            store.create_candidate("ctr_1", 1, "code", "exp-a",
                                   "fam", "s1", None, None)
            store.create_candidate("ctr_1", 2, "code", "exp-b",
                                   "fam", "s2", None, None)
            store._conn.execute(
                "DROP INDEX IF EXISTS idx_candidates_contract_attempt")
            store._conn.commit()
            store.close()

            store = Store(path)
            try:
                rows = store._conn.execute(
                    "SELECT name FROM sqlite_master"
                    " WHERE type='index'"
                    " AND name='idx_candidates_contract_attempt'"
                    ).fetchall()
                self.assertEqual(len(rows), 1)
                # Results unchanged by the index.
                self.assertEqual(
                    [c["expert_id"] for c in
                     store.candidates_for("ctr_1", attempt=2)],
                    ["exp-b"])
                self.assertEqual(
                    [c["expert_id"] for c in
                     store.candidates_for("ctr_1")],
                    ["exp-a", "exp-b"])
            finally:
                store.close()
        finally:
            os.unlink(path)


class TestCorruptRouteLineN4(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        _seed_corrupt(self.store)

    def tearDown(self):
        self.store.close()

    def test_demoting_the_duplicated_primary_names_the_repair(self):
        # Lane-4 F-R10-1, verbatim shape.
        line = router.apply_lesson_route_change(
            self.store, "code", {"demote": "E", "reason": "lost on gate"},
            "ctr_1")
        route = self.store.get_route("code")
        # State: unchanged from the pre-fix behaviour — the duplicate
        # compete seat is vacated, the demotee stays primary.
        self.assertEqual(route["primary"], "E")
        self.assertIsNone(route["compete_with"])
        self.assertEqual(route["fallback"], ["A", "F", "D"])
        # Line: no false fallback landing; the repair is named.
        self.assertNotIn("within the fallback order", line)
        self.assertIn("duplicate compete seat was vacated", line)
        self.assertIn("remains primary", line)
        self.assertIn("held the primary and compete seats", line)
        # The ledger note carries the same repair, so the spoken
        # record and the stored route no longer disagree.
        notes = [row["note"] for row in self.store.ledger_rows()
                 if row["expert_id"] == "E"]
        self.assertTrue(
            any("duplicate compete seat vacated" in note for note in notes),
            f"ledger note does not name the repair: {notes}")

    def test_unseated_demotee_on_corrupt_seed_names_the_repair(self):
        # Sibling shape: the demotee holds no route position, so the
        # ONLY change is the guard vacating the duplicate — the old
        # template still claimed a fallback landing for it.
        line = router.apply_lesson_route_change(
            self.store, "code", {"demote": "X", "reason": "lost"}, "c1")
        route = self.store.get_route("code")
        self.assertEqual(route["primary"], "E")
        self.assertIsNone(route["compete_with"])
        self.assertEqual(route["fallback"], ["A", "F", "D"])
        self.assertNotIn("within the fallback order", line)
        self.assertIn("holds no route position", line)
        self.assertIn("duplicate compete seat was vacated", line)

    def test_fallback_demotee_on_corrupt_seed_names_both_moves(self):
        # Sibling shape: the fallback demotee's move is real (A goes
        # to the end of the fallback) AND the duplicate is vacated
        # behind it — the line names both, not just the fallback.
        line = router.apply_lesson_route_change(
            self.store, "code", {"demote": "A", "reason": "lost"}, "c2")
        route = self.store.get_route("code")
        self.assertEqual(route["primary"], "E")
        self.assertIsNone(route["compete_with"])
        self.assertEqual(route["fallback"], ["F", "D", "A"])
        self.assertIn("within the fallback order", line)
        self.assertIn("duplicate compete seat vacated", line)

    def test_well_formed_routes_unchanged(self):
        # The repair branches fire only on the corrupted shape: an
        # ordinary primary demotion and an ordinary fallback demotion
        # keep their pre-fix lines.
        store = Store(":memory:")
        try:
            store.upsert_route("code", "P", "C", ["F1", "F2"], [], "r")
            line = router.apply_lesson_route_change(
                store, "code", {"demote": "P", "reason": "lost"}, "c4")
            self.assertIn("promoted C", line)
            self.assertNotIn("duplicate compete seat", line)
            line = router.apply_lesson_route_change(
                store, "code", {"demote": "F1", "reason": "lost"}, "c5")
            self.assertIn("within the fallback order", line)
            self.assertNotIn("duplicate compete seat", line)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
