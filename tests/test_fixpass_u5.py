"""Regression tests for the round-4 fix pass — Unit 5 (standing,
reports, hooks truth). Spec: FIX-SPEC-ROUND4-2026-11-08.md G8/G3.

Each test names its source finding (reports in
~/workspace/mm-review3-2026-11-08/ and ~/workspace/mm-review4-2026-11-08/):

- counted-row rule v2 — round-3 attack F1/F2: a counted provenance
  label no longer counts by itself; the row must be referentially
  backed by the store's candidate graph, and "" counts as nothing.
- chain-gated standing — round-3 attack F2(b): standing used to sum a
  book whose chain verify_chain called broken.
- excluded totals split by cause — round-3 attack F2, round-4
  regression F9 (disclosure), fix spec G8.
- per-expert figures from the unified totals — round-4 contest F8.
- CPVP definition — round-4 contest F10.
- verdict_report winner commit / attempt / tie_break — contest F12.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

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


class TestReferentialCounting(_StoreCase):
    """Round-3 attack F1, reproduced: the round-3 store read
    MILLIONAIRE from rows whose contract/candidate never existed,
    under labels the writer invented. Under counted-row rule v2 the
    chain still verifies (the rows are honestly sealed) — the rows
    simply do not count, and the report says why."""

    def _book(self):
        cand_gen = self._candidate(slot="gen")   # stored provenance "generated"
        cand_er = self._candidate(slot="er")
        add = self.store.add_ledger
        # Genuine, referentially backed rows — these count.
        add("tgt-1", "ctr-1", cand_gen, "implement", "exp-1",
            10, 5, 100.0, "completed", None, provenance="generated")
        add("tgt-1", "ctr-1", cand_er, "implement", "exp-1",
            10, 5, 50.0, "completed", None,
            provenance="executor-reported")
        add("tgt-1", "ctr-1", None, "implement", "exp-1",
            10, 5, 25.0, "completed", None, provenance="legacy-import")
        # The attack, in its round-3 shapes.
        add("tgt-1", "ctr-1", "cand_ghost", "implement", "mallory",
            1, 1, 2_000_000.0, "completed", None,
            provenance="executor-reported")
        add("tgt-1", "ctr-1", "cand_ghost", "implement", "mallory",
            1, 1, 500.0, "completed", None, provenance="model-measured")
        add("tgt-1", "ctr-1", cand_gen, "implement", "mallory",
            1, 1, 250.0, "completed", None,
            provenance="totally-invented")
        add("tgt-1", "ctr-1", cand_gen, "implement", "mallory",
            1, 1, 700.0, "completed", None, provenance="")
        add("tgt-1", "ctr-1", cand_gen, "implement", "mallory",
            1, 1, 900.0, "completed", None, provenance="internal")
        add("tgt-1", "ctr-1", cand_er, "implement", "mallory",
            1, 1, 300.0, "observed", None,
            provenance="executor-reported")

    def test_unbacked_labels_do_not_move_the_tier(self):
        self._book()
        ok, bad = chain.verify_chain(self.store)
        self.assertTrue(ok, f"chain broke at {bad}")
        result = standing.compute_standing(self.store)
        self.assertEqual(result["cost_usd"], 175.0)
        self.assertEqual(result["tier"], "CLIENT")
        line = standing.standing_line(self.store)
        self.assertIn("CLIENT", line)
        self.assertNotIn("MILLIONAIRE", line)

    def test_excluded_totals_split_by_cause(self):
        self._book()
        result = standing.compute_standing(self.store)
        self.assertEqual(result["excluded_referential_cost_usd"], 2000750.0)
        self.assertEqual(result["excluded_referential_rows"], 3)
        self.assertEqual(result["excluded_provenance_cost_usd"], 1600.0)
        self.assertEqual(result["excluded_provenance_rows"], 2)
        self.assertEqual(result["excluded_outcome_cost_usd"], 300.0)
        self.assertEqual(result["excluded_outcome_rows"], 1)
        self.assertEqual(result["excluded_cost_usd"], 2002650.0)
        self.assertEqual(result["excluded_rows"], 6)

    def test_report_discloses_each_cause_with_its_label(self):
        self._book()
        text = standing.standing_report(self.store)
        self.assertIn(
            "$2,000,750.00 across 3 rows not counted — "
            "referential-excluded (no matching candidate on record)",
            text)
        self.assertIn(
            "$1,600.00 across 2 rows not counted — "
            "unattributed/internal writers", text)
        # Singular: "1 row", never "1 rows".
        self.assertIn(
            "$300.00 across 1 row not counted — non-dispatch outcomes",
            text)
        self.assertNotIn("1 rows not counted", text)

    def test_singular_referential_line(self):
        self.store.add_ledger(
            "tgt-1", "ctr-1", "cand_ghost", "implement", "mallory",
            1, 1, 5.0, "completed", None, provenance="executor-reported")
        text = standing.standing_report(self.store)
        self.assertIn(
            "$5.00 across 1 row not counted — referential-excluded",
            text)

    def test_sql_and_python_paths_agree(self):
        """G3: the SQL aggregation and the dict-path aggregation are
        the same rule — a store exposing only ledger_rows +
        get_candidate totals identically to the real store."""

        class DictStore:
            def __init__(self, real):
                self._real = real

            def ledger_rows(self, expert_id=None):
                return self._real.ledger_rows(expert_id)

            def get_candidate(self, candidate_id):
                return self._real.get_candidate(candidate_id)

        self._book()
        wrapped = DictStore(self.store)
        self.assertEqual(
            standing.ledger_totals(self.store),
            standing.ledger_totals(wrapped))
        self.assertEqual(
            standing.ledger_totals(self.store, contract_id="ctr-1"),
            standing.ledger_totals(wrapped, contract_id="ctr-1"))
        self.assertEqual(
            standing.expert_totals(self.store),
            standing.expert_totals(wrapped))


class TestChainGatedStanding(_StoreCase):
    """Round-3 attack F2 / fix spec G8: standing surfaces check the
    chain first. A broken chain renders the unavailable line instead
    of a tier; the hook still injects its lessons."""

    UNAVAILABLE = (
        "Millionaire Matrix standing unavailable — "
        "ledger chain broken at row 1"
    )

    def _genuine_book(self):
        cand = self._candidate()
        self.store.add_ledger(
            "tgt-1", "ctr-1", cand, "implement", "exp-1",
            100, 50, 100.0, "completed", None,
            provenance="executor-reported")
        return cand

    def _tamper_first_row(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "UPDATE ledger SET cost_usd=999999.0 WHERE id=1")
            conn.commit()
        finally:
            conn.close()

    def test_tampered_row_gates_the_line_and_the_report(self):
        self._genuine_book()
        self.assertIn("CLIENT", standing.standing_line(self.store))
        self._tamper_first_row()
        ok, bad = chain.verify_chain(self.store)
        self.assertFalse(ok)
        self.assertEqual(bad, 1)
        self.assertEqual(standing.standing_line(self.store),
                         self.UNAVAILABLE)
        report = standing.standing_report(self.store)
        self.assertIn(self.UNAVAILABLE, report)
        self.assertNotIn("MILLIONAIRE", report)
        self.assertNotIn("experts:", report)

    def test_raw_sql_garbage_row_gates_standing(self):
        # Round-3 attack F2(b): a row inserted behind the store's
        # back with garbage hashes — the chain is broken at that row,
        # and standing refuses to state a tier over it.
        self._genuine_book()
        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.execute(
                "INSERT INTO ledger (target_id, contract_id,"
                " candidate_id, stage, expert_id, tokens_in,"
                " tokens_out, cost_usd, outcome, note, created_at,"
                " prev_hash, row_hash, provenance, sealed_via)"
                " VALUES ('tgt-1', 'ctr-1', 'cand_ghost', 'implement',"
                " 'mallory', 1, 1, 999999.0, 'completed', NULL,"
                " '2026-01-01T00:00:00+00:00', 'abc', 'def',"
                " 'generated', 'append')")
            garbage_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(
            standing.standing_line(self.store),
            "Millionaire Matrix standing unavailable — "
            f"ledger chain broken at row {garbage_id}")

    def test_hook_degrades_the_line_and_still_injects_lessons(self):
        self._genuine_book()
        self.store.create_lesson(
            contract_id="ctr-1", domain="code", tags=[], outcome="o",
            cause="x", rule="pre-allocate buffers", route_change=None,
            evidence=["verdict:ctr-1"], written_to="plugin_store")
        self._tamper_first_row()
        with tempfile.TemporaryDirectory() as tmp:
            result = hooks.pre_llm_call(self.store, Path(tmp))
        self.assertIsNotNone(result)
        context = result["context"]
        self.assertIn(self.UNAVAILABLE, context)
        self.assertIn("pre-allocate buffers", context)
        self.assertNotIn("MILLIONAIRE", context)


class TestUnifiedExpertFigures(_StoreCase):
    """Contest F8 / fix spec G8: per-expert dispatch counts and costs
    in the reports are the unified totals' figures — the
    provenance-blind summary no longer prints beside them."""

    def _book(self):
        cand = self._candidate()
        self.store.add_ledger(
            "tgt-1", "ctr-1", cand, "implement", "exp-1",
            100, 50, 10.0, "completed", None,
            provenance="executor-reported")
        # Dispatch-shaped bait: a completed row with big numbers,
        # written as the plugin's own bookkeeping. The old summary
        # counted it; the unified totals exclude it by provenance.
        self.store.add_ledger(
            "tgt-1", "ctr-1", cand, "implement", "exp-1",
            999, 999, 999.0, "completed", None, provenance="internal")

    def test_ledger_report_uses_counted_figures(self):
        self._book()
        report = ledger_report(self.store)
        exp_lines = [
            line for line in report.splitlines()
            if line.startswith("exp-1:")
        ]
        self.assertTrue(exp_lines)
        self.assertIn("dispatches=1", exp_lines[0])
        self.assertIn("cost_usd=10.0", exp_lines[0])
        self.assertNotIn("1009", report)

    def test_standing_report_uses_counted_figures(self):
        self._book()
        result = standing.compute_standing(self.store)
        rows = {r["expert_id"]: r for r in result["experts"]}
        self.assertEqual(rows["exp-1"]["dispatches"], 1)
        self.assertEqual(rows["exp-1"]["cost_usd"], 10.0)
        text = standing.standing_report(self.store)
        self.assertIn("dispatches=1", text)
        self.assertNotIn("1009", text)


class TestCpvpDefinition(_StoreCase):
    """Contest F10 / fix spec G8: wherever cpvp prints, its
    definition prints with it — generation cost of scored candidates;
    judging cost is not recorded and is excluded, stated plainly."""

    def test_definition_prints_in_both_reports(self):
        cand = self._candidate()
        self.store.add_ledger(
            "tgt-1", "ctr-1", cand, "implement", "exp-1",
            100, 50, 10.0, "completed", None,
            provenance="executor-reported")
        for text in (ledger_report(self.store),
                     standing.standing_report(self.store)):
            self.assertIn("generation cost of scored candidates", text)
            self.assertIn(
                "judging cost is not recorded by the system", text)


class TestVerdictReportFields(_StoreCase):
    """Contest F12 / fix spec G8: the verdict report shows what
    merged — the winner's commit, the attempt, and the tie_break."""

    def test_winner_commit_attempt_and_tie_break_print(self):
        tid = self.store.create_target("ask", "code")
        cid = self.store.create_contract(
            tid, stage="implement", task="t", domain="code", tags=[],
            acceptance=[])
        cand = self.store.create_candidate(
            cid, 1, "implement", "exp-1", "fam", "win", None, None)
        self.store.update_candidate(cand, payload_hash="abc123commit")
        self.store.create_verdict(
            contract_id=cid, attempt=2, winner=cand, eliminated=[],
            hard="pass", measured="pass", judged=None, escalated=False,
            escalation_reason=None, waiver=None, human_gate="approved",
            tie_break="first finisher")
        text = verdict_report(self.store, cid)
        self.assertIn("winner_commit: abc123commit", text)
        self.assertIn("attempt: 2", text)
        self.assertIn("tie_break: first finisher", text)


class TestHasStandingProbe(_StoreCase):
    """Fix spec G3: has_standing is an existence probe — any dispatch
    on record (counted or not, the set the experts list is built
    from), and no standing computation behind it."""

    def test_probe(self):
        self.assertFalse(standing.has_standing(self.store))
        self.store.add_ledger(
            "tgt-1", "ctr-1", None, "implement", "queue",
            None, None, None, "observed", "a note",
            provenance="internal")
        self.assertFalse(standing.has_standing(self.store))
        self.store.add_ledger(
            "tgt-1", "ctr-1", None, "implement", "exp-1",
            None, None, None, "completed", None, provenance="internal")
        self.assertTrue(standing.has_standing(self.store))


if __name__ == "__main__":
    unittest.main()
