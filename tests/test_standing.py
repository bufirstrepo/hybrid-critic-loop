"""Unit A tests: standing — tiers, the client book, and its surfaces.

Everything runs against the real Store in a temp dir; the numbers are
seeded ledger rows, and the assertions are on what the reports print
from those rows (never on invented totals).
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from hybrid_critic_loop import cli, hooks, standing
from hybrid_critic_loop.reports import verdict_report
from hybrid_critic_loop.state import Store


def _dispatch(store, expert="exp-1", contract="ctr-1", tin=100, tout=50,
              cost=12.5):
    return store.add_ledger(
        "tgt-1", contract, f"cand-{expert}-{id(object())}", "implement",
        expert, tin, tout, cost, "completed", None,
    )


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "data.db")

    def tearDown(self):
        try:
            self.store.close()
        finally:
            self._tmp.cleanup()


class TestTierFor(unittest.TestCase):
    def test_boundaries(self):
        cases = [
            (None, "WINDOW SHOPPER"),
            (0, "WINDOW SHOPPER"),
            (0.009, "WINDOW SHOPPER"),
            (0.01, "CLIENT"),
            (999.99, "CLIENT"),
            (1000, "HIGH ROLLER"),
            (9999.99, "HIGH ROLLER"),
            (10000, "PLAYER"),
            (99999.99, "PLAYER"),
            (100000, "BALLER"),
            (999999.99, "BALLER"),
            (1000000, "MILLIONAIRE"),
            (2500000, "MILLIONAIRE"),
        ]
        for spend, expected in cases:
            with self.subTest(spend=spend):
                self.assertEqual(standing.tier_for(spend), expected)

    def test_next_tier(self):
        self.assertEqual(standing.next_tier(None), ("CLIENT", 0.01))
        self.assertEqual(standing.next_tier(500), ("HIGH ROLLER", 500.0))
        name, additional = standing.next_tier(150000)
        self.assertEqual(name, "MILLIONAIRE")
        self.assertAlmostEqual(additional, 850000.0)
        self.assertIsNone(standing.next_tier(1000000))
        self.assertIsNone(standing.next_tier(5000000))


class TestComputeStanding(_StoreCase):
    def test_empty_store(self):
        result = standing.compute_standing(self.store)
        self.assertIsNone(result["tokens"])
        self.assertIsNone(result["cost_usd"])
        self.assertEqual(result["tier"], "WINDOW SHOPPER")
        self.assertEqual(result["experts"], [])
        self.assertFalse(standing.has_standing(self.store))

    def test_unmeasured_store(self):
        _dispatch(self.store, tin=None, tout=None, cost=None)
        result = standing.compute_standing(self.store)
        self.assertIsNone(result["tokens"])
        self.assertIsNone(result["cost_usd"])
        self.assertEqual(result["tier"], "WINDOW SHOPPER")
        self.assertTrue(standing.has_standing(self.store))

    def test_totals_sum_recorded_values_only(self):
        _dispatch(self.store, expert="exp-1", tin=100, tout=50, cost=10.0)
        _dispatch(self.store, expert="exp-2", tin=200, tout=None, cost=2.5)
        _dispatch(self.store, expert="exp-1", tin=None, tout=25, cost=None)
        result = standing.compute_standing(self.store)
        self.assertEqual(result["tokens_in"], 300)
        self.assertEqual(result["tokens_out"], 75)
        self.assertEqual(result["tokens"], 375)
        self.assertEqual(result["cost_usd"], 12.5)
        self.assertEqual(result["tier"], "CLIENT")

    def test_per_expert_tier_from_validity_cost(self):
        self.store.upsert_expert("exp-1", "fam", "cli", "subscription")
        self.store.record_validity("exp-1", 10.0, 150000.0)
        _dispatch(self.store, expert="exp-1", cost=1.0)
        result = standing.compute_standing(self.store)
        rows = {r["expert_id"]: r for r in result["experts"]}
        self.assertEqual(rows["exp-1"]["tier"], "BALLER")
        self.assertEqual(rows["exp-1"]["validity_cost_usd"], 150000.0)
        # The headline tier is the ledger's lifetime cost, not validity cost.
        self.assertEqual(result["tier"], "CLIENT")


class TestStandingLine(_StoreCase):
    def test_measured_line_format(self):
        _dispatch(self.store, tin=1000, tout=500, cost=123.45)
        self.assertEqual(
            standing.standing_line(self.store),
            "Millionaire Matrix standing: CLIENT — "
            "$123.45 lifetime measured spend · 1,500 tokens",
        )

    def test_unmeasured_line_reports_not_reported(self):
        _dispatch(self.store, tin=None, tout=None, cost=None)
        line = standing.standing_line(self.store)
        self.assertIn("WINDOW SHOPPER", line)
        self.assertIn("not reported", line)
        self.assertNotIn("$", line)


class TestStandingReport(_StoreCase):
    def test_empty_store(self):
        text = standing.standing_report(self.store)
        self.assertIn("standing — the client book:", text)
        self.assertIn("WINDOW SHOPPER", text)
        self.assertIn("not reported", text)
        self.assertIn("next tier: CLIENT", text)
        self.assertIn("experts: not run", text)

    def test_next_tier_gap_is_exact(self):
        _dispatch(self.store, cost=500.0)
        text = standing.standing_report(self.store)
        self.assertIn("next tier: HIGH ROLLER — $500.00 more measured spend",
                      text)

    def test_millionaire_has_no_next_tier(self):
        _dispatch(self.store, cost=1500000.0)
        text = standing.standing_report(self.store)
        self.assertIn("MILLIONAIRE", text)
        self.assertNotIn("next tier:", text)

    def test_expert_rows_carry_their_tier(self):
        self.store.upsert_expert("exp-1", "fam", "cli", "subscription")
        self.store.record_validity("exp-1", 10.0, 150000.0)
        _dispatch(self.store, expert="exp-1", cost=1.0)
        text = standing.standing_report(self.store)
        self.assertIn("exp-1:", text)
        self.assertIn("tier=BALLER", text)


class TestHookInjection(_StoreCase):
    def _lesson(self, rule="pre-allocate buffers", contract="c1"):
        return self.store.create_lesson(
            contract_id=contract, domain="code", tags=[], outcome="o",
            cause="x", rule=rule, route_change=None,
            evidence=[f"verdict:{contract}"], written_to="plugin_store",
        )

    def test_standing_alone_when_no_lessons(self):
        _dispatch(self.store)
        with tempfile.TemporaryDirectory() as tmp:
            result = hooks.pre_llm_call(self.store, Path(tmp))
        self.assertIsNotNone(result)
        self.assertEqual(result["context"], standing.standing_line(self.store))

    def test_standing_line_above_lessons(self):
        _dispatch(self.store)
        self._lesson()
        with tempfile.TemporaryDirectory() as tmp:
            result = hooks.pre_llm_call(self.store, Path(tmp))
        context = result["context"]
        self.assertTrue(context.startswith("Millionaire Matrix standing:"))
        self.assertIn("pre-allocate buffers", context)
        self.assertLess(
            context.index("Millionaire Matrix standing:"),
            context.index("pre-allocate buffers"),
        )

    def test_no_standing_without_any_record(self):
        self._lesson()
        with tempfile.TemporaryDirectory() as tmp:
            result = hooks.pre_llm_call(self.store, Path(tmp))
        self.assertIsNotNone(result)
        self.assertNotIn("Millionaire Matrix standing", result["context"])

    def test_nothing_on_record_injects_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(hooks.pre_llm_call(self.store, Path(tmp)))

    def test_first_turn_only(self):
        _dispatch(self.store)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                hooks.pre_llm_call(self.store, Path(tmp), is_first_turn=False))

    def test_cap_holds_with_standing_prepended(self):
        _dispatch(self.store)
        for i in range(40):
            self._lesson(rule="long rule " + "x" * 300, contract=f"c{i}")
        with tempfile.TemporaryDirectory() as tmp:
            result = hooks.pre_llm_call(self.store, Path(tmp))
        self.assertIsNotNone(result)
        self.assertLessEqual(len(result["context"]), 10000)
        self.assertTrue(
            result["context"].startswith("Millionaire Matrix standing:"))


class TestCli(_StoreCase):
    def _seed_data_dir(self):
        data_dir = Path(self._tmp.name) / "cli-data"
        data_dir.mkdir()
        store = Store(data_dir / "data.db")
        try:
            _dispatch(store, tin=1000, tout=500, cost=123.45)
        finally:
            store.close()
        return data_dir

    def _run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def test_standing_command(self):
        data_dir = self._seed_data_dir()
        code, out = self._run_cli(["standing", "--data-dir", str(data_dir)])
        self.assertEqual(code, 0)
        self.assertIn("standing — the client book:", out)
        self.assertIn(
            "Millionaire Matrix standing: CLIENT — "
            "$123.45 lifetime measured spend · 1,500 tokens", out)

    def test_session_start_shows_the_standing_line(self):
        data_dir = self._seed_data_dir()
        code, out = self._run_cli(
            ["session-start", "--data-dir", str(data_dir)])
        self.assertEqual(code, 0)
        self.assertIn("Millionaire Matrix standing: CLIENT", out)


class TestVerdictReportStanding(_StoreCase):
    def _verdict(self, contract="ctr-1"):
        return self.store.create_verdict(
            contract_id=contract, attempt=1, winner="cand-1",
            eliminated=[], hard="pass", measured="pass", judged=None,
            escalated=False, escalation_reason=None, waiver=None,
            human_gate="pending", tie_break=None,
        )

    def test_verdict_report_carries_tier_and_run_contribution(self):
        _dispatch(self.store, contract="ctr-1", tin=1000, tout=500,
                  cost=123.45)
        self._verdict("ctr-1")
        text = verdict_report(self.store, "ctr-1")
        self.assertIn(
            "standing: CLIENT — this run: 1,500 tokens · $123.45", text)

    def test_verdict_report_unmeasured_run(self):
        self._verdict("ctr-9")
        text = verdict_report(self.store, "ctr-9")
        self.assertIn("standing: WINDOW SHOPPER — this run: not reported",
                      text)


if __name__ == "__main__":
    unittest.main()
