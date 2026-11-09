"""Regression tests for the state.py half of the round-5 fix pass
(FIX-SPEC-ROUND5-2026-11-08): H1 claim validation + retry from the
failure state, H2's reserved "legacy-import" label and write-once
candidate provenance, H6's head-complete open probe and cascade
marker preservation, the chain-verified head meta accessors, and
the raw-tuple ledger path (H3).

Each test names the round-5 finding it pins (reports in
~/workspace/mm-review5-2026-11-08/).
"""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hybrid_critic_loop import chain
from hybrid_critic_loop.state import Store

_FIELDS_PRE_SEALED_VIA = tuple(
    f for f in chain.LEDGER_FIELDS if f != "sealed_via")
_FIELDS_LEGACY = tuple(
    f for f in _FIELDS_PRE_SEALED_VIA if f != "provenance")


def _digest(fields, prev_hash, row):
    """sha256(prev || canonical(row)) over an explicit field set —
    the fixture-local mirror of chain.canonical_row for building
    historical seals without trusting the code under test."""
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


def _verdict(store, human_gate="pending"):
    target_id = store.create_target("do the thing", "code")
    store.create_contract(
        target_id, stage="solve", task="fix the bug", domain="code",
        tags=[], acceptance=[], contract_id="ctr_0001")
    return store.create_verdict(
        contract_id="ctr_0001", attempt=1, winner="cand_1",
        eliminated=[], hard="pass", measured="pass", judged=None,
        escalated=False, escalation_reason=None, waiver=None,
        human_gate=human_gate, tie_break=None)


class TestClaimValidation(unittest.TestCase):
    """Round-5 attack F5 / security F5: claim_human_gate validated
    nothing — "pending" returned True and recorded nothing (the gate
    stayed claimable), a nonsense value consumed the gate with
    garbage no surface can act on."""

    def test_pending_is_not_a_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.db")
            try:
                vid = _verdict(store)
                with self.assertRaises(ValueError):
                    store.claim_human_gate(vid, "pending")
                # The gate is untouched: a real claim still lands.
                self.assertTrue(store.claim_human_gate(vid, "approved"))
            finally:
                store.close()

    def test_nonsense_decision_rejected_and_gate_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.db")
            try:
                vid = _verdict(store)
                with self.assertRaises(ValueError):
                    store.claim_human_gate(vid, "bogus-value")
                verdict = store.latest_verdict("ctr_0001")
                self.assertEqual(verdict["human_gate"], "pending")
                self.assertTrue(store.claim_human_gate(vid, "rejected"))
            finally:
                store.close()

    def test_failed_is_a_state_not_a_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.db")
            try:
                vid = _verdict(store)
                with self.assertRaises(ValueError):
                    store.claim_human_gate(vid, "failed")
            finally:
                store.close()


class TestClaimRetryFromFailed(unittest.TestCase):
    """Round-5 fix spec H1: the verdict's human_gate value "failed"
    is the recorded post-claim failure state — settable through the
    existing setter, returned plainly, and re-claimable ONLY with
    retry=True."""

    def test_failed_state_round_trip_and_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.db")
            try:
                vid = _verdict(store)
                self.assertTrue(store.claim_human_gate(vid, "approved"))
                # The completion failed after the claim: the loop
                # records the failure state through the setter.
                store.set_verdict_human_gate(vid, "failed")
                self.assertEqual(
                    store.latest_verdict("ctr_0001")["human_gate"],
                    "failed")
                self.assertEqual(
                    store.verdict_for("ctr_0001", vid)["human_gate"],
                    "failed")
                # A plain claim does NOT fire from "failed"...
                self.assertFalse(store.claim_human_gate(vid, "approved"))
                # ...a retry claim does, and records the decision.
                self.assertTrue(
                    store.claim_human_gate(vid, "approved", retry=True))
                self.assertEqual(
                    store.latest_verdict("ctr_0001")["human_gate"],
                    "approved")
                # A decided gate is not claimable even with retry.
                self.assertFalse(
                    store.claim_human_gate(vid, "approved", retry=True))
            finally:
                store.close()

    def test_retry_also_claims_from_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.db")
            try:
                vid = _verdict(store)
                self.assertTrue(
                    store.claim_human_gate(vid, "waived", retry=True))
            finally:
                store.close()


class TestLegacyImportReserved(unittest.TestCase):
    """Round-5 attack F1: "legacy-import" was a writer-suppliable
    label the counted rule exempted unconditionally — the round-3
    mint with a new magic string. Round-5 fix spec H2: add_ledger
    rejects it; the migration's backfill (a raw UPDATE inside
    _migrate, never the public method) is its only writer."""

    def test_add_ledger_rejects_the_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.db")
            try:
                with self.assertRaises(ValueError):
                    _append(store, provenance="legacy-import")
                self.assertEqual(store.ledger_rows(), [])
            finally:
                store.close()

    def test_migration_still_backfills_pre_column_rows(self):
        # A book predating the provenance column: rows carry NULL
        # provenance; the migration labels them "legacy-import" on
        # open — through its private UPDATE, unaffected by the
        # public rejection.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data.db"
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "CREATE TABLE ledger ("
                    " id INTEGER PRIMARY KEY, target_id TEXT,"
                    " contract_id TEXT, candidate_id TEXT, stage TEXT,"
                    " expert_id TEXT, tokens_in INTEGER,"
                    " tokens_out INTEGER, cost_usd REAL,"
                    " outcome TEXT NOT NULL, note TEXT,"
                    " created_at TEXT NOT NULL, prev_hash TEXT,"
                    " row_hash TEXT, sealed_via TEXT)")
                conn.execute(
                    "INSERT INTO ledger (id, target_id, contract_id,"
                    " candidate_id, stage, expert_id, tokens_in,"
                    " tokens_out, cost_usd, outcome, note, created_at)"
                    " VALUES (1, 'tgt_0001', 'ctr_0001', 'cand_1',"
                    " 'solve', 'exp-1', 10, 5, 0.5, 'completed',"
                    " NULL, '2026-01-01T00:00:00+00:00')")
                conn.commit()
            finally:
                conn.close()
            store = Store(db_path)
            try:
                rows = store.ledger_rows()
                self.assertEqual(
                    [r["provenance"] for r in rows], ["legacy-import"])
            finally:
                store.close()


class TestWriteOnceCandidateProvenance(unittest.TestCase):
    """Round-5 attack F4: candidate provenance was rewritable at
    any time (update_candidate, unsealed, unlogged) — one call
    could retroactively back excluded rows or un-back counted
    history. Round-5 fix spec H2: write-once once the candidate has
    ledger rows; freely correctable before the first dispatch."""

    def _candidate(self, store):
        return store.create_candidate(
            "ctr_0001", 1, "solve", "exp-1", "fam-a", "0", None, "base")

    def test_change_before_any_rows_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.db")
            try:
                cid = self._candidate(store)
                store.update_candidate(cid, provenance="model-measured")
                self.assertEqual(
                    store.get_candidate(cid)["provenance"],
                    "model-measured")
            finally:
                store.close()

    def test_change_after_rows_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.db")
            try:
                cid = self._candidate(store)
                _append(store, candidate_id=cid)
                with self.assertRaises(ValueError):
                    store.update_candidate(
                        cid, provenance="executor-reported")
                # The stored value is untouched.
                self.assertEqual(
                    store.get_candidate(cid)["provenance"], "generated")
                # Other fields still update fine.
                store.update_candidate(cid, status="done")
                self.assertEqual(
                    store.get_candidate(cid)["status"], "done")
            finally:
                store.close()


class TestMixedFormProbe(unittest.TestCase):
    """Round-5 attack F3: the tail-only open probe stranded
    mixed-form books (legacy prefix, current tail) — the sealing
    pass never ran and verify_chain called the intact legacy seal
    "broken at row 1". Round-5 fix spec H6: the probe also checks
    the book's HEAD, so the pass runs and the prefix upgrades."""

    def test_legacy_prefix_current_tail_upgrades_on_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data.db"
            store = Store(db_path)
            for _ in range(3):
                _append(store)
            store.close()
            # Rows 1-2 re-sealed under the legacy form over their
            # stored links (the mixed-form book); row 3 stays
            # current-form, linking to row 2's legacy head.
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM ledger ORDER BY id ASC").fetchall()
                prev = chain.GENESIS_HASH
                for row in rows[:2]:
                    digest = _digest(_FIELDS_LEGACY, prev, dict(row))
                    conn.execute(
                        "UPDATE ledger SET prev_hash=?, row_hash=?"
                        " WHERE id=?", (prev, digest, row["id"]))
                    prev = digest
                # Row 3 sealed under the CURRENT form over the
                # legacy head — the attack's construction exactly.
                d3 = _digest(chain.LEDGER_FIELDS, prev, dict(rows[2]))
                conn.execute(
                    "UPDATE ledger SET prev_hash=?, row_hash=?"
                    " WHERE id=3", (prev, d3))
                conn.commit()
            finally:
                conn.close()
            store = Store(db_path)
            try:
                # All three rows re-seal in the one cascade (the
                # counter counts cascade work, as in round 4); only
                # the two converted rows change marker.
                self.assertEqual(store.chain_upgrades, 3)
                self.assertEqual(
                    [r["sealed_via"] for r in store.ledger_rows()],
                    ["upgrade", "upgrade", "append"])
                self.assertEqual(store.last_seal_count, 3)
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"chain broke at {bad}")
            finally:
                store.close()
            # And it persists: the next open is a no-op (round-3
            # F1's shape — the upgrade lands exactly once).
            store = Store(db_path)
            try:
                self.assertEqual(store.chain_upgrades, 0)
                self.assertEqual(store.last_seal_count, 0)
            finally:
                store.close()


class TestCascadeMarkerPreservation(unittest.TestCase):
    """Round-5 attack F8: the cascade stamped "upgrade" on every
    row it touched, erasing a genuine "repair" mark downstream of a
    historical row. Round-5 fix spec H6: a current-form row
    rehashed only because its link changed KEEPS its marker; only
    form-converted rows are marked "upgrade"."""

    def test_repair_mark_survives_a_later_cascade(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data.db"
            store = Store(db_path)
            for _ in range(3):
                _append(store)
            store.close()
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM ledger ORDER BY id ASC").fetchall()
                # Row 1 back to the legacy form (a later cascade
                # must convert it); row 2 current-form, re-linked;
                # row 3 current-form carrying a genuine repair mark,
                # re-linked behind row 2.
                first = dict(rows[0])
                d1 = _digest(_FIELDS_LEGACY, chain.GENESIS_HASH, first)
                conn.execute(
                    "UPDATE ledger SET prev_hash=?, row_hash=?"
                    " WHERE id=1", (chain.GENESIS_HASH, d1))
                second = dict(rows[1])
                d2 = _digest(chain.LEDGER_FIELDS, d1, second)
                conn.execute(
                    "UPDATE ledger SET prev_hash=?, row_hash=?"
                    " WHERE id=2", (d1, d2))
                third = dict(rows[2])
                third["sealed_via"] = "repair"
                d3 = _digest(chain.LEDGER_FIELDS, d2, third)
                conn.execute(
                    "UPDATE ledger SET prev_hash=?, row_hash=?,"
                    " sealed_via='repair' WHERE id=3", (d2, d3))
                conn.commit()
            finally:
                conn.close()
            store = Store(db_path)
            try:
                rows = store.ledger_rows()
                self.assertEqual(
                    [r["sealed_via"] for r in rows],
                    ["upgrade", "append", "repair"])
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"chain broke at {bad}")
            finally:
                store.close()


class TestChainVerifiedHeadMeta(unittest.TestCase):
    """Round-5 fix spec, pinned interfaces: the chain-verified head
    cache accessors chain.verify_chain /
    chain.verify_chain_cached build on."""

    def test_unset_reads_none_and_round_trip_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data.db"
            store = Store(db_path)
            try:
                self.assertIsNone(store.get_chain_verified_head())
                store.set_chain_verified_head(42, "ab" * 32, 42)
                self.assertEqual(
                    store.get_chain_verified_head(), (42, "ab" * 32, 42))
            finally:
                store.close()
            store = Store(db_path)
            try:
                self.assertEqual(
                    store.get_chain_verified_head(), (42, "ab" * 32, 42))
            finally:
                store.close()


class TestLedgerRowsRaw(unittest.TestCase):
    """Round-5 fix spec H3: the raw-tuple ledger path behind
    chain.py's fast verification — tuples in LEDGER_FIELDS order
    with (prev_hash, row_hash) appended, values matching the dict
    path field for field."""

    def test_raw_tuples_match_dict_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.db")
            try:
                _append(store)
                _append(store, tokens_in=7, cost_usd=None)
                dicts = store.ledger_rows()
                raw = store.ledger_rows_raw()
                self.assertEqual(len(raw), len(dicts))
                self.assertEqual(
                    Store.LEDGER_RAW_COLUMNS,
                    tuple(chain.LEDGER_FIELDS) + ("prev_hash", "row_hash"))
                for tup, d in zip(raw, dicts):
                    self.assertEqual(len(tup), 16)
                    for idx, field in enumerate(chain.LEDGER_FIELDS):
                        self.assertEqual(tup[idx], d[field])
                    self.assertEqual(tup[14], d["prev_hash"])
                    self.assertEqual(tup[15], d["row_hash"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
