"""Regression tests for the round-5 fix pass — Unit 5 (standing,
reports, hooks). Spec: FIX-SPEC-ROUND5-2026-11-08.md H2/H3.

Each test names its source finding (reports in
~/workspace/mm-review5-2026-11-08/):

- verdict_report chain gate — round-5 attack F2: the standing
  surfaces were gated, but the verdict report printed a BALLER tier
  and minted a fresh MM CERTIFIED stamp over the same broken book;
  ledger_report showed no chain state at all.
- (False, None) gate mapping + degraded honesty — round-5 upgrade
  F1: hashless rows (a degraded store's shape) mapped to "no gate",
  has_standing's fallback dead-ended through expert_totals, and the
  counted rule silently degraded to label-trust.
- contract-matched backing — round-5 attack F4 (totals half): a
  candidate recorded under contract A backed rows under contract B.
- SQL ledger_summary + hook call count — round-5 performance
  (ledger_summary materialized the whole book inside every
  compute_standing) and round-5 test-audit Finding 4 (the G3
  "hook computes standing once" bullet had no test).
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hybrid_critic_loop import chain, hooks, standing
from hybrid_critic_loop.reports import ledger_report, verdict_report
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

    def _candidate(self, contract="ctr-1", expert="exp-1", slot="cand"):
        return self.store.create_candidate(
            contract, 1, "implement", expert, "fam", slot, None, None)

    def _dispatch(self, cand, cost, contract="ctr-1", expert="exp-1",
                  provenance="executor-reported"):
        return self.store.add_ledger(
            "tgt-1", contract, cand, "implement", expert,
            100, 50, cost, "completed", None, provenance=provenance)


class _DictStore:
    """ledger_rows + get_candidate only — the totals' Python path."""

    def __init__(self, real):
        self._real = real

    def ledger_rows(self, expert_id=None):
        return self._real.ledger_rows(expert_id)

    def get_candidate(self, candidate_id):
        return self._real.get_candidate(candidate_id)


class TestVerdictReportChainGate(_StoreCase):
    """Round-5 attack F2: a mid-book tamper (tail intact) leaves the
    standing surfaces correctly "unavailable" — and, before this fix,
    the verdict report printed the doctored tier and a fresh stamp.
    Now the report is gated like every other standing surface."""

    UNAVAILABLE = (
        "Millionaire Matrix standing unavailable — "
        "ledger chain broken at row 2"
    )

    def _book(self):
        cand = self._candidate()
        self._dispatch(cand, 100.0)
        self._dispatch(cand, 200.0)
        self._dispatch(cand, 300.0)
        self.store.create_verdict(
            contract_id="ctr-1", attempt=1, winner=cand, eliminated=[],
            hard="pass", measured="pass", judged=None, escalated=False,
            escalation_reason=None, waiver=None, human_gate="approved",
            tie_break=None)
        return cand

    def _tamper_second_row(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "UPDATE ledger SET cost_usd=900000.0 WHERE id=2")
            conn.commit()
        finally:
            conn.close()

    def test_healthy_book_reports_tier_and_chain_state(self):
        self._book()
        text = verdict_report(self.store, "ctr-1")
        self.assertIn("standing:", text)
        self.assertNotIn("standing unavailable", text)
        self.assertIn(
            "chain state: verified through row 3",
            ledger_report(self.store))

    def test_tampered_book_prints_unavailable_and_no_stamp(self):
        self._book()
        self._tamper_second_row()
        ok, bad = chain.verify_chain(self.store)
        self.assertFalse(ok)
        self.assertEqual(bad, 2)
        text = verdict_report(self.store, "ctr-1")
        self.assertIn(self.UNAVAILABLE, text)
        # The mandatory assertions: no certification block, no tier
        # from the doctored totals, anywhere in the report.
        self.assertNotIn("MM CERTIFIED", text)
        self.assertNotIn("BALLER", text)
        self.assertNotIn("900,400", text)
        self.assertNotIn("standing:", text)

    def test_ledger_report_names_the_break(self):
        self._book()
        self._tamper_second_row()
        self.assertIn(
            "chain state: broken at row 2", ledger_report(self.store))


class TestCrossContractBacking(_StoreCase):
    """Round-5 attack F4 (totals half) / spec H2: the referential leg
    requires the candidate to belong to the row's own contract. A
    candidate recorded under contract A no longer backs rows written
    under contract B — in the SQL path and the Python path alike."""

    def _book(self):
        cand_a = self._candidate(contract="ctr-a", slot="cand")
        # The candidate's stored provenance is "generated"
        # (create_candidate); the contract-B row matches even that —
        # only the contract differs.
        self._dispatch(cand_a, 500.0, contract="ctr-b",
                       provenance="generated")
        return cand_a

    def test_cross_contract_row_does_not_count_sql(self):
        self._book()
        totals_b = standing.ledger_totals(
            self.store, contract_id="ctr-b")
        self.assertIsNone(totals_b["cost_usd"])
        self.assertEqual(totals_b["excluded_referential_rows"], 1)
        self.assertEqual(totals_b["excluded_referential_cost_usd"], 500.0)
        result = standing.compute_standing(self.store)
        self.assertIsNone(result["cost_usd"])
        self.assertEqual(result["tier"], "WINDOW SHOPPER")

    def test_cross_contract_row_does_not_count_python(self):
        cand_a = self._book()
        wrapped = _DictStore(self.store)
        self.assertEqual(
            standing.ledger_totals(self.store),
            standing.ledger_totals(wrapped))
        totals_b = standing.ledger_totals(wrapped, contract_id="ctr-b")
        self.assertIsNone(totals_b["cost_usd"])
        self.assertEqual(totals_b["excluded_referential_rows"], 1)
        # Control: the same candidate's row under its OWN contract
        # counts, on both paths.
        self._dispatch(cand_a, 500.0, contract="ctr-a",
                       provenance="generated")
        for store in (self.store, wrapped):
            totals_a = standing.ledger_totals(
                store, contract_id="ctr-a")
            self.assertEqual(totals_a["cost_usd"], 500.0)


class TestHashlessRowsGate(unittest.TestCase):
    """Round-5 upgrade F1 / spec H2: verify_chain's (False, None) —
    the hashless-rows break a degraded store produces — renders the
    unavailable line on every gated surface (row shown as "—"), and
    has_standing answers over the store's own rows."""

    UNAVAILABLE = (
        "Millionaire Matrix standing unavailable — "
        "ledger chain broken at row —"
    )

    def _duck(self, rows):
        class Duck:
            def ledger_rows(self, expert_id=None):
                return list(rows)

            def ledger_summary(self):
                return []

        return Duck()

    def test_hashless_rows_gate_the_line_and_the_report(self):
        duck = self._duck([
            {"expert_id": "exp-1", "outcome": "completed",
             "tokens_in": 100, "tokens_out": 50, "cost_usd": 6.0,
             "provenance": "executor-reported"},
        ])
        self.assertEqual(chain.verify_chain(duck), (False, None))
        self.assertEqual(standing.standing_line(duck), self.UNAVAILABLE)
        self.assertIn(
            self.UNAVAILABLE, standing.standing_report(duck))

    def test_has_standing_reads_the_stores_own_rows(self):
        self.assertFalse(standing.has_standing(self._duck([])))
        self.assertFalse(standing.has_standing(self._duck([
            {"expert_id": "queue", "outcome": "observed"}])))
        self.assertTrue(standing.has_standing(self._duck([
            {"expert_id": "exp-1", "outcome": "completed"}])))


class TestReferentialUncheckedDisclosure(_StoreCase):
    """Round-5 upgrade F1 / spec H2: when the totals layer cannot
    evaluate the referential leg (no candidate resolver), the totals
    carry referential_unchecked and the line discloses it — the tier
    is never silently label-trusted. The real Store never takes it."""

    class _RowsOnly:
        """A real book's rows + summary, with no resolver surface."""

        def __init__(self, real):
            self._real = real

        def ledger_rows(self, expert_id=None):
            return self._real.ledger_rows(expert_id)

        def ledger_summary(self):
            return self._real.ledger_summary()

    def _book(self):
        cand = self._candidate()
        self._dispatch(cand, 100.0)
        return cand

    def test_unchecked_leg_is_flagged_and_disclosed(self):
        self._book()
        wrapped = self._RowsOnly(self.store)
        totals = standing.ledger_totals(wrapped)
        self.assertTrue(totals["referential_unchecked"])
        line = standing.standing_line(wrapped)
        self.assertIn("CLIENT", line)
        self.assertTrue(line.endswith("· backing unverified"), line)
        report = standing.standing_report(wrapped)
        self.assertIn("· backing unverified", report)

    def test_real_store_is_checked_and_undisclosed(self):
        self._book()
        totals = standing.ledger_totals(self.store)
        self.assertFalse(totals["referential_unchecked"])
        line = standing.standing_line(self.store)
        self.assertNotIn("backing unverified", line)


class TestSummarySql(_StoreCase):
    """Round-5 performance / spec H3: the summary compute_standing
    consumes is aggregated in SQL when the store exposes a
    connection — byte-identical shape and figures to
    Store.ledger_summary, and no whole-book materialization."""

    def _book(self):
        self.store.upsert_expert("exp-1", "fam", "cli", "subscription")
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "UPDATE experts SET wins=3, losses=1,"
                " validity_points=4, validity_cost_usd=10.0"
                " WHERE id='exp-1'")
            conn.commit()
        finally:
            conn.close()
        cand = self._candidate()
        self._dispatch(cand, 10.5)
        # Provenance-blind in the summary: an internal row counts
        # here (the unified totals exclude it downstream).
        self._dispatch(cand, 3.25, provenance="internal")
        # A non-dispatch outcome is not a summary row at all.
        self.store.add_ledger(
            "tgt-1", "ctr-1", cand, "implement", "exp-1",
            None, None, None, "observed", "a note",
            provenance="internal")
        # An expert with no experts-table row: default fields.
        self.store.add_ledger(
            "tgt-1", "ctr-1", None, "implement", "exp-2",
            7, None, None, "refused", None,
            provenance="executor-reported")
        return cand

    def test_sql_summary_matches_store_summary(self):
        self._book()
        self.assertEqual(
            standing._summary_rows(self.store),
            self.store.ledger_summary())

    def test_compute_standing_does_not_materialize_the_book(self):
        self._book()
        real = self.store

        class CountingProxy:
            _conn = real._conn

            def __init__(self):
                self.rows_calls = 0
                self.summary_calls = 0

            def ledger_rows(self, expert_id=None):
                self.rows_calls += 1
                return real.ledger_rows(expert_id)

            def ledger_summary(self):
                self.summary_calls += 1
                return real.ledger_summary()

            def get_candidate(self, candidate_id):
                return real.get_candidate(candidate_id)

        proxy = CountingProxy()
        result = standing.compute_standing(proxy)
        self.assertEqual(proxy.rows_calls, 0)
        self.assertEqual(proxy.summary_calls, 0)
        self.assertEqual(
            result["cost_usd"],
            standing.compute_standing(real)["cost_usd"])


class TestHookComputesStandingOnce(_StoreCase):
    """Round-5 test-audit Finding 4 / spec H3 acceptance: the G3
    bullet "the hook computes standing once" is pinned by counting —
    one standing_line call (with the cached gate), one underlying
    compute_standing, per pre_llm_call."""

    def test_one_computation_per_hook_call(self):
        cand = self._candidate()
        self._dispatch(cand, 100.0)
        self.store.create_lesson(
            contract_id="ctr-1", domain="code", tags=[], outcome="o",
            cause="x", rule="pre-allocate buffers", route_change=None,
            evidence=["verdict:ctr-1"], written_to="plugin_store")
        with mock.patch.object(
                standing, "standing_line",
                wraps=standing.standing_line) as line_spy, \
                mock.patch.object(
                    standing, "compute_standing",
                    wraps=standing.compute_standing) as compute_spy:
            with tempfile.TemporaryDirectory() as tmp:
                result = hooks.pre_llm_call(self.store, Path(tmp))
        self.assertIsNotNone(result)
        self.assertEqual(line_spy.call_count, 1)
        self.assertEqual(compute_spy.call_count, 1)
        # The hook's gate is the cached one (spec H3 pinned split).
        self.assertTrue(line_spy.call_args.kwargs.get("cached"))
        self.assertIn(
            "Millionaire Matrix standing:", result["context"])
        self.assertIn("pre-allocate buffers", result["context"])


if __name__ == "__main__":
    unittest.main()
