"""Regression tests for the state.py half of the round-4 fix pass
(FIX-SPEC-ROUND4-2026-11-08): G2 sealing commits / upgrade marker /
counter accumulation / close() / lock courtesy, G3 open fast path /
migration gating / hot-path indexes, G1's store claim + payload-hash
setter, G8's demotion honesty.

Each test names the round + finding it pins (see
~/workspace/mm-review3-2026-11-08/ and
~/workspace/mm-review4-2026-11-08/).
"""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hybrid_critic_loop import chain
from hybrid_critic_loop.state import Store

# Canonical field sets of the historical forms (fix-spec pinned
# variants): "pre_sealed_via" is the current form minus sealed_via;
# "legacy" also lacks provenance. Derived from chain.LEDGER_FIELDS so
# the fixtures track the pin.
_FIELDS_PRE_SEALED_VIA = tuple(
    f for f in chain.LEDGER_FIELDS if f != "sealed_via")
_FIELDS_LEGACY = tuple(
    f for f in _FIELDS_PRE_SEALED_VIA if f != "provenance")


def _digest(fields, prev_hash, row):
    """sha256(prev || canonical(row)) over an explicit field set —
    the test-local mirror of chain.canonical_row for fixture
    building (historical seals must be built without trusting the
    code under test to build them)."""
    body = {f: row.get(f) for f in fields}
    data = (str(prev_hash) + json.dumps(
        body, sort_keys=True, separators=(",", ":"))).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _append(store, **overrides):
    args = dict(
        target_id="tgt_0001", contract_id="ctr_0001",
        candidate_id="cand_1", stage="solve", expert_id="exp-1",
        tokens_in=100, tokens_out=50, cost_usd=1.25,
        outcome="completed", note=None)
    args.update(overrides)
    return store.add_ledger(**args)


def _sealed_store(tmp, n=3):
    db_path = Path(tmp) / "data.db"
    store = Store(db_path)
    for _ in range(n):
        _append(store)
    store.close()
    return db_path


def _rewrite_chain_form(db_path, fields):
    """Re-seal every row of an existing book under a historical
    canonical form, the way the build of that era would have left
    it."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM ledger ORDER BY id ASC").fetchall()
        prev = chain.GENESIS_HASH
        for row in rows:
            digest = _digest(fields, prev, dict(row))
            conn.execute(
                "UPDATE ledger SET prev_hash=?, row_hash=? WHERE id=?",
                (prev, digest, row["id"]))
            prev = digest
        conn.commit()
    finally:
        conn.close()


def _raw_hashes(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT prev_hash, row_hash FROM ledger ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()


def _raw_legacy_store(tmp, n=3):
    """A pre-T12 book built by hand: no provenance / sealed_via
    columns at all, hashes under the legacy form, chain flag set."""
    db_path = Path(tmp) / "data.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE ledger ("
            " id INTEGER PRIMARY KEY, target_id TEXT, contract_id TEXT,"
            " candidate_id TEXT, stage TEXT, expert_id TEXT,"
            " tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL,"
            " outcome TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL,"
            " prev_hash TEXT, row_hash TEXT)")
        prev = chain.GENESIS_HASH
        for i in range(1, n + 1):
            row = {
                "id": i, "target_id": "tgt_0001",
                "contract_id": "ctr_0001", "candidate_id": "cand_1",
                "stage": "solve", "expert_id": "exp-1",
                "tokens_in": 10 * i, "tokens_out": 5, "cost_usd": 0.5,
                "outcome": "completed", "note": None,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
            digest = _digest(_FIELDS_LEGACY, prev, row)
            conn.execute(
                "INSERT INTO ledger (id, target_id, contract_id,"
                " candidate_id, stage, expert_id, tokens_in,"
                " tokens_out, cost_usd, outcome, note, created_at,"
                " prev_hash, row_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?)",
                (row["id"], row["target_id"], row["contract_id"],
                 row["candidate_id"], row["stage"], row["expert_id"],
                 row["tokens_in"], row["tokens_out"], row["cost_usd"],
                 row["outcome"], row["note"], row["created_at"],
                 prev, digest))
            prev = digest
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()
    return db_path


class TestUpgradeCascadePersists(unittest.TestCase):
    """G2 (round-3 F1; round-4 upgrade F1/F2): the canonical-form
    cascade must COMMIT on the open that performs it. The old pass
    left its UPDATEs in an open transaction — the upgrade re-ran on
    every open, never landed on disk, and the dangling write
    transaction locked other processes out."""

    def test_pre_sealed_via_cascade_persists_across_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _sealed_store(tmp, 3)
            _rewrite_chain_form(db_path, _FIELDS_PRE_SEALED_VIA)
            store = Store(db_path)
            try:
                self.assertEqual(store.chain_upgrades, 3)
                self.assertEqual(store.chain_repairs, 0)
                self.assertEqual(store.chain_backfilled, 0)
                rows = store.ledger_rows()
                self.assertEqual(
                    [r["sealed_via"] for r in rows], ["upgrade"] * 3)
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"chain broke at {bad}")
            finally:
                store.close()
            disk_hashes = _raw_hashes(db_path)
            # Second open: the upgrade is ON DISK — nothing re-seals,
            # and the hashes read cold are the ones the first open
            # wrote.
            store = Store(db_path)
            try:
                self.assertEqual(store.chain_upgrades, 0)
                self.assertEqual(store.chain_repairs, 0)
                self.assertEqual(store.chain_backfilled, 0)
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"chain broke at {bad}")
            finally:
                store.close()
            self.assertEqual(_raw_hashes(db_path), disk_hashes)

    def test_legacy_form_cascade_persists_across_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _raw_legacy_store(tmp, 3)
            store = Store(db_path)
            try:
                self.assertEqual(store.chain_upgrades, 3)
                rows = store.ledger_rows()
                self.assertEqual(
                    [r["sealed_via"] for r in rows], ["upgrade"] * 3)
                self.assertEqual(
                    [r["provenance"] for r in rows],
                    ["legacy-import"] * 3)
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"chain broke at {bad}")
            finally:
                store.close()
            disk_hashes = _raw_hashes(db_path)
            store = Store(db_path)
            try:
                self.assertEqual(store.chain_upgrades, 0)
                self.assertEqual(store.chain_repairs, 0)
                self.assertEqual(store.chain_backfilled, 0)
            finally:
                store.close()
            self.assertEqual(_raw_hashes(db_path), disk_hashes)

    def test_cascade_and_suffix_counters_accumulate(self):
        # One open doing both kinds of work: a historical-form prefix
        # (cascade) and an unsealed suffix (repair). The old code
        # OVERWROTE chain_repairs with the suffix count, losing the
        # cascade's rows from the operator's counters entirely.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _sealed_store(tmp, 2)
            _rewrite_chain_form(db_path, _FIELDS_LEGACY)
            conn = sqlite3.connect(str(db_path))
            try:
                for _ in range(2):
                    conn.execute(
                        "INSERT INTO ledger (target_id, contract_id,"
                        " candidate_id, stage, expert_id, tokens_in,"
                        " tokens_out, cost_usd, outcome, note,"
                        " created_at) VALUES ('tgt_0001', 'ctr_0001',"
                        " 'cand_x', 'solve', 'exp-2', 7, 3, 0.10,"
                        " 'completed', NULL,"
                        " '2026-01-02T00:00:00+00:00')")
                conn.commit()
            finally:
                conn.close()
            store = Store(db_path)
            try:
                self.assertEqual(store.chain_upgrades, 2)
                self.assertEqual(store.chain_repairs, 2)
                self.assertEqual(store.chain_backfilled, 0)
                self.assertEqual(
                    [r["sealed_via"] for r in store.ledger_rows()],
                    ["upgrade", "upgrade", "repair", "repair"])
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"chain broke at {bad}")
            finally:
                store.close()


class TestCloseCommitsResidual(unittest.TestCase):
    """G2 (round-3 F1): close() commits a residual sealing
    transaction instead of letting it die with the connection."""

    def test_close_commits_open_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data.db"
            store = Store(db_path)
            _append(store)
            store._conn.execute(
                "UPDATE ledger SET note='residual' WHERE id=1")
            self.assertTrue(store._conn.in_transaction)
            store.close()
            conn = sqlite3.connect(str(db_path))
            try:
                note = conn.execute(
                    "SELECT note FROM ledger WHERE id=1").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(note, "residual")


class TestLockedOpenMessage(unittest.TestCase):
    """G2 (round-4 upgrade F1): a locked database at open names the
    likely holder instead of sqlite's bare "database is locked"."""

    def test_locked_open_names_the_likely_holder(self):
        with tempfile.TemporaryDirectory() as tmp:
            # The victim book is a legacy-form store: opening it must
            # WRITE (column migration + the upgrade cascade), so the
            # holder's write lock genuinely blocks it. (A healthy
            # current-form book now opens read-only and would not —
            # and should not — notice the lock at all.)
            db_path = _raw_legacy_store(tmp, 2)
            holder = sqlite3.connect(str(db_path))
            try:
                holder.execute("BEGIN IMMEDIATE")
                holder.execute(
                    "INSERT INTO ledger (id, target_id, contract_id,"
                    " candidate_id, stage, expert_id, tokens_in,"
                    " tokens_out, cost_usd, outcome, note, created_at,"
                    " prev_hash, row_hash) VALUES (99, 't', 'c', 'k',"
                    " 'solve', 'exp-1', 1, 1, 0.1, 'completed', NULL,"
                    " '2026-01-01T00:00:00+00:00', NULL, NULL)")
                real_connect = sqlite3.connect

                def quick_connect(*args, **kwargs):
                    kwargs["timeout"] = 0.5
                    return real_connect(*args, **kwargs)

                with mock.patch.object(sqlite3, "connect",
                                       quick_connect):
                    with self.assertRaises(
                            sqlite3.OperationalError) as ctx:
                        Store(db_path)
                message = str(ctx.exception)
                self.assertIn("locked", message.lower())
                self.assertIn("another process", message)
            finally:
                holder.rollback()
                holder.close()


class TestOpenFastPath(unittest.TestCase):
    """G3 (round-4 performance P1/P5/P8): a healthy current-form
    store opens without the full hash walk, without writes, and with
    the hot-path indexes present."""

    def test_healthy_open_performs_no_full_hash_walk(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _sealed_store(tmp, 25)
            counts = {"variants": 0, "hash": 0, "legacy": 0}
            orig_variants = chain.row_hash_variants
            orig_hash = chain.row_hash
            orig_legacy = chain.row_hash_legacy

            def counting_variants(row):
                counts["variants"] += 1
                return orig_variants(row)

            def counting_hash(*args, **kwargs):
                counts["hash"] += 1
                return orig_hash(*args, **kwargs)

            def counting_legacy(*args, **kwargs):
                counts["legacy"] += 1
                return orig_legacy(*args, **kwargs)

            with mock.patch.object(chain, "row_hash_variants",
                                   counting_variants), \
                    mock.patch.object(chain, "row_hash",
                                      counting_hash), \
                    mock.patch.object(chain, "row_hash_legacy",
                                      counting_legacy):
                store = Store(db_path)
            try:
                self.assertEqual(store.chain_backfilled, 0)
                self.assertEqual(store.chain_repairs, 0)
                self.assertEqual(store.chain_upgrades, 0)
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"chain broke at {bad}")
            finally:
                store.close()
            # Structural assertion, not timing: a 25-row book was
            # probed at its tail AND its head (round-5 fix spec H6:
            # two variants calls — the tail-only probe stranded
            # mixed-form books) — a full walk would have cost one
            # call per row.
            self.assertEqual(counts["variants"], 2)
            # Two probe points × up to three forms each — a walk
            # over 25 rows would be 25 variants calls, ~75 digests.
            self.assertLessEqual(counts["hash"] + counts["legacy"], 6)

    def test_healthy_open_issues_no_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _sealed_store(tmp, 5)

            class TracedConnection(sqlite3.Connection):
                statements = []

                def execute(self, sql, parameters=()):
                    type(self).statements.append(str(sql))
                    return super().execute(sql, parameters)

                def executescript(self, script):
                    type(self).statements.append(str(script))
                    return super().executescript(script)

            TracedConnection.statements = []
            real_connect = sqlite3.connect

            def traced_connect(*args, **kwargs):
                kwargs["factory"] = TracedConnection
                return real_connect(*args, **kwargs)

            with mock.patch.object(sqlite3, "connect", traced_connect):
                store = Store(db_path)
            store.close()
            writes = [
                s for s in TracedConnection.statements
                if s.lstrip().upper().startswith(
                    ("UPDATE", "INSERT", "DELETE", "REPLACE"))]
            self.assertEqual(writes, [])

    def test_hot_path_indexes_exist(self):
        store = Store(":memory:")
        try:
            ledger_idx = {
                r[1] for r in store._all("PRAGMA index_list(ledger)")}
            verdict_idx = {
                r[1] for r in store._all("PRAGMA index_list(verdicts)")}
        finally:
            store.close()
        self.assertIn("idx_ledger_contract_id", ledger_idx)
        self.assertIn("idx_ledger_expert_id", ledger_idx)
        self.assertIn("idx_verdicts_contract_id", verdict_idx)

    def test_preexisting_database_gains_indexes_on_open(self):
        # A database created before the indexes existed (a bare
        # ledger table, nothing else) gains them through the schema
        # script on open — the package's existing migration style.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _raw_legacy_store(tmp, 1)
            store = Store(db_path)
            try:
                ledger_idx = {
                    r[1]
                    for r in store._all("PRAGMA index_list(ledger)")}
            finally:
                store.close()
            self.assertIn("idx_ledger_contract_id", ledger_idx)
            self.assertIn("idx_ledger_expert_id", ledger_idx)


class TestClaimHumanGate(unittest.TestCase):
    """G1 pinned interface (round-4 security F4, contest-core F2):
    claim_human_gate is one conditional UPDATE, committed — exactly
    one caller, in this process or any other, records the decision.
    """

    def _pending_verdict(self, store):
        target_id = store.create_target("ask", "code")
        cid = store.create_contract(
            target_id, "solve", "task", "code", [], [])
        vid = store.create_verdict(
            cid, 1, "cand_1", [], "pass", "pass", None, False,
            None, None, "pending", None)
        return cid, vid

    def test_claim_succeeds_exactly_once(self):
        store = Store(":memory:")
        try:
            cid, vid = self._pending_verdict(store)
            self.assertTrue(store.claim_human_gate(vid, "approved"))
            self.assertEqual(
                store.latest_verdict(cid)["human_gate"], "approved")
            self.assertFalse(store.claim_human_gate(vid, "approved"))
            self.assertFalse(store.claim_human_gate(vid, "rejected"))
            self.assertEqual(
                store.latest_verdict(cid)["human_gate"], "approved")
        finally:
            store.close()

    def test_claim_rejects_unknown_and_decided_verdicts(self):
        store = Store(":memory:")
        try:
            self.assertFalse(
                store.claim_human_gate("vrd_9999", "approved"))
            target_id = store.create_target("ask", "code")
            cid = store.create_contract(
                target_id, "solve", "task", "code", [], [])
            vid = store.create_verdict(
                cid, 1, "cand_1", [], "pass", "pass", None, False,
                None, None, "approved", None)
            self.assertFalse(store.claim_human_gate(vid, "rejected"))
        finally:
            store.close()

    def test_claim_race_across_processes_has_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data.db"
            store = Store(db_path)
            _cid, vid = self._pending_verdict(store)
            store.close()
            first = Store(db_path)
            second = Store(db_path)
            try:
                results = sorted([
                    first.claim_human_gate(vid, "approved"),
                    second.claim_human_gate(vid, "approved"),
                ])
                self.assertEqual(results, [False, True])
            finally:
                first.close()
                second.close()


class TestSetCandidatePayloadHash(unittest.TestCase):
    """G1 support: the verdict-time snapshot re-points the winner's
    payload hash at the snapshot commit, committed."""

    def test_set_and_persist_payload_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data.db"
            store = Store(db_path)
            target_id = store.create_target("ask", "code")
            cid = store.create_contract(
                target_id, "solve", "task", "code", [], [])
            cand = store.create_candidate(
                cid, 1, "solve", "exp-1", "fam", "slot-a", "/wt",
                "basehash")
            try:
                self.assertIsNone(
                    store.get_candidate(cand)["payload_hash"])
                store.set_candidate_payload_hash(cand, "snaphash")
                self.assertEqual(
                    store.get_candidate(cand)["payload_hash"],
                    "snaphash")
            finally:
                store.close()
            store = Store(db_path)
            try:
                self.assertEqual(
                    store.get_candidate(cand)["payload_hash"],
                    "snaphash")
            finally:
                store.close()


class TestRouteDemotionHonesty(unittest.TestCase):
    """G8 (round-3 contest-core F5): apply_route_demotion handles
    the fallback branch and RETURNS what actually changed — the old
    code silently no-oped for fallback/absent experts while callers
    reported demotions that never happened."""

    def setUp(self):
        self.store = Store(":memory:")
        self.store.upsert_route(
            "code", "exp-a", "exp-b", ["exp-c", "exp-d", "exp-e"],
            ["j"], "r")

    def tearDown(self):
        self.store.close()

    def test_fallback_member_moves_to_end(self):
        result = self.store.apply_route_demotion(
            "code", "exp-d", None, "exp-d kept losing")
        self.assertTrue(result["changed"])
        self.assertEqual(result["branch"], "fallback")
        self.assertIsNone(result["promoted"])
        self.assertEqual(
            result["fallback"], ["exp-c", "exp-e", "exp-d"])
        self.assertEqual(result["primary"], "exp-a")
        self.assertEqual(result["compete_with"], "exp-b")

    def test_fallback_member_already_last_changes_nothing(self):
        before = self.store.get_route("code")
        result = self.store.apply_route_demotion(
            "code", "exp-e", None, "already last")
        self.assertFalse(result["changed"])
        self.assertEqual(result["branch"], "fallback")
        self.assertEqual(self.store.get_route("code"), before)

    def test_absent_expert_changes_nothing(self):
        before = self.store.get_route("code")
        result = self.store.apply_route_demotion(
            "code", "exp-ghost", "exp-c", "not seated")
        self.assertFalse(result["changed"])
        self.assertEqual(result["branch"], "none")
        self.assertIsNone(result["promoted"])
        # The route row was not even rewritten (updated_at intact).
        self.assertEqual(self.store.get_route("code"), before)

    def test_primary_branch_reports_promotion(self):
        result = self.store.apply_route_demotion(
            "code", "exp-a", "exp-c", "exp-a kept losing")
        self.assertTrue(result["changed"])
        self.assertEqual(result["branch"], "primary")
        self.assertEqual(result["promoted"], "exp-c")
        self.assertEqual(result["primary"], "exp-c")
        self.assertEqual(result["compete_with"], "exp-a")

    def test_primary_without_successor_changes_nothing(self):
        result = self.store.apply_route_demotion(
            "code", "exp-a", None, "no successor named")
        self.assertFalse(result["changed"])
        self.assertEqual(result["branch"], "primary")
        self.assertIsNone(result["promoted"])
        self.assertEqual(result["primary"], "exp-a")

    def test_compete_with_branch_reports_promotion(self):
        result = self.store.apply_route_demotion(
            "code", "exp-b", "exp-d", "exp-b refused")
        self.assertTrue(result["changed"])
        self.assertEqual(result["branch"], "compete_with")
        self.assertEqual(result["promoted"], "exp-d")
        self.assertEqual(result["compete_with"], "exp-d")
        self.assertEqual(result["fallback"][0], "exp-b")


if __name__ == "__main__":
    unittest.main()
