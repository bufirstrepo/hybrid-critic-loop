"""Regression tests for the state.py core of the tightening pass
(TIGHTENING-SPEC-2026-11-08): T2 ledger provenance, T3 sealing pass,
T7 add_ledger input discipline and transaction hygiene.

Each test names the review finding it pins (see
~/workspace/mm-review2-2026-11-08/: 1-security.md, 2-correctness.md,
3-host-integration.md, 4-claims.md).
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from hybrid_critic_loop import chain
from hybrid_critic_loop.state import Store


def _raw_store(tmp, rows, user_version=0, with_hash_columns=False):
    """Build a ledger-only database by hand, the way an old build (or a
    legacy writer beside the plugin) would have left it."""
    db_path = Path(tmp) / "data.db"
    hash_cols = ", prev_hash TEXT, row_hash TEXT" if with_hash_columns else ""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE ledger ("
            " id INTEGER PRIMARY KEY, target_id TEXT, contract_id TEXT,"
            " candidate_id TEXT, stage TEXT, expert_id TEXT,"
            " tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL,"
            f" outcome TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL"
            f"{hash_cols})")
        for i, (tin, tout, cost) in enumerate(rows):
            conn.execute(
                "INSERT INTO ledger (target_id, contract_id, candidate_id,"
                " stage, expert_id, tokens_in, tokens_out, cost_usd,"
                " outcome, note, created_at) VALUES ('tgt_0001',"
                " 'ctr_0001', 'cand_1', 'solve', 'exp-1', ?, ?, ?,"
                " 'completed', NULL, '2026-01-01T00:00:00+00:00')",
                (tin, tout, cost))
        conn.execute(f"PRAGMA user_version = {user_version}")
        conn.commit()
    finally:
        conn.close()
    return db_path


def _append(store, **overrides):
    args = dict(
        target_id="tgt_0001", contract_id="ctr_0001",
        candidate_id="cand_1", stage="solve", expert_id="exp-1",
        tokens_in=100, tokens_out=50, cost_usd=1.25,
        outcome="completed", note=None)
    args.update(overrides)
    return store.add_ledger(**args)


class TestProvenanceMigration(unittest.TestCase):
    """T2 (security F2): the migration adds provenance/sealed_via and
    pre-migration rows are labelled legacy-import, never silently
    treated as attributed."""

    def test_pre_migration_rows_backfill_legacy_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _raw_store(tmp, [(10, 5, 0.5), (20, 5, 0.25)])
            store = Store(db_path)
            try:
                rows = store.ledger_rows()
                self.assertEqual(
                    [r["provenance"] for r in rows],
                    ["legacy-import", "legacy-import"])
                # Sealed by the pre-chain backfill route.
                self.assertEqual(
                    [r["sealed_via"] for r in rows],
                    ["backfill", "backfill"])
            finally:
                store.close()

    def test_add_ledger_provenance_defaults_and_explicit(self):
        store = Store(":memory:")
        try:
            _append(store)
            _append(store, provenance="executor-reported")
            rows = store.ledger_rows()
            self.assertEqual(rows[0]["provenance"], "unattributed")
            self.assertEqual(rows[1]["provenance"], "executor-reported")
            self.assertEqual(
                [r["sealed_via"] for r in rows], ["append", "append"])
        finally:
            store.close()


class TestSealingPassBackfill(unittest.TestCase):
    """T3 (security F1/F4, correctness F9): pre-chain stores seal on
    open with hashes recomputed, never trusted."""

    def test_pre_chain_store_seals_all_and_sets_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _raw_store(tmp, [(10, 5, 0.5), (20, 5, 0.25)])
            store = Store(db_path)
            try:
                self.assertEqual(store.chain_backfilled, 2)
                self.assertEqual(store.chain_repairs, 0)
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"chain broke at {bad}")
            finally:
                store.close()
            conn = sqlite3.connect(str(db_path))
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
            self.assertGreaterEqual(version, 1)

    def test_trusted_hash_that_does_not_recompute_is_corruption(self):
        # A pre-chain store where an early row already carries a hash
        # that does not recompute from its fields (someone rewrote the
        # row and kept a seal). The old pass trusted the stored hash as
        # the link for everything after it; the tightening pass must
        # seal nothing and leave the book as found (security F4).
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _raw_store(
                tmp, [(10, 5, 0.5), (20, 5, 0.25)],
                with_hash_columns=True)
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "UPDATE ledger SET prev_hash=?, row_hash=?"
                    " WHERE id=1",
                    (chain.GENESIS_HASH, "f" * 64))
                conn.commit()
            finally:
                conn.close()
            store = Store(db_path)
            try:
                self.assertEqual(store.chain_backfilled, 0)
                self.assertEqual(store.chain_repairs, 0)
                rows = store.ledger_rows()
                self.assertEqual(rows[0]["row_hash"], "f" * 64)
                self.assertIsNone(rows[1]["row_hash"])
                ok, bad = chain.verify_chain(store)
                self.assertFalse(ok)
                self.assertEqual(bad, 1)
            finally:
                store.close()


class TestSealingPassRepair(unittest.TestCase):
    """T3 (correctness F1): a flagged store that gains unsealed rows
    (a pre-chain writer appending beside the plugin) gets its suffix
    repaired on open instead of a permanently broken chain."""

    def _flagged_store_with_unsealed_suffix(self, tmp, suffix_rows=2):
        db_path = Path(tmp) / "data.db"
        store = Store(db_path)
        _append(store)
        _append(store)
        store.close()
        conn = sqlite3.connect(str(db_path))
        try:
            for i in range(suffix_rows):
                conn.execute(
                    "INSERT INTO ledger (target_id, contract_id,"
                    " candidate_id, stage, expert_id, tokens_in,"
                    " tokens_out, cost_usd, outcome, note, created_at)"
                    " VALUES ('tgt_0001', 'ctr_0001', 'cand_x', 'solve',"
                    " 'exp-2', 7, 3, 0.10, 'completed', NULL,"
                    " '2026-01-02T00:00:00+00:00')")
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_unsealed_suffix_is_repaired_on_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._flagged_store_with_unsealed_suffix(tmp)
            store = Store(db_path)
            try:
                self.assertEqual(store.chain_repairs, 2)
                self.assertEqual(store.chain_backfilled, 0)
                rows = store.ledger_rows()
                self.assertEqual(
                    [r["sealed_via"] for r in rows],
                    ["append", "append", "repair", "repair"])
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"chain broke at {bad}")
                # And the book grows again from the repaired tail.
                _append(store, expert_id="exp-2")
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"chain broke after append at {bad}")
            finally:
                store.close()

    def test_middle_null_is_corruption_left_unsealed(self):
        # correctness F1 companion case: an unsealed row with a hashed
        # row after it is not a suffix — sealing it would rewrite
        # history's middle. Seal 1..3, null row 2's hashes by hand, and
        # the pass must refuse (row 3 is hashed after unsealed row 2);
        # verify_chain names row 2.
        with tempfile.TemporaryDirectory() as tmp:
            db_path2 = Path(tmp) / "mid.db"
            store = Store(db_path2)
            _append(store)
            _append(store)
            _append(store)
            store.close()
            conn = sqlite3.connect(str(db_path2))
            try:
                conn.execute(
                    "UPDATE ledger SET prev_hash=NULL, row_hash=NULL"
                    " WHERE id=2")
                conn.commit()
            finally:
                conn.close()
            store = Store(db_path2)
            try:
                self.assertEqual(store.chain_repairs, 0)
                self.assertEqual(store.chain_backfilled, 0)
                rows = store.ledger_rows()
                self.assertIsNone(rows[1]["row_hash"])
                ok, bad = chain.verify_chain(store)
                self.assertFalse(ok)
                self.assertEqual(bad, 2)
            finally:
                store.close()


class TestAddLedgerUnsealedPredecessor(unittest.TestCase):
    """T3 (correctness F1): add_ledger refuses to seal onto GENESIS
    over an unsealed predecessor."""

    def test_append_over_unsealed_tail_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data.db"
            store = Store(db_path)
            _append(store)
            store.close()
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "INSERT INTO ledger (target_id, contract_id,"
                    " candidate_id, stage, expert_id, tokens_in,"
                    " tokens_out, cost_usd, outcome, note, created_at)"
                    " VALUES ('tgt_0001', 'ctr_0001', 'cand_x', 'solve',"
                    " 'exp-2', 7, 3, 0.10, 'completed', NULL,"
                    " '2026-01-02T00:00:00+00:00')")
                # Also drop the meta flag's protection: pretend the
                # repair pass already ran by hand-nulling after open is
                # impossible; instead null the row while the store is
                # open below. Here: mark user_version already set (it
                # is) and reopen — the repair pass would seal it, so
                # instead simulate the corrupt-middle state where the
                # pass leaves the tail unsealed.
                conn.execute(
                    "UPDATE ledger SET prev_hash=NULL, row_hash=NULL"
                    " WHERE id=1")
                conn.commit()
            finally:
                conn.close()
            store = Store(db_path)
            try:
                # Row 1 unsealed with a hashed row after it = middle
                # corruption; the pass leaves the book as found. The
                # refusal case needs the LAST row unsealed: null it
                # directly against the open store's connection.
                store._conn.execute(
                    "UPDATE ledger SET prev_hash=NULL, row_hash=NULL"
                    " WHERE id=(SELECT MAX(id) FROM ledger)")
                store._conn.commit()
                with self.assertRaises(ValueError):
                    _append(store)
                # The failed append wrote nothing and left no open
                # transaction behind (correctness F7).
                self.assertFalse(store._conn.in_transaction)
                self.assertEqual(len(store.ledger_rows()), 2)
            finally:
                store.close()


class TestAddLedgerValidation(unittest.TestCase):
    """T7 (security F8, correctness F5): the API cannot write rows its
    own seal rejects — no negatives, no NaN, no string counts."""

    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_negative_values_rejected(self):
        for overrides in ({"tokens_in": -1}, {"tokens_out": -5},
                          {"cost_usd": -0.01}):
            with self.assertRaises(ValueError, msg=str(overrides)):
                _append(self.store, **overrides)
        self.assertEqual(self.store.ledger_rows(), [])

    def test_non_finite_and_wrong_types_rejected(self):
        bad = [
            {"tokens_in": float("nan")}, {"tokens_in": float("inf")},
            {"tokens_in": "100"}, {"tokens_in": True},
            {"tokens_in": 1.5},
            {"tokens_out": float("nan")}, {"tokens_out": "x"},
            {"cost_usd": float("nan")}, {"cost_usd": float("inf")},
            {"cost_usd": "1.25"}, {"cost_usd": True},
        ]
        for overrides in bad:
            with self.assertRaises(ValueError, msg=str(overrides)):
                _append(self.store, **overrides)
        self.assertEqual(self.store.ledger_rows(), [])

    def test_integral_floats_and_int_cost_accepted_and_normalized(self):
        _append(self.store, tokens_in=100.0, tokens_out=50.0, cost_usd=2)
        row = self.store.ledger_rows()[0]
        self.assertEqual(row["tokens_in"], 100)
        self.assertIsInstance(row["tokens_in"], int)
        self.assertEqual(row["tokens_out"], 50)
        self.assertEqual(row["cost_usd"], 2.0)
        self.assertIsInstance(row["cost_usd"], float)
        ok, bad_id = chain.verify_chain(self.store)
        self.assertTrue(ok, f"chain broke at {bad_id}")

    def test_none_values_still_allowed(self):
        _append(self.store, tokens_in=None, tokens_out=None, cost_usd=None)
        row = self.store.ledger_rows()[0]
        self.assertIsNone(row["tokens_in"])
        self.assertIsNone(row["cost_usd"])


class TestWriteTransactionHygiene(unittest.TestCase):
    """T7 (correctness F7): a failed _begin_write caller rolls back —
    its partial work is never committed by the next unrelated write."""

    def test_failed_create_contract_rolls_back(self):
        store = Store(":memory:")
        try:
            target_id = store.create_target("ask", "code")
            cid = store.create_contract(
                target_id, "solve", "task", "code", [], [],
                contract_id="ctr_fixed")
            with self.assertRaises(sqlite3.IntegrityError):
                store.create_contract(
                    target_id, "solve", "task", "code", [], [],
                    contract_id="ctr_fixed")
            self.assertFalse(store._conn.in_transaction)
            # The next unrelated write commits only its own work.
            store.create_target("ask2", "code")
            self.assertIsNotNone(store.get_contract(cid))
            rows = store._all("SELECT id FROM contracts")
            self.assertEqual([r["id"] for r in rows], ["ctr_fixed"])
        finally:
            store.close()

    def test_failed_add_ledger_leaves_no_open_transaction(self):
        store = Store(":memory:")
        try:
            _append(store)
            with self.assertRaises(ValueError):
                _append(store, outcome="bogus-outcome")
            self.assertFalse(store._conn.in_transaction)
            _append(store)
            self.assertEqual(len(store.ledger_rows()), 2)
        finally:
            store.close()


class TestVerdictFor(unittest.TestCase):
    """T4 support: verdict lookup by id for certificate verification."""

    def test_verdict_for_finds_named_verdict_only(self):
        store = Store(":memory:")
        try:
            target_id = store.create_target("ask", "code")
            cid = store.create_contract(
                target_id, "solve", "task", "code", [], [])
            vid = store.create_verdict(
                cid, 1, "cand_1", [], "pass", "pass", None, False,
                None, None, "pending", None)
            got = store.verdict_for(cid, vid)
            self.assertIsNotNone(got)
            self.assertEqual(got["id"], vid)
            self.assertEqual(got["winner"], "cand_1")
            self.assertIsNone(store.verdict_for(cid, "vrd_9999"))
            self.assertIsNone(store.verdict_for("ctr_other", vid))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
