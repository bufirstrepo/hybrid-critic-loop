"""Regression tests for the round-7 fix pass — Unit 2 (store /
chain machinery). Spec: FIX-SPEC-ROUND7-2026-11-08.md K3 (store
half) / K4 / K7 / K9 (wording half).

Each test names its source finding (reports in
~/workspace/mm-review7-2026-11-08/):

- K3 (gate-core F2, security F2): two presenters racing one gate
  wrote TWO final answers — the loop's existence check and the
  store's insert were separate operations, and the interrupted-
  completion detector's decided→failed flip was an unconditional
  setter that could stomp a live completion. The store half: an
  atomic check-and-insert in create_final_answer (duplicate signal:
  FinalAnswerExistsError) and the conditional gate setter
  Store.set_human_gate_if the detector's flip now uses.
- K4 (attack F2): the era rollback — PRAGMA user_version=0 re-armed
  the migration, so a raw NULL-provenance append to a healthy
  current book sealed as "backfill" and counted $2M unbacked via
  the legacy exemption. The backfill route now requires a book no
  current-form seal contradicts.
- K7 (attack F4): J3's cache deletion was best-effort AND silent —
  when the delete failed, the stale cache survived and the hook
  went on certifying the doctored book, traceless. A failed clear
  now marks the instance's cache untrusted; the cached check falls
  back to the full walk until a successful clear or seed.
- K9 (upgrade-walk F1): a historical-form row at an interior
  position is invisible to the open-time probe and strands the
  book, and standing misreported the un-upgraded book as corrupt
  ("broken"). The unavailable line now names the older-form state
  for a first bad row that recomputes under a historical variant.
"""

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from hybrid_critic_loop import chain, standing
from hybrid_critic_loop.state import FinalAnswerExistsError, Store


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

    def _candidate(self, contract="ctr-1", expert="exp-1", slot="cand"):
        return self.store.create_candidate(
            contract, 1, "implement", expert, "fam", slot, None, None)

    def _dispatch(self, cand, cost, contract="ctr-1", expert="exp-1",
                  provenance="executor-reported"):
        return self.store.add_ledger(
            "tgt-1", contract, cand, "implement", expert,
            100, 50, cost, "completed", None, provenance=provenance)

    def _raw(self, sql, params=()):
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def _verdict(self, contract="ctr-1", human_gate="pending"):
        tid = self.store.create_target(
            ask="x", domain="code", constraints=[], budget=None,
            done_when="human_gate")
        self.store.create_contract(
            target_id=tid, stage="solve", task="t", domain="code",
            tags=[], acceptance=[], depends_on=[],
            test_author_family=None, repo_path=".", contract_id=contract)
        return self.store.create_verdict(
            contract, 1, "w1", [], "pass", "pass", None, False, None,
            None, human_gate, None)


# ---------------------------------------------------------------------------
# K3 — the conditional gate setter (cross-unit API)
# ---------------------------------------------------------------------------

class TestConditionalGateSetter(_StoreCase):
    """Store.set_human_gate_if(verdict_id, expected, value) -> bool:
    the compare-and-swap the interrupted-completion detector's
    decided→failed flip is built on. The flip lands only on the
    state the detector observed; a state that moved under it is
    left exactly as found."""

    def test_flip_lands_only_on_the_observed_state(self):
        vid = self._verdict()
        # Pending → decided (the claim shape).
        self.assertTrue(
            self.store.set_human_gate_if(vid, "pending", "approved"))
        self.assertEqual(
            self.store.latest_verdict("ctr-1")["human_gate"], "approved")
        # The same flip again loses: the state is no longer pending.
        self.assertFalse(
            self.store.set_human_gate_if(vid, "pending", "approved"))
        # The detector's flip: decided → failed, conditional on the
        # exact decided value it observed.
        self.assertFalse(
            self.store.set_human_gate_if(vid, "rejected", "failed"))
        self.assertTrue(
            self.store.set_human_gate_if(vid, "approved", "failed"))
        self.assertEqual(
            self.store.latest_verdict("ctr-1")["human_gate"], "failed")
        # A live completion landing first (approved) defeats a stale
        # detector's flip: expected "failed" no longer holds once a
        # retry re-claimed the gate.
        self.assertTrue(
            self.store.set_human_gate_if(vid, "failed", "approved"))
        self.assertFalse(
            self.store.set_human_gate_if(vid, "failed", "failed"))
        # Unknown verdicts flip nothing.
        self.assertFalse(
            self.store.set_human_gate_if("vrd_nope", "pending", "approved"))


# ---------------------------------------------------------------------------
# K3 — create_final_answer is an atomic check-and-insert
# ---------------------------------------------------------------------------

class TestFinalAnswerAtomicInsert(_StoreCase):
    """Round-7 gate-core F2's store half: the duplicate signal is
    FinalAnswerExistsError, raised when the contract already has a
    final answer; the existence re-check and the INSERT run under
    one BEGIN IMMEDIATE write transaction, so two Store instances
    cannot both pass the check."""

    def test_second_create_raises_and_writes_nothing(self):
        fid = self.store.create_final_answer(
            "tgt-1", "ctr-1", "abc123", human_gate="approved")
        self.assertIsInstance(fid, int)
        with self.assertRaises(FinalAnswerExistsError) as caught:
            self.store.create_final_answer(
                "tgt-1", "ctr-1", "def456", human_gate="approved")
        self.assertEqual(caught.exception.existing_id, fid)
        self.assertEqual(caught.exception.contract_id, "ctr-1")
        rows = self.store.final_answers_for("ctr-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["commit"], "abc123")
        # A different contract is unaffected.
        other = self.store.create_final_answer(
            "tgt-1", "ctr-2", "def456", human_gate="approved")
        self.assertIsInstance(other, int)
        self.assertEqual(len(self.store.final_answers_for("ctr-2")), 1)

    def test_interleaved_presenters_write_exactly_one_answer(self):
        """The race, deterministically: presenter A performs the
        old pre-check (no answer exists), presenter B completes,
        then A's insert runs. Under the pre-fix shape A's check and
        insert were separate and A wrote a second answer; now A's
        create itself re-checks under the write lock and raises."""
        other = Store(self.db_path)
        try:
            self.assertEqual(self.store.final_answers_for("ctr-1"), [])
            fid = other.create_final_answer(
                "tgt-1", "ctr-1", "bbb222", human_gate="approved")
            with self.assertRaises(FinalAnswerExistsError) as caught:
                self.store.create_final_answer(
                    "tgt-1", "ctr-1", "aaa111", human_gate="approved")
            self.assertEqual(caught.exception.existing_id, fid)
            rows = self.store.final_answers_for("ctr-1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["commit"], "bbb222")
        finally:
            other.close()

    def test_threaded_presenters_write_exactly_one_answer(self):
        """Two Store instances, two threads, one barrier-synchronised
        start: exactly one create succeeds, the other converges on
        FinalAnswerExistsError, and the book holds one row."""
        other = Store(self.db_path)
        barrier = threading.Barrier(2)
        outcomes = {}

        def present(name, store, commit):
            barrier.wait(timeout=30)
            try:
                outcomes[name] = (
                    "created",
                    store.create_final_answer(
                        "tgt-1", "ctr-1", commit, human_gate="approved"))
            except FinalAnswerExistsError as exc:
                outcomes[name] = ("exists", exc.existing_id)

        threads = [
            threading.Thread(
                target=present, args=("a", self.store, "aaa111")),
            threading.Thread(
                target=present, args=("b", other, "bbb222")),
        ]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
            self.assertFalse(any(t.is_alive() for t in threads))
            kinds = sorted(kind for kind, _id in outcomes.values())
            self.assertEqual(kinds, ["created", "exists"])
            created_id = next(
                _id for kind, _id in outcomes.values() if kind == "created")
            exists_id = next(
                _id for kind, _id in outcomes.values() if kind == "exists")
            self.assertEqual(exists_id, created_id)
            self.assertEqual(
                len(self.store.final_answers_for("ctr-1")), 1)
        finally:
            other.close()


# ---------------------------------------------------------------------------
# K4 — backfill requires a genuinely pre-chain book (attack F2)
# ---------------------------------------------------------------------------

def _raw_pre_chain_store(tmp, rows):
    """A ledger-only database the way pre-chain code left it: no
    provenance/sealed_via columns, no hashes, user_version 0."""
    db_path = Path(tmp) / "data.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE ledger ("
            " id INTEGER PRIMARY KEY, target_id TEXT, contract_id TEXT,"
            " candidate_id TEXT, stage TEXT, expert_id TEXT,"
            " tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL,"
            " outcome TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL)")
        for tin, tout, cost in rows:
            conn.execute(
                "INSERT INTO ledger (target_id, contract_id, candidate_id,"
                " stage, expert_id, tokens_in, tokens_out, cost_usd,"
                " outcome, note, created_at) VALUES ('tgt_0001',"
                " 'ctr_0001', 'cand_1', 'solve', 'exp-1', ?, ?, ?,"
                " 'completed', NULL, '2026-01-01T00:00:00+00:00')",
                (tin, tout, cost))
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
    finally:
        conn.close()
    return db_path


class TestBackfillRequiresSealFreeBook(_StoreCase):
    """The era rollback, verbatim from round-7 attack F2: healthy
    closed book, PRAGMA user_version=0, one raw NULL-provenance
    $2M append, reopen. The flag alone used to re-arm the backfill
    route; now the book's own current-form seals contradict the
    era claim, and the append seals as "repair" — counted only via
    the ordinary referential leg, which it fails."""

    def test_era_rollback_append_seals_as_repair_not_backfill(self):
        cand = self._candidate()
        self._dispatch(cand, 100.0)
        self._dispatch(cand, 200.0)
        self._dispatch(cand, 300.0)
        self.store.close()
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("PRAGMA user_version = 0")
            conn.execute(
                "INSERT INTO ledger (target_id, contract_id,"
                " candidate_id, stage, expert_id, tokens_in,"
                " tokens_out, cost_usd, outcome, note, created_at,"
                " prev_hash, row_hash, provenance, sealed_via)"
                " VALUES ('tgt-1', 'ghost', 'nobody', 'implement',"
                " 'mallory', 100, 100, 2000000.0, 'completed', NULL,"
                " '2026-01-02T00:00:00+00:00', NULL, NULL, NULL, NULL)")
            conn.commit()
        finally:
            conn.close()
        self.store = Store(self.db_path)
        # The rollback did NOT re-arm the migration route.
        self.assertEqual(self.store.chain_backfilled, 0)
        self.assertEqual(self.store.chain_repairs, 1)
        row = self.store.ledger_rows()[-1]
        self.assertEqual(row["provenance"], "legacy-import")
        self.assertEqual(row["sealed_via"], "repair")
        ok, bad = chain.verify_chain(self.store)
        self.assertTrue(ok, f"chain broke at {bad}")
        # The $2M does not count via the exemption.
        totals = standing.ledger_totals(self.store)
        self.assertEqual(totals["cost_usd"], 600.0)
        self.assertEqual(totals["excluded_referential_rows"], 1)
        self.assertEqual(
            totals["excluded_referential_cost_usd"], 2000000.0)
        line = standing.standing_line(self.store)
        self.assertIn("CLIENT", line)
        self.assertNotIn("MILLIONAIRE", line)
        # The version flag still advances, as before.
        conn = sqlite3.connect(str(self.db_path))
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(version, 1)

    def test_genuine_pre_chain_book_still_backfills_and_counts(self):
        """Positive control: a genuinely pre-chain book — no seals
        anywhere — backfills exactly as before, and its rows count
        via the exemption (the upgrade walk's migration shapes keep
        their totals)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = _raw_pre_chain_store(tmp, [(10, 5, 0.5), (20, 5, 0.25)])
            store = Store(db_path)
            try:
                self.assertEqual(store.chain_backfilled, 2)
                self.assertEqual(store.chain_repairs, 0)
                rows = store.ledger_rows()
                self.assertEqual(
                    [r["provenance"] for r in rows],
                    ["legacy-import", "legacy-import"])
                self.assertEqual(
                    [r["sealed_via"] for r in rows],
                    ["backfill", "backfill"])
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"chain broke at {bad}")
                totals = standing.ledger_totals(store)
                self.assertEqual(totals["cost_usd"], 0.75)
            finally:
                store.close()


# ---------------------------------------------------------------------------
# K7 — a failed cache clear marks the cache untrusted (attack F4)
# ---------------------------------------------------------------------------

class TestCacheClearFailureDistrustsCache(_StoreCase):
    """J3's deletion was best-effort and silent: with the delete
    sabotaged (the reviewer's stand-in for WAL lock contention on
    the meta rows), a proven break left the cache byte-identical
    and the hook printed the doctored tier. Now the failed clear
    records itself on the instance, and the cached check answers
    from the full walk until a successful clear or seed."""

    UNAVAILABLE = (
        "Millionaire Matrix standing unavailable — "
        "ledger chain broken at row 2"
    )

    def test_failed_clear_makes_the_cached_check_walk(self):
        cand = self._candidate()
        self._dispatch(cand, 100.0)
        self._dispatch(cand, 200.0)
        self._dispatch(cand, 300.0)
        self.assertEqual(chain.verify_chain(self.store), (True, None))
        self.assertIsNotNone(self.store.get_chain_verified_head())
        self.assertFalse(self.store._chain_cache_untrusted)

        self._raw("UPDATE ledger SET cost_usd=900000.0 WHERE id=2")

        def sabotaged_clear():
            raise RuntimeError("meta delete blocked (test double)")

        self.store.clear_chain_verified_head = sabotaged_clear
        try:
            # The full walk proves the break; its clear fails.
            self.assertEqual(chain.verify_chain(self.store), (False, 2))
            # The failure is recorded, not silent — and the cache is
            # still physically there, which is exactly the danger.
            self.assertTrue(self.store._chain_cache_untrusted)
            self.assertIsNotNone(self.store.get_chain_verified_head())
            # The cached check distrusts it: the verdict comes from
            # the full walk, and the recorded check says so.
            self.assertEqual(
                chain.verify_chain_cached(self.store), (False, 2))
            self.assertEqual(
                self.store._mm_last_chain_check, ("full", None))
            # The hook's line answers from the walk too — never the
            # doctored BALLER tier the surviving cache would print.
            line = standing.standing_line(self.store, cached=True)
            self.assertEqual(line, self.UNAVAILABLE)
            self.assertNotIn("BALLER", line)
        finally:
            del self.store.clear_chain_verified_head
        # With the clear working again, the next failed walk drops
        # the cache and resets the marker.
        self.assertEqual(chain.verify_chain(self.store), (False, 2))
        self.assertIsNone(self.store.get_chain_verified_head())
        self.assertFalse(self.store._chain_cache_untrusted)
        self.assertEqual(
            chain.verify_chain_cached(self.store), (False, 2))

    def test_successful_seed_after_a_failed_clear_restores_trust(self):
        cand = self._candidate()
        self._dispatch(cand, 100.0)
        self._dispatch(cand, 200.0)
        self._dispatch(cand, 300.0)
        self.assertEqual(chain.verify_chain(self.store), (True, None))
        self._raw("UPDATE ledger SET cost_usd=900000.0 WHERE id=2")

        def sabotaged_clear():
            raise RuntimeError("meta delete blocked (test double)")

        self.store.clear_chain_verified_head = sabotaged_clear
        try:
            self.assertEqual(chain.verify_chain(self.store), (False, 2))
            self.assertTrue(self.store._chain_cache_untrusted)
        finally:
            del self.store.clear_chain_verified_head
        # Heal the book; the successful walk's seed clears the
        # marker, and the cached check trusts the cache again.
        self._raw("UPDATE ledger SET cost_usd=200.0 WHERE id=2")
        self.assertEqual(chain.verify_chain(self.store), (True, None))
        self.assertFalse(self.store._chain_cache_untrusted)
        self.assertEqual(
            chain.verify_chain_cached(self.store), (True, None))
        self.assertEqual(self.store._mm_last_chain_check[0], "cached")


# ---------------------------------------------------------------------------
# K9 — the unavailable line names an older-form seal (upgrade-walk F1)
# ---------------------------------------------------------------------------

class TestOlderFormWording(_StoreCase):
    """A historical-form row at an interior position is invisible
    to the open-time probe (head and tail spot-checks only), so the
    book strands un-upgraded; round 7's standing line called it
    corrupt. When the walk's first bad row recomputes under a
    historical variant, the line now says the book is sealed under
    an older form at that row; a row matching no form stays
    "broken"."""

    OLDER_FORM = (
        "Millionaire Matrix standing unavailable — "
        "ledger sealed under an older form at row 2"
    )
    BROKEN = (
        "Millionaire Matrix standing unavailable — "
        "ledger chain broken at row 2"
    )

    def _mixed_form_book(self):
        """Four current-form rows via the API; then, fixture-side,
        row 2 is re-sealed under the LEGACY canonical form and rows
        3–4 re-sealed (current form) over the new link, so every
        stored link is intact and only row 2's form is historical.
        Reopening is a no-op (the probe sees a healthy head and
        tail) — the round-7 walk's mixed-mid shape."""
        cand = self._candidate()
        for cost in (100.0, 200.0, 300.0, 400.0):
            self._dispatch(cand, cost)
        self.store.close()
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM ledger ORDER BY id ASC")]
            prev = rows[0]["row_hash"]
            row2 = dict(rows[1])
            row2["prev_hash"] = prev
            digest2 = chain.row_hash_legacy(prev, row2)
            conn.execute(
                "UPDATE ledger SET row_hash=? WHERE id=2", (digest2,))
            prev = digest2
            for row in rows[2:]:
                rewritten = dict(row)
                rewritten["prev_hash"] = prev
                digest = chain.row_hash(prev, rewritten)
                conn.execute(
                    "UPDATE ledger SET prev_hash=?, row_hash=?"
                    " WHERE id=?", (prev, digest, row["id"]))
                prev = digest
            conn.commit()
        finally:
            conn.close()
        self.store = Store(self.db_path)
        # The open-time probe missed it: nothing was re-sealed.
        self.assertEqual(self.store.chain_upgrades, 0)

    def test_interior_historical_row_reads_as_older_form(self):
        self._mixed_form_book()
        self.assertEqual(chain.verify_chain(self.store), (False, 2))
        self.assertEqual(standing.standing_line(self.store), self.OLDER_FORM)
        self.assertEqual(
            standing.chain_unavailable_line(self.store), self.OLDER_FORM)
        report = standing.standing_report(self.store)
        self.assertIn(self.OLDER_FORM, report)
        # The store-less call (reports.py's import shape) keeps the
        # plain broken wording exactly as before.
        self.assertEqual(standing._unavailable_line(2), self.BROKEN)

    def test_row_matching_no_form_stays_broken(self):
        cand = self._candidate()
        for cost in (100.0, 200.0, 300.0):
            self._dispatch(cand, cost)
        # Tamper: the stored hash now recomputes under NO variant.
        self._raw("UPDATE ledger SET cost_usd=900000.0 WHERE id=2")
        self.assertEqual(chain.verify_chain(self.store), (False, 2))
        self.assertEqual(standing.standing_line(self.store), self.BROKEN)
        self.assertEqual(
            standing.chain_unavailable_line(self.store), self.BROKEN)


if __name__ == "__main__":
    unittest.main()
