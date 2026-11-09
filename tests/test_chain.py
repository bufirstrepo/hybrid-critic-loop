"""Unit tests for hybrid_critic_loop.chain — the ledger hash chain.

Covers: a fresh store's chain verifies from genesis; every appended row
carries both hashes linked to its predecessor; tampering with a stored
row breaks verification at exactly that row; a database created with the
pre-chain schema (no hash columns) is backfilled on first open and the
backfill meta flag (PRAGMA user_version) is set so it runs once.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from hybrid_critic_loop import chain
from hybrid_critic_loop.state import Store


class ChainTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "data.db"
        self.store = Store(self.db_path)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _row(self, expert="exp-1", tokens_in=100, tokens_out=50,
             cost_usd=1.25, outcome="completed"):
        return self.store.add_ledger(
            "tgt_0001", "ctr_0001", "cand_1", "solve", expert,
            tokens_in, tokens_out, cost_usd, outcome, "a note")


class TestChainFreshStore(ChainTestCase):
    def test_empty_store_verifies(self):
        ok, bad = chain.verify_chain(self.store)
        self.assertTrue(ok)
        self.assertIsNone(bad)

    def test_appends_are_sealed_and_linked(self):
        self._row()
        self._row(expert="exp-2")
        self._row(expert="exp-1")
        rows = self.store.ledger_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["prev_hash"], chain.GENESIS_HASH)
        for prev, cur in zip(rows, rows[1:]):
            self.assertEqual(cur["prev_hash"], prev["row_hash"])
        for row in rows:
            self.assertEqual(
                row["row_hash"], chain.row_hash(row["prev_hash"], row))
        ok, bad = chain.verify_chain(self.store)
        self.assertTrue(ok, f"chain broke at {bad}")

    def test_genesis_and_row_hash_shape(self):
        rid = self._row()
        row = self.store.ledger_rows()[0]
        self.assertEqual(row["id"], rid)
        self.assertEqual(len(row["row_hash"]), 64)
        int(row["row_hash"], 16)  # hex digest


class TestChainTamper(ChainTestCase):
    def test_tampered_tokens_break_at_that_row(self):
        self._row()
        rid = self._row(expert="exp-2")
        self._row(expert="exp-1")
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "UPDATE ledger SET tokens_in=999999 WHERE id=?", (rid,))
            conn.commit()
        finally:
            conn.close()
        ok, bad = chain.verify_chain(self.store)
        self.assertFalse(ok)
        self.assertEqual(bad, rid)

    def test_tampered_note_breaks_chain(self):
        rid = self._row()
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "UPDATE ledger SET note='rewritten' WHERE id=?", (rid,))
            conn.commit()
        finally:
            conn.close()
        ok, bad = chain.verify_chain(self.store)
        self.assertFalse(ok)
        self.assertEqual(bad, rid)

    def test_prefix_verification_stops_at_upto_id(self):
        # Tightening spec T4 (correctness F2, security F7): a
        # certificate verifies the chain prefix up to its high-water
        # mark, so tampering AFTER that mark does not fail the prefix —
        # and tampering INSIDE it still does.
        self._row()
        rid2 = self._row(expert="exp-2")
        rid3 = self._row(expert="exp-1")
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "UPDATE ledger SET tokens_in=999999 WHERE id=?", (rid3,))
            conn.commit()
        finally:
            conn.close()
        ok, bad = chain.verify_chain(self.store, upto_id=rid2)
        self.assertTrue(ok, f"prefix broke at {bad}")
        ok, bad = chain.verify_chain(self.store)
        self.assertFalse(ok)
        self.assertEqual(bad, rid3)
        ok, bad = chain.verify_chain(self.store, upto_id=rid3)
        self.assertFalse(ok)
        self.assertEqual(bad, rid3)


_OLD_LEDGER_SCHEMA = """
CREATE TABLE ledger (
    id INTEGER PRIMARY KEY,
    target_id TEXT,
    contract_id TEXT,
    candidate_id TEXT,
    stage TEXT,
    expert_id TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,
    outcome TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
"""


class TestChainBackfill(unittest.TestCase):
    def test_pre_chain_database_is_backfilled_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "old.db"
            conn = sqlite3.connect(str(db_path))
            try:
                conn.executescript(_OLD_LEDGER_SCHEMA)
                for i in range(3):
                    conn.execute(
                        "INSERT INTO ledger (target_id, contract_id,"
                        " candidate_id, stage, expert_id, tokens_in,"
                        " tokens_out, cost_usd, outcome, note, created_at)"
                        " VALUES ('tgt_0001','ctr_0001','cand_1','solve',"
                        " 'exp-1', 10, 5, 0.5, 'completed', NULL,"
                        " '2026-01-01T00:00:00+00:00')")
                conn.commit()
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(version, 0)
            finally:
                conn.close()

            store = Store(db_path)
            try:
                rows = store.ledger_rows()
                self.assertEqual(len(rows), 3)
                self.assertEqual(rows[0]["prev_hash"], chain.GENESIS_HASH)
                for row in rows:
                    self.assertTrue(row["row_hash"])
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"backfilled chain broke at {bad}")
                # New appends continue the backfilled chain.
                store.add_ledger(
                    "tgt_0001", "ctr_0001", "cand_2", "solve", "exp-2",
                    1, 1, None, "completed", None)
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"chain broke after append at {bad}")
            finally:
                store.close()

            conn = sqlite3.connect(str(db_path))
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
            self.assertGreaterEqual(version, 1)

            # Reopening does not re-run or disturb the sealed rows.
            store = Store(db_path)
            try:
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"chain broke on reopen at {bad}")
            finally:
                store.close()


class TestVerifiedHeadCache(ChainTestCase):
    """Round-5 fix spec H3 (pinned interfaces): a successful FULL
    walk records the chain-verified head cache in store meta; a
    failed walk leaves it untouched; the cached check agrees with
    the walk on a healthy book and extends the cache over new
    rows."""

    def test_successful_walk_populates_the_cache(self):
        self.assertIsNone(self.store.get_chain_verified_head())
        self._row()
        self._row(expert="exp-2")
        ok, _bad = chain.verify_chain(self.store)
        self.assertTrue(ok)
        head = self.store.get_chain_verified_head()
        self.assertIsNotNone(head)
        self.assertEqual(head[0], 2)
        self.assertEqual(
            head[1], self.store.ledger_rows()[-1]["row_hash"])
        self.assertEqual(head[2], 2)

    def test_failed_walk_does_not_refresh_the_cache(self):
        self._row()
        self._row(expert="exp-2")
        chain.verify_chain(self.store)
        cached = self.store.get_chain_verified_head()
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "UPDATE ledger SET note='tampered' WHERE id=1")
            conn.commit()
        finally:
            conn.close()
        ok, bad = chain.verify_chain(self.store)
        self.assertEqual((ok, bad), (False, 1))
        self.assertEqual(self.store.get_chain_verified_head(), cached)

    def test_cached_check_agrees_and_extends(self):
        self._row()
        chain.verify_chain(self.store)
        self._row(expert="exp-2")
        self.assertEqual(
            chain.verify_chain_cached(self.store), (True, None))
        self.assertEqual(self.store.get_chain_verified_head()[0], 2)


if __name__ == "__main__":
    unittest.main()
