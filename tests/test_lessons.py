"""Worker D tests: lessons derivation, router demotion, reports, hooks.

Uses an in-memory FakeStore implementing the Store subset these modules
call — it does NOT depend on hybrid_critic_loop.state.
"""

import json
import tempfile
import unittest
from pathlib import Path

from hybrid_critic_loop import hooks, lessons, reports, router


class FakeStore:
    """In-memory stub of the Store API subset used by Worker D modules."""

    def __init__(self, db_path=None):
        self.db_path = db_path
        self._lessons = []
        self._next = 1
        self.routes = {}
        self.route_demotion_calls = []
        self.add_ledger_calls = []
        self._ledger = []
        self.ledger_summary_rows = []
        self.verdicts = {}
        self.open_contract = None
        self.observations = {}

    # -- lessons ---------------------------------------------------------
    def create_lesson(self, contract_id=None, domain=None, tags=None,
                      outcome=None, cause=None, rule=None, route_change=None,
                      evidence=None, written_to=None, provenance="generated",
                      **kw):
        lesson_id = f"lsn-{self._next}"
        self._next += 1
        self._lessons.append({
            "id": lesson_id, "contract_id": contract_id, "domain": domain,
            "tags": list(tags or []), "outcome": outcome, "cause": cause,
            "rule": rule, "route_change": route_change,
            "evidence": list(evidence or []), "written_to": written_to,
            "provenance": provenance,
        })
        return lesson_id

    def get_lesson(self, lesson_id):
        for lesson in self._lessons:
            if lesson["id"] == lesson_id:
                return lesson
        return None

    def lessons_for(self, domain=None, tags=None, limit=5):
        rows = [l for l in self._lessons
                if domain is None or l["domain"] == domain]
        wanted = set(tags or [])

        def key(lesson):
            overlap = len(wanted.intersection(lesson["tags"]))
            return (overlap, int(lesson["id"].split("-")[1]))

        rows.sort(key=key, reverse=True)
        return rows[:limit] if limit is not None else rows

    def count_lessons(self):
        return len(self._lessons)

    # -- routes ------------------------------------------------------------
    def get_route(self, category):
        route = self.routes.get(category)
        return dict(route) if route else None

    def set_route(self, category, route):
        self.routes[category] = dict(route)

    def upsert_route(self, category, route=None, **kw):
        self.routes[category] = dict(route) if route else dict(kw)

    def apply_route_demotion(self, category, demote, promote, reason):
        self.route_demotion_calls.append((category, demote, promote, reason))
        route = dict(self.routes.get(category) or {})
        if route.get("primary") == demote and promote:
            route["compete_with"] = demote
            route["primary"] = promote
        elif route.get("compete_with") == demote and promote:
            route["compete_with"] = promote
        self.routes[category] = route
        return dict(route)

    # -- ledger --------------------------------------------------------------
    def add_ledger(self, expert_id=None, outcome=None, note=None, **kw):
        self.add_ledger_calls.append(
            {"expert_id": expert_id, "outcome": outcome, "note": note, **kw}
        )
        row = {"expert_id": expert_id, "outcome": outcome, "note": note}
        self._ledger.append(row)
        return len(self._ledger)

    def ledger_rows(self, expert_id=None):
        if expert_id is None:
            return list(self._ledger)
        return [r for r in self._ledger if r.get("expert_id") == expert_id]

    def ledger_summary(self):
        return list(self.ledger_summary_rows)

    # -- contracts / verdicts --------------------------------------------------
    def latest_verdict(self, contract_id):
        return self.verdicts.get(contract_id)

    def latest_open_contract(self):
        return self.open_contract

    def append_observation(self, contract_id, obs):
        self.observations.setdefault(contract_id, []).append(obs)

    def get_contract(self, contract_id):
        return None

    def record_expert_result(self, expert_id, category, won):
        return None


CONTRACT = {"id": "c1", "domain": "code.game", "tags": ["perf"]}


class TestDeriveOutcomeCause(unittest.TestCase):
    def test_measured_alloc_failure(self):
        gate_details = {
            "measurements": [{
                "candidate_id": "cand-a", "metric": "alloc_bytes_per_tick",
                "value": 512, "op": "<=", "threshold": 0, "pass": False,
            }],
            "candidate_experts": {"cand-a": "codex"},
            "hard_failures": [], "judged": None, "winner_expert": None,
            "loser_experts": ["codex"], "escalation_reason": None,
        }
        outcome, cause, rule, route_change, evidence = lessons.derive_outcome_cause(
            CONTRACT, {"contract_id": "c1"}, gate_details
        )
        self.assertEqual(
            outcome,
            "cand-a failed the measured gate: alloc_bytes_per_tick 512, "
            "threshold <= 0",
        )
        self.assertEqual(cause, "per-tick allocation in the hot loop")
        self.assertEqual(
            rule,
            "pre-allocate buffers; check hot-loop allocations before the judged gate",
        )
        self.assertEqual(
            route_change,
            {"category": "code", "demote": "codex", "reason": "lost on measured gate"},
        )
        self.assertEqual(
            evidence, ["verdict:c1", "measurement:cand-a:alloc_bytes_per_tick"]
        )

    def test_measured_ops_and_tick_mappings(self):
        for metric, cause_part, rule_part in [
            ("ops_per_tick", "per-tick recomputation", "O(1)"),
            ("tick_ms_p95", "per-frame work over the tick budget", "profile the hot loop"),
        ]:
            gate_details = {
                "measurements": [{
                    "candidate_id": "cand-a", "metric": metric, "value": 9,
                    "op": "<", "threshold": 1, "pass": False,
                }],
                "candidate_experts": {"cand-a": "grok"},
            }
            _, cause, rule, route_change, _ = lessons.derive_outcome_cause(
                CONTRACT, {}, gate_details
            )
            self.assertIn(cause_part, cause)
            self.assertIn(rule_part, rule)
            self.assertEqual(route_change["demote"], "grok")

    def test_measured_unknown_metric_defaults(self):
        gate_details = {
            "measurements": [{
                "candidate_id": "cand-a", "metric": "mystery_metric",
                "value": 3, "op": ">", "threshold": 1, "pass": False,
            }],
            "candidate_experts": {},
        }
        _, cause, rule, route_change, _ = lessons.derive_outcome_cause(
            CONTRACT, {}, gate_details
        )
        self.assertEqual(cause, "measured metric mystery_metric breached its threshold")
        self.assertIn("mystery_metric", rule)
        self.assertIsNone(route_change)  # failing expert unknown

    def test_hard_failure(self):
        gate_details = {
            "measurements": [],
            "hard_failures": [{
                "candidate_id": "cand-b", "cmd": "pytest -q", "exit_code": 1,
            }],
            "candidate_experts": {"cand-b": "grok"},
        }
        outcome, cause, rule, route_change, evidence = lessons.derive_outcome_cause(
            CONTRACT, {"contract_id": "c1"}, gate_details
        )
        self.assertIn("pytest -q", outcome)
        self.assertIn("1", outcome)
        self.assertEqual(cause, "acceptance command failed")
        self.assertEqual(
            rule,
            "run the contract's acceptance commands locally before submitting the candidate",
        )
        self.assertEqual(
            route_change,
            {"category": "code", "demote": "grok", "reason": "lost on hard gate"},
        )
        self.assertIn("verdict:c1", evidence)

    def test_escalation(self):
        gate_details = {"measurements": [], "hard_failures": [],
                        "escalation_reason": "judges split beyond threshold"}
        outcome, cause, rule, route_change, evidence = lessons.derive_outcome_cause(
            CONTRACT, {"contract_id": "c1"}, gate_details
        )
        self.assertEqual(
            outcome, "contract c1 escalated: judges split beyond threshold"
        )
        self.assertEqual(cause, "judges split beyond threshold")
        self.assertEqual(rule, "operator review required; no automatic route change")
        self.assertIsNone(route_change)

    def test_clean_win(self):
        gate_details = {
            "measurements": [], "hard_failures": [],
            "winner_expert": "claude", "judged": {"median": 0.9},
            "escalation_reason": None,
        }
        outcome, cause, rule, route_change, evidence = lessons.derive_outcome_cause(
            CONTRACT, {"contract_id": "c1", "winner": "claude"}, gate_details
        )
        self.assertEqual(outcome, "claude won contract c1")
        self.assertIn("decided by the gate stack", cause)
        self.assertIn("0.9", cause)
        self.assertEqual(rule, "no failure recorded; keep current route")
        self.assertIsNone(route_change)


class TestWriteLessonAndContext(unittest.TestCase):
    def test_write_lesson_appends_lessons_md(self):
        store = FakeStore()
        gate_details = {
            "measurements": [{
                "candidate_id": "cand-a", "metric": "alloc_bytes_per_tick",
                "value": 128, "op": "<=", "threshold": 0, "pass": False,
            }],
            "candidate_experts": {"cand-a": "codex"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            lesson = lessons.write_lesson(
                store, data_dir, CONTRACT, {"contract_id": "c1"}, gate_details
            )
            self.assertEqual(store.count_lessons(), 1)
            self.assertEqual(lesson["domain"], "code.game")
            self.assertEqual(lesson["written_to"], "plugin_store")
            text = (data_dir / "lessons.md").read_text(encoding="utf-8")
            self.assertIn(
                "# Hybrid Critic Loop — lessons (plugin store)", text
            )
            self.assertIn(lesson["id"], text)
            self.assertIn(
                "pre-allocate buffers; check hot-loop allocations", text
            )
            self.assertIn("verdict:c1", text)
            # Second lesson appends without duplicating the header.
            lessons.write_lesson(
                store, data_dir, CONTRACT, {"contract_id": "c1"}, gate_details
            )
            text2 = (data_dir / "lessons.md").read_text(encoding="utf-8")
            self.assertEqual(
                text2.count("# Hybrid Critic Loop — lessons (plugin store)"), 1
            )
            self.assertEqual(store.count_lessons(), 2)

    def test_lessons_context_empty(self):
        store = FakeStore()
        self.assertEqual(lessons.lessons_context(store, "code"), "")

    def test_lessons_context_format(self):
        store = FakeStore()
        store.create_lesson(
            contract_id="c1", domain="code", tags=[], outcome="cand-a failed",
            cause="x", rule="pre-allocate buffers", route_change=None,
            evidence=["verdict:c1"], written_to="plugin_store",
        )
        ctx = lessons.lessons_context(store, "code")
        self.assertIn(
            "## Lessons from prior Hybrid Critic Loop runs (domain: code)", ctx
        )
        self.assertIn("RULE: pre-allocate buffers", ctx)
        self.assertIn("verdict:c1", ctx)

    def test_lessons_context_cap(self):
        store = FakeStore()
        for i in range(200):
            store.create_lesson(
                contract_id=f"c{i}", domain="code", tags=[],
                outcome="some candidate failed the measured gate " * 3,
                cause="x", rule="a fairly long rule about hot loops " * 3,
                route_change=None, evidence=[f"verdict:c{i}"],
                written_to="plugin_store",
            )
        ctx = lessons.lessons_context(store, "code", limit=200)
        self.assertLessEqual(len(ctx), 10000)
        self.assertIn("more lessons not shown", ctx)
        self.assertIn("10000-char injection cap", ctx)


class TestRouter(unittest.TestCase):
    def test_category_of(self):
        self.assertEqual(router.category_of("code.game"), "code")
        self.assertEqual(router.category_of("code"), "code")
        self.assertEqual(router.category_of(""), "code")

    def test_ensure_and_route_for(self):
        store = FakeStore()
        router.ensure_routes(store)
        route = router.route_for(store, "code")
        self.assertEqual(route["primary"], "codex")
        self.assertEqual(route["compete_with"], "grok")
        self.assertEqual(
            router.DEFAULT_ROUTES["code"]["rule"],
            "judge.family not in contest.families; panel recomputed on fallback",
        )

    def test_demotion_of_primary_promotes_compete_with(self):
        store = FakeStore()
        router.ensure_routes(store)
        line = router.apply_lesson_route_change(
            store, "code",
            {"demote": "codex", "reason": "lost on measured gate"}, "c1",
        )
        self.assertIsNotNone(line)
        self.assertEqual(
            store.route_demotion_calls,
            [("code", "codex", "grok", "lost on measured gate")],
        )
        self.assertIn("codex", line)
        self.assertIn("grok", line)
        self.assertEqual(len(store.add_ledger_calls), 1)
        call = store.add_ledger_calls[0]
        self.assertEqual(call["expert_id"], "codex")
        self.assertEqual(call["outcome"], "observed")
        self.assertIn("route_change demote codex", call["note"])
        self.assertIn("c1", call["note"])
        # Route actually updated in the store.
        self.assertEqual(store.get_route("code")["primary"], "grok")

    def test_demotion_of_non_primary_keeps_primary_as_promote(self):
        store = FakeStore()
        router.ensure_routes(store)
        router.apply_lesson_route_change(
            store, "code", {"demote": "claude", "reason": "lost on hard gate"},
            "c2",
        )
        self.assertEqual(
            store.route_demotion_calls,
            [("code", "claude", "codex", "lost on hard gate")],
        )

    def test_no_route_change_returns_none(self):
        store = FakeStore()
        router.ensure_routes(store)
        self.assertIsNone(
            router.apply_lesson_route_change(store, "code", None, "c1")
        )
        self.assertIsNone(
            router.apply_lesson_route_change(store, "code", {}, "c1")
        )
        self.assertIsNone(
            router.apply_lesson_route_change(
                store, "code", {"reason": "no demote named"}, "c1"
            )
        )
        self.assertEqual(store.route_demotion_calls, [])
        self.assertEqual(store.add_ledger_calls, [])


class TestReports(unittest.TestCase):
    def test_not_run_strings(self):
        store = FakeStore()
        self.assertEqual(reports.ledger_report(store), "ledger: not run")
        self.assertEqual(reports.verdict_report(store, "cX"), "verdict: not run")
        self.assertEqual(reports.lessons_report(store), "lessons: not run")

    def test_ledger_report_populated(self):
        store = FakeStore()
        store.add_ledger(expert_id="codex", outcome="completed", note="x")
        store.ledger_summary_rows = [{
            "expert_id": "codex", "dispatches": 4, "completed": 3,
            "refused": 0, "timeout": 1, "tokens_in": None, "tokens_out": None,
            "cost_usd": None, "wins": 3, "losses": 1, "win_rate": 0.75,
        }]
        text = reports.ledger_report(store)
        self.assertIn("codex", text)
        self.assertIn("dispatches=4", text)
        self.assertIn("not reported", text)
        self.assertIn("3W-1L (0.75)", text)

    def test_ledger_report_win_rate_not_run(self):
        store = FakeStore()
        store.add_ledger(expert_id="grok", outcome="observed", note="x")
        store.ledger_summary_rows = [{
            "expert_id": "grok", "dispatches": 1, "completed": 0,
            "refused": 0, "timeout": 0, "tokens_in": 10, "tokens_out": 5,
            "cost_usd": 0.5, "wins": 0, "losses": 0, "win_rate": None,
        }]
        text = reports.ledger_report(store)
        self.assertIn("win_rate=not run", text)
        self.assertIn("cost_usd=0.5", text)

    def test_verdict_report_populated(self):
        store = FakeStore()
        store.verdicts["c1"] = {
            "winner": "claude", "eliminated": ["cand-a"],
            "hard": "pass", "measured": "pass",
            "judged": {"median": 0.9}, "escalated": False,
            "human_gate": "approved",
        }
        text = reports.verdict_report(store, "c1")
        self.assertIn("winner: claude", text)
        self.assertIn("eliminated: cand-a", text)
        self.assertIn("human_gate: approved", text)
        self.assertIn("median=0.9", text)

    def test_lessons_report_populated(self):
        store = FakeStore()
        store.create_lesson(
            contract_id="c1", domain="code", tags=[], outcome="o1",
            cause="x", rule="rule one", route_change=None, evidence=[],
            written_to="plugin_store",
        )
        text = reports.lessons_report(store)
        self.assertIn("rule one", text)
        self.assertIn("o1", text)
        self.assertTrue(text.startswith("1. "))


class TestHooks(unittest.TestCase):
    def test_on_session_start_writes_session_json(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            hooks.on_session_start(
                store, data_dir, domain="code.game", session_id="s1"
            )
            payload = json.loads(
                (data_dir / "session.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["domain"], "code.game")
            self.assertEqual(payload["session_id"], "s1")

    def test_the_module_offers_exactly_the_two_hooks_the_plugin_registers(self):
        # Fix pass Wave 2.10 (review 1C-17): the post_tool_call / post_llm_call
        # observers read fields Hermes never sends and wrote against a
        # contract no finished run leaves open; they are gone, not kept for
        # the count.
        self.assertTrue(callable(hooks.on_session_start))
        self.assertTrue(callable(hooks.pre_llm_call))
        self.assertFalse(hasattr(hooks, "post_tool_call"))
        self.assertFalse(hasattr(hooks, "post_llm_call"))

    def test_pre_llm_call_none_without_lessons(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(hooks.pre_llm_call(store, Path(tmp)))

    def test_pre_llm_call_returns_context_with_lessons(self):
        store = FakeStore()
        store.create_lesson(
            contract_id="c1", domain="code", tags=[], outcome="o",
            cause="x", rule="pre-allocate buffers", route_change=None,
            evidence=["verdict:c1"], written_to="plugin_store",
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = hooks.pre_llm_call(store, Path(tmp))
            self.assertIsNotNone(result)
            self.assertIn("pre-allocate buffers", result["context"])

    def test_pre_llm_call_uses_session_domain(self):
        store = FakeStore()
        store.create_lesson(
            contract_id="c1", domain="code.game", tags=[], outcome="o",
            cause="x", rule="ring buffer rule", route_change=None,
            evidence=["verdict:c1"], written_to="plugin_store",
        )
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            hooks.on_session_start(store, data_dir, domain="code.game")
            result = hooks.pre_llm_call(store, data_dir)
            self.assertIsNotNone(result)
            self.assertIn("ring buffer rule", result["context"])
            self.assertIn("domain: code.game", result["context"])


class TestMirrorNeedsALocation(unittest.TestCase):
    """write_lesson with data_dir=None stores the row and writes no file
    anywhere (fix pass Wave 2.4, packet F3); the loop maps in-memory and
    path-less stores to None."""

    def test_write_lesson_without_a_location_writes_no_file(self):
        import contextlib
        import os
        from hybrid_critic_loop import loop as loop_mod

        store = FakeStore()  # db_path None
        gate_details = {"measurements": [], "hard_failures": [], "candidate_experts": {}}
        with tempfile.TemporaryDirectory() as td, contextlib.chdir(td):
            lesson = lessons.write_lesson(store, None, CONTRACT, {"contract_id": "c1"}, gate_details)
            self.assertEqual(os.listdir(td), [])
        self.assertEqual(lesson["id"], "lsn-1")
        self.assertEqual(store.count_lessons(), 1)
        self.assertIsNone(loop_mod._lesson_data_dir(store))
        store.db_path = ":memory:"
        self.assertIsNone(loop_mod._lesson_data_dir(store))
        store.db_path = "/some/dir/data.db"
        self.assertEqual(loop_mod._lesson_data_dir(store), Path("/some/dir"))


class TestInjectionCapAndTurns(unittest.TestCase):
    """The injected block is filled to the cap and its note counts the true
    remainder; pre_llm_call injects on the first turn only when the host says
    which turn it is; the default domain reaches dotted lessons through the
    real store (fix pass Wave 2.3: F4, H10, 1C-12)."""

    def _seed(self, store, n, domain="code", rule="short rule"):
        for i in range(n):
            store.create_lesson(
                contract_id=f"c{i}", domain=domain, tags=[], outcome=f"o{i}",
                cause="x", rule=f"{rule} {i}", route_change=None,
                evidence=[f"verdict:c{i}"], written_to="plugin_store",
            )

    def test_all_matching_lessons_are_injected_when_they_fit(self):
        store = FakeStore()
        self._seed(store, 7)
        ctx = lessons.lessons_context(store, "code")
        self.assertEqual(sum(line.startswith("- [lsn ") for line in ctx.splitlines()), 7)
        self.assertNotIn("not shown", ctx)

    def test_the_not_shown_note_counts_the_true_remainder(self):
        store = FakeStore()
        self._seed(store, 9)
        line_len = len(lessons._lesson_line(store.lessons_for("code", limit=None)[0]))
        header = len("## Lessons from prior Hybrid Critic Loop runs (domain: code)")
        cap = header + 1 + 3 * (line_len + 1) + 70  # room for three lessons and the note
        ctx = lessons.lessons_context(store, "code", max_chars=cap)
        shown = sum(line.startswith("- [lsn ") for line in ctx.splitlines())
        self.assertGreaterEqual(shown, 2)
        self.assertIn(f"({9 - shown} more lessons not shown", ctx)
        self.assertLessEqual(len(ctx), cap)

    def test_pre_llm_call_injects_on_the_first_turn_only_when_told(self):
        store = FakeStore()
        self._seed(store, 1, rule="first-turn rule")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(hooks.pre_llm_call(store, Path(tmp), is_first_turn=False))
            self.assertIn("first-turn rule", hooks.pre_llm_call(store, Path(tmp), is_first_turn=True)["context"])
            self.assertIn("first-turn rule", hooks.pre_llm_call(store, Path(tmp))["context"])

    def test_default_domain_reaches_dotted_lessons_through_the_real_store(self):
        from hybrid_critic_loop.state import Store

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.db")
            try:
                store.create_lesson(
                    contract_id="c1", domain="code.game", tags=["sampler"], outcome="o",
                    cause="x", rule="ring buffer rule", route_change=None,
                    evidence=["verdict:c1"], written_to="plugin_store")
                store.create_lesson(
                    contract_id="c2", domain="ops", tags=[], outcome="o",
                    cause="x", rule="ops rule", route_change=None,
                    evidence=["verdict:c2"], written_to="plugin_store")
                result = hooks.pre_llm_call(store, Path(tmp))  # no domain: the host never passes one
                self.assertIsNotNone(result)
                self.assertIn("ring buffer rule", result["context"])
                self.assertNotIn("ops rule", result["context"])
                self.assertIn("(domain: code)", result["context"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
