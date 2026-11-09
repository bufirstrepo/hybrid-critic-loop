"""Regression tests for the round-8 fix pass — Unit 2 (store /
chain machinery). Spec: FIX-SPEC-ROUND8-2026-11-08.md L2 / L3.

Each test names its source finding (reports in
~/workspace/mm-review8-2026-11-08/):

- L2 (attack F2): round-7's K4 made the open-time backfill route
  require a book with no CURRENT-form seal. A prefix sealed
  entirely under a historical form (the legacy digest) set no
  current-form seal, so the condition passed vacuously: flag
  rollback + a raw append over a legacy-sealed prefix still
  sealed as "backfill" and the append counted via the legacy
  exemption (MILLIONAIRE — $2,000,020). The route now requires
  NO RECOGNISED SEAL OF ANY FORM in the validated prefix; only
  a genuinely seal-free book (pre-chain rows carry row_hash
  NULL throughout) still backfills.

  Counting nuance, pinned deliberately below: the exemption the
  route governs applies only to provenance-missing
  (legacy-import) rows. A suffix row that is fully backed by
  the ordinary referential leg (its candidate exists, on its
  own contract, with matching stored provenance) counts under
  ANY route — that is the counted rule's design since round 3,
  not the laundering class. So the count regression uses the
  exemption-shaped append (NULL provenance, counting SOLELY via
  the route), and a separate test pins the route flip on the
  parent's verbatim P2 shape (a referentially backed append),
  where the seal — not the count — is what L2 changes.

- L3 (attack F3): round-7's K7 cache distrust was a per-instance
  flag; a fresh Store on the same file trusted the stale cache
  the failed clear had left behind — and the hook the distrust
  protects is itself a fresh process per firing. The distrust is
  now a durable store_meta record: every instance's cached
  verification checks it before trusting the cache, and a
  successful clear, seed, or full-walk verification removes it.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from hybrid_critic_loop import chain, standing
from hybrid_critic_loop.state import Store


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "data.db"
        self.store = Store(self.db_path)

    def tearDown(self):
        try:
            self.store.close()
        finally:
            self._tmp.cleanup()

    def _raw(self, sql, params=()):
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def _meta(self, key):
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute(
                "SELECT value FROM store_meta WHERE key=?",
                (key,)).fetchone()
            return None if row is None else row[0]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# L2 — backfill requires a book free of ANY recognised seal (attack F2)
# ---------------------------------------------------------------------------

class TestBackfillRequiresAnyFormSealFreeBook(_StoreCase):
    """The parent's P2 construction (muse-round8-verify.py): a
    genuine $20 row, its seal rewritten to the LEGACY canonical
    form (an all-historical prefix), PRAGMA user_version=0, then a
    raw $2M append. At 5f30ea5 the append sealed as "backfill" —
    K4's condition saw no CURRENT-form seal and passed vacuously.
    Now the legacy seal is a recognised seal: it disqualifies the
    route, the append seals as "repair", and — in the
    exemption-shaped variant — counts nothing."""

    def _legacy_prefix_book(self, append_provenance):
        """Build the P2 book and reopen it; returns the reopened
        store. ``append_provenance`` is the raw append's provenance
        value (None for the exemption-shaped append, "measured"
        for the parent's verbatim referentially backed one)."""
        self.store.upsert_expert("e1", "fam", "cli", "api")
        self.store.create_contract(
            target_id="t", stage="s", task="x", domain="d", tags=[],
            acceptance=[], depends_on=[], contract_id="c1")
        self.store.create_candidate(
            "c1", 1, "gen", "e1", "fam", "a", "/tmp", "bh")
        self.store.update_candidate("c1_a", provenance="measured")
        self.store.add_ledger(
            "t", "c1", "c1_a", "gen", "e1", 10, 10, 20.0,
            "completed", provenance="measured")
        self.store.close()
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = dict(conn.execute("SELECT * FROM ledger").fetchone())
            legacy = chain.row_hash_variants(row)["legacy"]
            conn.execute(
                "UPDATE ledger SET row_hash=?, sealed_via='legacy'"
                " WHERE id=?", (legacy, row["id"]))
            conn.execute("PRAGMA user_version=0")
            conn.execute(
                "INSERT INTO ledger (target_id, contract_id,"
                " candidate_id, stage, expert_id, tokens_in,"
                " tokens_out, cost_usd, outcome, provenance,"
                " created_at) VALUES ('t', 'c1', 'c1_a', 'gen',"
                " 'ghost', 0, 0, 2000000.0, 'completed', ?,"
                " '2026-11-08T00:00:00+00:00')",
                (append_provenance,))
            conn.commit()
        finally:
            conn.close()
        self.store = Store(self.db_path)
        return self.store

    def test_legacy_prefix_append_seals_repair_and_counts_nothing(self):
        store = self._legacy_prefix_book(append_provenance=None)
        # The rollback did NOT re-arm the migration route: the
        # legacy-form seal in the prefix disqualifies it.
        self.assertEqual(store.chain_backfilled, 0)
        self.assertEqual(store.chain_repairs, 1)
        rows = store.ledger_rows()
        # The historical prefix row cascades to "upgrade" and keeps
        # counting, exactly as a genuine migration's rows do.
        self.assertEqual(rows[0]["sealed_via"], "upgrade")
        self.assertEqual(rows[0]["cost_usd"], 20.0)
        # The append seals as "repair" — the open-time pass labels
        # its missing provenance the way it labels every raw row.
        self.assertEqual(rows[1]["sealed_via"], "repair")
        self.assertEqual(rows[1]["provenance"], "legacy-import")
        ok, bad = chain.verify_chain(store)
        self.assertTrue(ok, f"chain broke at {bad}")
        # The $2M counted SOLELY via the backfill exemption; with
        # the route gone it falls to the ordinary referential leg
        # (a legacy-import row's provenance matches nothing the
        # candidate graph stores) and is excluded, named.
        totals = standing.ledger_totals(store)
        self.assertEqual(totals["cost_usd"], 20.0)
        self.assertEqual(totals["excluded_referential_rows"], 1)
        self.assertEqual(
            totals["excluded_referential_cost_usd"], 2000000.0)
        line = standing.standing_line(store)
        self.assertIn("CLIENT", line)
        self.assertNotIn("MILLIONAIRE", line)
        # The version flag still advances, as before.
        conn = sqlite3.connect(str(self.db_path))
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(version, 1)

    def test_legacy_prefix_route_flips_for_a_backed_append_too(self):
        """The parent's verbatim P2 append (provenance "measured",
        the real candidate with aligned stored provenance): L2
        flips its SEAL to "repair" — the route is the defect. Its
        counting is NOT the exemption class: the referential leg
        backs it outright (candidate exists, own contract,
        provenance matches), so it counts under any route, by the
        counted rule's design. Pinned so the boundary is a choice
        on the record, not an oversight."""
        store = self._legacy_prefix_book(append_provenance="measured")
        self.assertEqual(store.chain_backfilled, 0)
        rows = store.ledger_rows()
        self.assertEqual(rows[1]["sealed_via"], "repair")
        ok, bad = chain.verify_chain(store)
        self.assertTrue(ok, f"chain broke at {bad}")
        totals = standing.ledger_totals(store)
        self.assertEqual(totals["cost_usd"], 2000020.0)


# ---------------------------------------------------------------------------
# L3 — cache distrust is durable across instances (attack F3)
# ---------------------------------------------------------------------------

class TestCacheDistrustIsDurable(_StoreCase):
    """K7's marker lived on the instance; the hook — a fresh
    process per firing — never saw it. A failed clear now leaves
    a store_meta record every instance honours, and a successful
    clear, seed, or full-walk verification removes it."""

    UNAVAILABLE = (
        "Millionaire Matrix standing unavailable — "
        "ledger chain broken at row 2"
    )

    def _book_with_cache(self):
        cand = self.store.create_candidate(
            "ctr-1", 1, "implement", "exp-1", "fam", "cand", None, None)
        for cost in (100.0, 200.0, 300.0):
            self.store.add_ledger(
                "tgt-1", "ctr-1", cand, "implement", "exp-1",
                100, 50, cost, "completed", None,
                provenance="executor-reported")
        self.assertEqual(chain.verify_chain(self.store), (True, None))
        self.assertIsNotNone(self.store.get_chain_verified_head())

    def _sabotage_clear(self, store):
        def sabotaged_clear():
            raise RuntimeError("meta delete blocked (test double)")
        store.clear_chain_verified_head = sabotaged_clear
        return sabotaged_clear

    def test_fresh_instance_honours_a_failed_clear(self):
        self._book_with_cache()
        self._raw("UPDATE ledger SET cost_usd=900000.0 WHERE id=2")
        self._sabotage_clear(self.store)
        try:
            # The full walk proves the break; instance A's clear
            # fails — the distrust is now a durable record, and the
            # stale cache is still physically there.
            self.assertEqual(chain.verify_chain(self.store), (False, 2))
            self.assertTrue(self.store._chain_cache_untrusted)
            self.assertIsNotNone(self._meta("chain_cache_untrusted"))
            self.assertIsNotNone(self.store.get_chain_verified_head())
        finally:
            del self.store.clear_chain_verified_head

        # A FRESH instance on the same file: its own flag starts
        # unset (K7's whole defect), but the durable record makes
        # its cached check fall back to the full walk, and its
        # hook line renders the honest line — never the doctored
        # BALLER tier the surviving cache would print.
        fresh = Store(self.db_path)
        try:
            self.assertFalse(fresh._chain_cache_untrusted)
            self.assertTrue(fresh.chain_cache_untrusted())
            self.assertEqual(chain.verify_chain_cached(fresh), (False, 2))
            self.assertEqual(fresh._mm_last_chain_check, ("full", None))
            line = standing.standing_line(fresh, cached=True)
            self.assertEqual(line, self.UNAVAILABLE)
            self.assertNotIn("BALLER", line)
            # The fresh instance's own (working) clear finally
            # landed during that walk: cache AND distrust record
            # are both gone now — the record lives exactly as long
            # as the stale cache it guards against.
            self.assertIsNone(fresh.get_chain_verified_head())
            self.assertIsNone(self._meta("chain_cache_untrusted"))
        finally:
            fresh.close()

    def test_successful_walk_removes_the_durable_record(self):
        self._book_with_cache()
        self._raw("UPDATE ledger SET cost_usd=900000.0 WHERE id=2")
        self._sabotage_clear(self.store)
        try:
            self.assertEqual(chain.verify_chain(self.store), (False, 2))
            self.assertIsNotNone(self._meta("chain_cache_untrusted"))
        finally:
            del self.store.clear_chain_verified_head
        # Heal the book; a fresh instance's successful walk re-seeds
        # the cache and removes the durable distrust.
        self._raw("UPDATE ledger SET cost_usd=200.0 WHERE id=2")
        healed = Store(self.db_path)
        try:
            self.assertEqual(chain.verify_chain(healed), (True, None))
            self.assertIsNone(self._meta("chain_cache_untrusted"))
            self.assertFalse(healed.chain_cache_untrusted())
        finally:
            healed.close()
        # And the NEXT fresh instance trusts the cache again: the
        # cached check answers from the cache, not a full walk.
        trusting = Store(self.db_path)
        try:
            self.assertFalse(trusting.chain_cache_untrusted())
            self.assertEqual(
                chain.verify_chain_cached(trusting), (True, None))
            self.assertEqual(trusting._mm_last_chain_check[0], "cached")
        finally:
            trusting.close()


if __name__ == "__main__":
    unittest.main()
