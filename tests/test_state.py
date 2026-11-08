"""Unit tests for hybrid_critic_loop.state.Store (stdlib unittest)."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from hybrid_critic_loop.state import Store

EXPECTED_TABLES = {
    "targets",
    "contracts",
    "candidates",
    "measurements",
    "scores",
    "verdicts",
    "final_answers",
    "experts",
    "routes",
    "ledger",
    "lessons",
    "events",
}


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "nested" / "state.db"
        self.store = Store(self.db_path)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def _target(self):
        return self.store.create_target(
            "do the thing", "code",
            constraints=["no network"], budget={"usd": 5})

    def _contract(self, target_id=None, **kw):
        tid = target_id or self._target()
        args = dict(
            stage="solve", task="fix the bug", domain="code",
            tags=["python", "bug"],
            acceptance=[{"check": "tests pass", "kind": "command"}],
        )
        args.update(kw)
        return self.store.create_contract(tid, **args)

    def _candidate(self, contract_id=None, attempt=1, slot="A"):
        cid = contract_id or self._contract()
        return self.store.create_candidate(
            cid, attempt, "solve", "exp-1", "fam-a", slot,
            f"/tmp/wt-{slot}", "basehash123")


class TestSchema(StoreTestCase):
    def test_exactly_twelve_tables(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%'").fetchall()
        finally:
            conn.close()
        self.assertEqual({r[0] for r in rows}, EXPECTED_TABLES)
        self.assertEqual(len(rows), 12)

    def test_wal_mode(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode.lower(), "wal")


class TestTargetsContracts(StoreTestCase):
    def test_target_round_trip_parsed_json(self):
        tid = self._target()
        self.assertEqual(tid, "tgt_0001")
        got = self.store.get_target(tid)
        self.assertEqual(got["id"], tid)
        self.assertEqual(got["ask"], "do the thing")
        self.assertEqual(got["domain"], "code")
        self.assertEqual(got["constraints"], ["no network"])
        self.assertIsInstance(got["constraints"], list)
        self.assertEqual(got["budget"], {"usd": 5})
        self.assertIsInstance(got["budget"], dict)
        self.assertEqual(got["done_when"], "human_gate")
        self.assertTrue(got["created_at"])

    def test_target_defaults(self):
        tid = self.store.create_target("ask", "research")
        got = self.store.get_target(tid)
        self.assertEqual(got["constraints"], [])
        self.assertIsNone(got["budget"])
        self.assertIsNone(self.store.get_target("tgt_9999"))

    def test_sequential_target_ids(self):
        self.assertEqual(self.store.create_target("a", "d"), "tgt_0001")
        self.assertEqual(self.store.create_target("b", "d"), "tgt_0002")

    def test_contract_round_trip_parsed_json(self):
        tid = self._target()
        cid = self.store.create_contract(
            tid, "solve", "fix it", "code", ["python"],
            [{"check": "pytest", "kind": "command"}],
            depends_on=["ctr_0000"], test_author_family="fam-t",
            repo_path="/repo")
        self.assertEqual(cid, "ctr_0001")
        got = self.store.get_contract(cid)
        self.assertEqual(got["target_id"], tid)
        self.assertEqual(got["stage"], "solve")
        self.assertEqual(got["attempt"], 1)
        self.assertEqual(got["tags"], ["python"])
        self.assertEqual(got["depends_on"], ["ctr_0000"])
        self.assertEqual(got["acceptance"],
                         [{"check": "pytest", "kind": "command"}])
        self.assertIsNone(got["acceptance_baseline_run"])
        self.assertEqual(got["test_author_family"], "fam-t")
        self.assertEqual(got["repo_path"], "/repo")
        self.assertEqual(got["status"], "open")
        self.assertEqual(got["observations"], [])
        self.assertIsNone(self.store.get_contract("ctr_9999"))

    def test_contract_explicit_id(self):
        tid = self._target()
        cid = self.store.create_contract(
            tid, "s", "t", "d", [], [], contract_id="ctr_custom")
        self.assertEqual(cid, "ctr_custom")
        self.assertEqual(self.store.get_contract("ctr_custom")["id"],
                         "ctr_custom")

    def test_baseline_status_and_observations(self):
        cid = self._contract()
        self.store.set_baseline_run(cid, "fail")
        self.assertEqual(
            self.store.get_contract(cid)["acceptance_baseline_run"], "fail")
        self.store.set_baseline_run(cid, "pass")
        self.assertEqual(
            self.store.get_contract(cid)["acceptance_baseline_run"], "pass")
        self.store.append_observation(cid, {"note": "one"})
        self.store.append_observation(cid, {"note": "two"})
        self.assertEqual(self.store.get_contract(cid)["observations"],
                         [{"note": "one"}, {"note": "two"}])
        self.store.set_contract_status(cid, "closed")
        self.assertEqual(self.store.get_contract(cid)["status"], "closed")

    def test_observation_cap_50(self):
        cid = self._contract()
        for i in range(60):
            self.store.append_observation(cid, {"n": i})
        obs = self.store.get_contract(cid)["observations"]
        self.assertEqual(len(obs), 50)
        self.assertEqual(obs[0], {"n": 10})
        self.assertEqual(obs[-1], {"n": 59})

    def test_latest_open_contract(self):
        self.assertIsNone(self.store.latest_open_contract())
        c1 = self._contract()
        c2 = self._contract()
        self.assertEqual(self.store.latest_open_contract()["id"], c2)
        self.store.set_contract_status(c2, "closed")
        self.assertEqual(self.store.latest_open_contract()["id"], c1)


class TestCandidates(StoreTestCase):
    def test_candidate_id_attempt1(self):
        cid = self._contract()
        cand = self.store.create_candidate(
            cid, 1, "solve", "exp-1", "fam-a", "A", "/wt", "hash0")
        self.assertEqual(cand, f"{cid}_A")
        got = self.store.get_candidate(cand)
        self.assertEqual(got["contract_id"], cid)
        self.assertEqual(got["attempt"], 1)
        self.assertEqual(got["slot"], "A")
        self.assertEqual(got["expert_id"], "exp-1")
        self.assertEqual(got["baseline_hash"], "hash0")
        self.assertEqual(got["provenance"], "generated")
        self.assertEqual(got["status"], "created")
        self.assertEqual(got["evidence"], {})
        self.assertIsNone(got["payload_hash"])
        self.assertIsNone(got["tokens_in"])

    def test_candidate_id_retry(self):
        cid = self._contract()
        cand2 = self.store.create_candidate(
            cid, 2, "solve", "exp-1", "fam-a", "A", "/wt", "hash0")
        self.assertEqual(cand2, f"{cid}_A_r2")
        cand3 = self.store.create_candidate(
            cid, 3, "solve", "exp-2", "fam-b", "B", "/wt", "hash0")
        self.assertEqual(cand3, f"{cid}_B_r3")

    def test_update_candidate(self):
        cand = self._candidate()
        self.store.update_candidate(
            cand, payload_hash="ph1", evidence={"tests": 3},
            tokens_in=100, tokens_out=50, cost_usd=0.25, status="done",
            provenance="generated")
        got = self.store.get_candidate(cand)
        self.assertEqual(got["payload_hash"], "ph1")
        self.assertEqual(got["evidence"], {"tests": 3})
        self.assertEqual(got["tokens_in"], 100)
        self.assertEqual(got["tokens_out"], 50)
        self.assertAlmostEqual(got["cost_usd"], 0.25)
        self.assertEqual(got["status"], "done")
        with self.assertRaises(ValueError):
            self.store.update_candidate(cand, not_a_field=1)

    def test_candidates_for_ordered_by_slot(self):
        cid = self._contract()
        self._candidate(cid, slot="B")
        self._candidate(cid, slot="A")
        self._candidate(cid, slot="C")
        got = self.store.candidates_for(cid)
        self.assertEqual([c["slot"] for c in got], ["A", "B", "C"])
        self.assertEqual(self.store.candidates_for(cid, attempt=2), [])
        retry = self._candidate(cid, attempt=2, slot="A")
        self.assertEqual(
            [c["id"] for c in self.store.candidates_for(cid, attempt=2)],
            [retry])


class TestMeasurementsScores(StoreTestCase):
    def test_measurements(self):
        cand = self._candidate()
        self.store.add_measurement(cand, "pytest", "pass_rate", 1.0,
                                   ">=", 1.0, True)
        self.store.add_measurement(cand, "lint", "errors", 2, "==", 0, False)
        rows = self.store.measurements_for(cand)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["sensor"], "pytest")
        self.assertEqual(rows[0]["metric"], "pass_rate")
        self.assertEqual(rows[0]["value"], 1.0)
        self.assertEqual(rows[0]["op"], ">=")
        self.assertEqual(rows[0]["threshold"], 1.0)
        self.assertIs(rows[0]["pass"], True)
        self.assertIs(rows[1]["pass"], False)
        self.assertEqual(rows[1]["value"], 2)
        self.assertEqual(self.store.measurements_for("nope"), [])

    def test_scores(self):
        cand = self._candidate()
        self.store.add_score(cand, "judged", "rubric-v1", "judge-1",
                             "fam-j", True, 2, {"clarity": 4}, 4.0)
        rows = self.store.scores_for(cand)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["judge_id"], "judge-1")
        self.assertIs(rows[0]["blind"], True)
        self.assertEqual(rows[0]["position_shown"], 2)
        self.assertEqual(rows[0]["items"], {"clarity": 4})
        self.assertAlmostEqual(rows[0]["mean"], 4.0)


class TestVerdicts(StoreTestCase):
    def test_verdict_round_trip(self):
        cid = self._contract()
        vid = self.store.create_verdict(
            cid, 1, winner=f"{cid}_A", eliminated=[f"{cid}_B"],
            hard="pass", measured="pass",
            judged={"winner": "A", "margin": 0.5}, escalated=False,
            escalation_reason=None, waiver=None, human_gate="pending",
            tie_break=None)
        self.assertEqual(vid, "vrd_0001")
        got = self.store.get_verdict(cid, 1)
        self.assertEqual(got["id"], vid)
        self.assertEqual(got["winner"], f"{cid}_A")
        self.assertEqual(got["eliminated"], [f"{cid}_B"])
        self.assertEqual(got["judged"], {"winner": "A", "margin": 0.5})
        self.assertIs(got["escalated"], False)
        self.assertIsNone(got["waiver"])
        self.assertEqual(got["human_gate"], "pending")
        self.assertIsNone(self.store.get_verdict(cid, 99))

    def test_latest_verdict(self):
        cid = self._contract()
        self.store.create_verdict(
            cid, 1, "w1", [], "pass", "pass", None, True, "tie", None,
            "pending", "coin")
        v2 = self.store.create_verdict(
            cid, 2, "w2", [], "pass", "pass", None, False, None,
            {"reason": "ok"}, "approved", None)
        latest = self.store.latest_verdict(cid)
        self.assertEqual(latest["id"], v2)
        self.assertEqual(latest["waiver"], {"reason": "ok"})
        self.assertIsNone(self.store.latest_verdict("ctr_nope"))

    def test_route_hold_round_trip(self):
        # Fix pass Wave 2.1: a route change held on a degraded verdict is a
        # verdict-row fact, NULL until recorded.
        cid = self._contract()
        vid = self.store.create_verdict(
            cid, 1, "w1", [], "pass", "fail", {"panel_status": "degraded"}, False,
            None, None, "pending", "bracket_validity")
        self.assertIsNone(self.store.latest_verdict(cid)["route_hold"])
        hold = {"status": "held", "panel_status": "degraded", "lesson_id": "lsn_0001",
                "route_change": {"category": "code", "demote": "x", "reason": "lost on measured gate"}}
        self.store.set_verdict_route_hold(vid, hold)
        self.assertEqual(self.store.latest_verdict(cid)["route_hold"], hold)
        self.assertEqual(self.store.get_verdict(cid, 1)["route_hold"]["route_change"]["demote"], "x")
        self.store.set_verdict_route_hold(vid, None)
        self.assertIsNone(self.store.latest_verdict(cid)["route_hold"])


class TestFinalAnswer(StoreTestCase):
    def test_create_final_answer(self):
        tid = self._target()
        cid = self._contract(tid)
        fid = self.store.create_final_answer(
            tid, cid, "abc123", intent_check={"ok": True},
            human_gate="approved")
        self.assertIsInstance(fid, int)


class TestExperts(StoreTestCase):
    def test_upsert_and_get(self):
        self.store.upsert_expert("exp-1", "fam-a", "cli", "subscription")
        got = self.store.get_expert("exp-1")
        self.assertEqual(got["family"], "fam-a")
        self.assertEqual(got["access"], "cli")
        self.assertEqual(got["cost_tier"], "subscription")
        self.assertIsNone(got["cost_tier_note"])
        self.assertEqual(got["category_scores"], {})
        self.assertEqual(got["wins"], 0)
        self.assertEqual(got["losses"], 0)
        self.assertEqual([e["id"] for e in self.store.list_experts()],
                         ["exp-1"])
        self.assertIsNone(self.store.get_expert("nope"))

    def test_cost_tier_enum_rejection(self):
        with self.assertRaises(ValueError):
            self.store.upsert_expert("exp-x", "fam", "cli",
                                     "free (unverified)")
        with self.assertRaises(ValueError):
            self.store.upsert_expert("exp-x", "fam", "cli", "cheap")
        # qualifier belongs in the note, and then it is accepted
        self.store.upsert_expert("exp-x", "fam", "cli", "free",
                                 cost_tier_note="free (unverified)")
        got = self.store.get_expert("exp-x")
        self.assertEqual(got["cost_tier"], "free")
        self.assertEqual(got["cost_tier_note"], "free (unverified)")

    def test_win_rate_computation(self):
        self.store.upsert_expert("exp-1", "fam-a", "cli", "api")
        self.store.record_expert_result("exp-1", "code", True)
        self.store.record_expert_result("exp-1", "code", True)
        self.store.record_expert_result("exp-1", "code", False)
        self.store.record_expert_result("exp-1", "prose", True)
        got = self.store.get_expert("exp-1")
        self.assertEqual(got["wins"], 3)
        self.assertEqual(got["losses"], 1)
        self.assertAlmostEqual(got["category_scores"]["code"], 2 / 3)
        self.assertAlmostEqual(got["category_scores"]["prose"], 1.0)
        self.assertTrue(got["last_scored"])

    def test_refusal_rate(self):
        self.store.upsert_expert("exp-1", "fam-a", "cli", "api")
        self.store.record_expert_result("exp-1", "code", True)
        self.store.record_refusal("exp-1")
        got = self.store.get_expert("exp-1")
        # 1 refusal over 2 dispatches (1 result + 1 refusal)
        self.assertAlmostEqual(got["refusal_rate"], 0.5)
        self.store.record_refusal("exp-1")
        got = self.store.get_expert("exp-1")
        self.assertAlmostEqual(got["refusal_rate"], 2 / 3)

    def test_record_result_unknown_expert(self):
        with self.assertRaises(KeyError):
            self.store.record_expert_result("ghost", "code", True)
        with self.assertRaises(KeyError):
            self.store.record_refusal("ghost")


class TestRoutes(StoreTestCase):
    def test_upsert_and_get_route(self):
        self.store.upsert_route("code", "exp-a", "exp-b", ["exp-c"],
                                ["judge-1"], "best of panel")
        got = self.store.get_route("code")
        self.assertEqual(got["primary"], "exp-a")
        self.assertEqual(got["compete_with"], "exp-b")
        self.assertEqual(got["fallback"], ["exp-c"])
        self.assertEqual(got["judges"], ["judge-1"])
        self.assertEqual(got["rule"], "best of panel")
        self.assertTrue(got["updated_at"])
        self.assertIsNone(self.store.get_route("nope"))

    def test_demotion_primary_branch(self):
        self.store.upsert_route("code", "exp-a", "exp-b", ["exp-c"],
                                ["j"], "r")
        new = self.store.apply_route_demotion("code", "exp-a", "exp-c",
                                              "exp-a kept losing")
        self.assertEqual(new["primary"], "exp-c")
        self.assertEqual(new["compete_with"], "exp-a")
        self.assertEqual(self.store.get_route("code")["primary"], "exp-c")

    def test_demotion_compete_with_branch(self):
        self.store.upsert_route("code", "exp-a", "exp-b", ["exp-c"],
                                ["j"], "r")
        new = self.store.apply_route_demotion("code", "exp-b", "exp-d",
                                              "exp-b refused")
        self.assertEqual(new["primary"], "exp-a")
        self.assertEqual(new["compete_with"], "exp-d")
        self.assertEqual(new["fallback"], ["exp-b", "exp-c"])


class TestLedger(StoreTestCase):
    def test_add_and_rows_insertion_order(self):
        tid = self._target()
        cid = self._contract(tid)
        self.store.add_ledger(tid, cid, None, "solve", "exp-1", 10, 5,
                              0.1, "completed")
        self.store.add_ledger(tid, cid, None, "solve", "exp-1", None,
                              None, None, "refused", note="no")
        rows = self.store.ledger_rows()
        self.assertEqual([r["outcome"] for r in rows],
                         ["completed", "refused"])
        self.assertEqual(
            [r["outcome"] for r in self.store.ledger_rows("exp-1")],
            ["completed", "refused"])
        self.assertEqual(self.store.ledger_rows("exp-2"), [])
        with self.assertRaises(ValueError):
            self.store.add_ledger(tid, cid, None, "solve", "exp-1",
                                  None, None, None, "bogus")
        # local extensions are accepted
        self.store.add_ledger(tid, cid, None, "solve", "exp-1", None,
                              None, None, "escalated")
        self.store.add_ledger(tid, cid, None, "solve", "exp-1", None,
                              None, None, "observed")

    def test_summary_none_handling(self):
        tid = self._target()
        cid = self._contract(tid)
        # exp-none: nothing reported -> sums must be None, not 0
        self.store.add_ledger(tid, cid, None, "solve", "exp-none", None,
                              None, None, "completed")
        self.store.add_ledger(tid, cid, None, "solve", "exp-none", None,
                              None, None, "timeout")
        # exp-num: real numbers, plus expert win/loss record
        self.store.upsert_expert("exp-num", "fam", "api", "api")
        self.store.record_expert_result("exp-num", "code", True)
        self.store.record_expert_result("exp-num", "code", False)
        self.store.add_ledger(tid, cid, None, "solve", "exp-num", 100,
                              40, 1.5, "completed")
        self.store.add_ledger(tid, cid, None, "solve", "exp-num", 50,
                              None, 0.5, "refused")
        summary = {s["expert_id"]: s for s in self.store.ledger_summary()}
        none_row = summary["exp-none"]
        self.assertEqual(none_row["dispatches"], 2)
        self.assertEqual(none_row["completed"], 1)
        self.assertEqual(none_row["timeout"], 1)
        self.assertEqual(none_row["refused"], 0)
        self.assertIsNone(none_row["tokens_in"])
        self.assertIsNone(none_row["tokens_out"])
        self.assertIsNone(none_row["cost_usd"])
        self.assertIsNone(none_row["win_rate"])
        num_row = summary["exp-num"]
        self.assertEqual(num_row["tokens_in"], 150)
        self.assertEqual(num_row["tokens_out"], 40)
        self.assertAlmostEqual(num_row["cost_usd"], 2.0)
        self.assertEqual(num_row["wins"], 1)
        self.assertEqual(num_row["losses"], 1)
        self.assertAlmostEqual(num_row["win_rate"], 0.5)


class TestLessons(StoreTestCase):
    def _lesson(self, domain="code", tags=("python",), rule="r"):
        return self.store.create_lesson(
            "ctr_0001", domain, list(tags), "fail", "cause-x", rule,
            None, ["ev-1"], "memory")

    def test_create_get_count(self):
        lid = self._lesson()
        self.assertEqual(lid, "lsn_0001")
        got = self.store.get_lesson(lid)
        self.assertEqual(got["tags"], ["python"])
        self.assertEqual(got["evidence"], ["ev-1"])
        self.assertEqual(got["provenance"], "generated")
        self.assertIsNone(got["route_change"])
        self.assertEqual(self.store.count_lessons(), 1)
        self.assertIsNone(self.store.get_lesson("lsn_9999"))

    def test_ranking_tag_overlap_beats_recency(self):
        older_high = self.store.create_lesson(
            "ctr_0001", "code", ["python", "testing"], "fail", "c",
            "older-high", None, [], "memory")
        newer_low = self.store.create_lesson(
            "ctr_0001", "code", ["python"], "fail", "c", "newer-low",
            None, [], "memory")
        got = self.store.lessons_for(tags=["python", "testing"])
        self.assertEqual([l["id"] for l in got], [older_high, newer_low])

    def test_ranking_newest_first_within_same_overlap(self):
        first = self._lesson(rule="first")
        second = self._lesson(rule="second")
        got = self.store.lessons_for(tags=["python"])
        self.assertEqual([l["id"] for l in got], [second, first])

    def test_domain_filter_and_limit(self):
        self._lesson(domain="code")
        self._lesson(domain="prose")
        self._lesson(domain="code")
        self.assertEqual(len(self.store.lessons_for(domain="code")), 2)
        self.assertEqual(len(self.store.lessons_for(domain="prose")), 1)
        self.assertEqual(len(self.store.lessons_for(limit=2)), 2)
        self.assertEqual(self.store.count_lessons(), 3)


class TestScoresV03(StoreTestCase):
    def test_confidence_and_details_round_trip(self):
        cand = self._candidate()
        details = {
            "row-a": {"score": 8, "confidence": 0.9,
                      "evidence": "ev-1", "rationale": "solid"},
            "row-b": {"score": 6, "confidence": 0.5,
                      "evidence": "ev-2", "rationale": "partial"},
        }
        self.store.add_score(
            cand, "validity", "peer-matrix-v1", "judge-1", "fam-j",
            True, 1, {"row-a": 8, "row-b": 6}, 7.0,
            confidence=0.7, details=details)
        rows = self.store.scores_for(cand)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["confidence"], 0.7)
        self.assertEqual(rows[0]["details"], details)

    def test_confidence_details_defaults(self):
        cand = self._candidate()
        # positional call exactly as v1 callers make it
        self.store.add_score(cand, "judged", "rubric-v1", "judge-1",
                             "fam-j", True, 2, {"clarity": 4}, 4.0)
        rows = self.store.scores_for(cand)
        self.assertIsNone(rows[0]["confidence"])
        self.assertEqual(rows[0]["details"], {})


class TestMigrationV03(unittest.TestCase):
    """A hand-built v1-shaped DB must open cleanly under v0.3."""

    _V1_SCORES = """
    CREATE TABLE scores (
        id INTEGER PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        gate TEXT,
        rubric TEXT,
        judge_id TEXT,
        judge_family TEXT,
        blind INTEGER NOT NULL DEFAULT 0,
        position_shown INTEGER,
        items_json TEXT NOT NULL DEFAULT '{}',
        mean REAL,
        created_at TEXT NOT NULL
    );
    """

    _V1_EXPERTS = """
    CREATE TABLE experts (
        id TEXT PRIMARY KEY,
        family TEXT,
        access TEXT,
        cost_tier TEXT NOT NULL,
        cost_tier_note TEXT,
        category_scores_json TEXT NOT NULL DEFAULT '{}',
        category_record_json TEXT NOT NULL DEFAULT '{}',
        wins INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0,
        judge_calibration REAL,
        refusal_rate REAL NOT NULL DEFAULT 0.0,
        refusals INTEGER NOT NULL DEFAULT 0,
        dispatches INTEGER NOT NULL DEFAULT 0,
        last_scored TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """

    def test_v1_db_migrates(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db_path = Path(tmp.name) / "v1.db"
            conn = sqlite3.connect(str(db_path))
            try:
                conn.executescript(self._V1_SCORES + self._V1_EXPERTS)
                conn.execute(
                    "INSERT INTO scores (candidate_id, gate, rubric,"
                    " judge_id, judge_family, blind, position_shown,"
                    " items_json, mean, created_at) VALUES"
                    " ('cand-old', 'judged', 'r', 'j-1', 'fam', 1, 1,"
                    " '{}', 5.0, '2026-01-01T00:00:00+00:00')")
                conn.commit()
            finally:
                conn.close()

            store = Store(db_path)
            try:
                check = sqlite3.connect(str(db_path))
                try:
                    score_cols = {r[1] for r in check.execute(
                        "PRAGMA table_info(scores)").fetchall()}
                    expert_cols = {r[1] for r in check.execute(
                        "PRAGMA table_info(experts)").fetchall()}
                finally:
                    check.close()
                self.assertIn("confidence", score_cols)
                self.assertIn("details_json", score_cols)
                self.assertIn("validity_points", expert_cols)
                self.assertIn("validity_cost_usd", expert_cols)
                self.assertIn("calibration_n", expert_cols)
                # pre-existing v1 row reads back with v0.3 defaults
                old = store.scores_for("cand-old")
                self.assertEqual(len(old), 1)
                self.assertIsNone(old[0]["confidence"])
                self.assertEqual(old[0]["details"], {})
                # and new writes round-trip on the migrated DB
                details = {"row-a": {"score": 9, "confidence": 0.8,
                                     "evidence": "ev",
                                     "rationale": "ok"}}
                store.add_score(
                    "cand-old", "validity", "peer-matrix-v1", "j-2",
                    "fam", True, 1, {"row-a": 9}, 9.0,
                    confidence=0.7, details=details)
                rows = store.scores_for("cand-old")
                self.assertEqual(len(rows), 2)
                self.assertAlmostEqual(rows[1]["confidence"], 0.7)
                self.assertEqual(rows[1]["details"], details)
            finally:
                store.close()
        finally:
            tmp.cleanup()


class TestEvents(StoreTestCase):
    def test_add_and_read_round_trip_newest_first(self):
        cid = self._contract()
        first = self.store.add_event(cid, "waiver", "operator waived gate")
        second = self.store.add_event(cid, "note", "second event")
        self.assertIsInstance(first, int)
        self.assertGreater(second, first)
        rows = self.store.events_for(cid)
        self.assertEqual([r["id"] for r in rows], [second, first])
        self.assertEqual(rows[0]["contract_id"], cid)
        self.assertEqual(rows[0]["type"], "note")
        self.assertEqual(rows[0]["detail"], "second event")
        self.assertTrue(rows[0]["created_at"])
        self.assertEqual(rows[1]["type"], "waiver")
        self.assertEqual(rows[1]["detail"], "operator waived gate")

    def test_per_contract_filter_and_all(self):
        cid = self._contract()
        other = self._contract()
        self.store.add_event(cid, "note", "for first")
        self.store.add_event(other, "note", "for second")
        self.store.add_event(cid, "note", "also first")
        self.assertEqual(len(self.store.events_for(cid)), 2)
        self.assertEqual(len(self.store.events_for(other)), 1)
        self.assertEqual(
            [r["detail"] for r in self.store.events_for(other)],
            ["for second"])
        self.assertEqual(len(self.store.events_for()), 3)
        self.assertEqual(self.store.events_for("ctr_nope"), [])

    def test_events_limit(self):
        cid = self._contract()
        for i in range(5):
            self.store.add_event(cid, "note", f"event-{i}")
        rows = self.store.events_for(cid, limit=2)
        self.assertEqual([r["detail"] for r in rows],
                         ["event-4", "event-3"])


class TestExpertValidity(StoreTestCase):
    def test_defaults_are_none(self):
        self.store.upsert_expert("exp-1", "fam-a", "cli", "api")
        got = self.store.get_expert("exp-1")
        self.assertIsNone(got["validity_points"])
        self.assertIsNone(got["validity_cost_usd"])
        self.assertIsNone(got["cost_per_validity_point"])
        self.assertIsNone(got["calibration_n"])

    def test_record_validity_cost_none_leaves_cost_null(self):
        self.store.upsert_expert("exp-1", "fam-a", "cli", "api")
        self.store.record_validity("exp-1", 10.0, None)
        got = self.store.get_expert("exp-1")
        self.assertAlmostEqual(got["validity_points"], 10.0)
        self.assertIsNone(got["validity_cost_usd"])
        self.assertIsNone(got["cost_per_validity_point"])
        # a second cost-less accumulation still leaves cost NULL
        self.store.record_validity("exp-1", 5.0, None)
        got = self.store.get_expert("exp-1")
        self.assertAlmostEqual(got["validity_points"], 15.0)
        self.assertIsNone(got["validity_cost_usd"])
        self.assertIsNone(got["cost_per_validity_point"])

    def test_cost_per_validity_point_math_exact(self):
        self.store.upsert_expert("exp-1", "fam-a", "cli", "api")
        self.store.record_validity("exp-1", 20.0, 1.0)
        got = self.store.get_expert("exp-1")
        self.assertAlmostEqual(got["validity_points"], 20.0)
        self.assertAlmostEqual(got["validity_cost_usd"], 1.0)
        self.assertAlmostEqual(got["cost_per_validity_point"], 0.05)

    def test_record_validity_accumulates_points_and_cost(self):
        self.store.upsert_expert("exp-1", "fam-a", "cli", "api")
        self.store.record_validity("exp-1", 10.0, 0.5)
        self.store.record_validity("exp-1", 10.0, 0.5)
        self.store.record_validity("exp-1", 5.0, None)
        got = self.store.get_expert("exp-1")
        self.assertAlmostEqual(got["validity_points"], 25.0)
        self.assertAlmostEqual(got["validity_cost_usd"], 1.0)
        self.assertAlmostEqual(got["cost_per_validity_point"], 0.04)

    def test_record_validity_unknown_expert(self):
        with self.assertRaises(KeyError):
            self.store.record_validity("ghost", 1.0, 0.1)

    def test_update_expert_calibration(self):
        self.store.upsert_expert("exp-1", "fam-a", "cli", "api")
        self.store.update_expert_calibration("exp-1", 0.87, 42)
        got = self.store.get_expert("exp-1")
        self.assertAlmostEqual(got["judge_calibration"], 0.87)
        self.assertEqual(got["calibration_n"], 42)
        with self.assertRaises(KeyError):
            self.store.update_expert_calibration("ghost", 0.5, 1)

    def test_upsert_does_not_clobber_validity_or_calibration(self):
        self.store.upsert_expert("exp-1", "fam-a", "cli", "api")
        self.store.record_validity("exp-1", 20.0, 1.0)
        self.store.update_expert_calibration("exp-1", 0.5, 7)
        self.store.upsert_expert("exp-1", "fam-b", "api", "subscription",
                                 cost_tier_note="changed")
        got = self.store.get_expert("exp-1")
        self.assertEqual(got["family"], "fam-b")
        self.assertAlmostEqual(got["validity_points"], 20.0)
        self.assertAlmostEqual(got["validity_cost_usd"], 1.0)
        self.assertAlmostEqual(got["cost_per_validity_point"], 0.05)
        self.assertAlmostEqual(got["judge_calibration"], 0.5)
        self.assertEqual(got["calibration_n"], 7)


class TestLedgerSummaryValidity(StoreTestCase):
    def test_summary_carries_validity_keys(self):
        tid = self._target()
        cid = self._contract(tid)
        self.store.upsert_expert("exp-num", "fam", "api", "api")
        self.store.record_validity("exp-num", 20.0, 1.0)
        self.store.upsert_expert("exp-none", "fam", "api", "api")
        self.store.add_ledger(tid, cid, None, "solve", "exp-num", 10, 5,
                              0.1, "completed")
        self.store.add_ledger(tid, cid, None, "solve", "exp-none", 10, 5,
                              0.1, "completed")
        self.store.add_ledger(tid, cid, None, "solve", "exp-ghost", 10, 5,
                              0.1, "completed")
        summary = {s["expert_id"]: s for s in self.store.ledger_summary()}
        num_row = summary["exp-num"]
        self.assertAlmostEqual(num_row["validity_points"], 20.0)
        self.assertAlmostEqual(num_row["validity_cost_usd"], 1.0)
        self.assertAlmostEqual(num_row["cost_per_validity_point"], 0.05)
        none_row = summary["exp-none"]
        self.assertIsNone(none_row["validity_points"])
        self.assertIsNone(none_row["validity_cost_usd"])
        self.assertIsNone(none_row["cost_per_validity_point"])
        ghost_row = summary["exp-ghost"]
        self.assertIsNone(ghost_row["validity_points"])
        self.assertIsNone(ghost_row["validity_cost_usd"])
        self.assertIsNone(ghost_row["cost_per_validity_point"])


class TestVerdictHoldMigration(unittest.TestCase):
    """A v0.3 database (verdicts without route_hold_json) opens and gains the
    column; its old rows read back with route_hold None (fix pass Wave 2.1)."""

    _V03_VERDICTS = """
    CREATE TABLE verdicts (
        id TEXT PRIMARY KEY,
        contract_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        winner TEXT,
        eliminated_json TEXT NOT NULL DEFAULT '[]',
        hard TEXT,
        measured TEXT,
        judged_json TEXT,
        escalated INTEGER NOT NULL DEFAULT 0,
        escalation_reason TEXT,
        waiver_json TEXT,
        human_gate TEXT,
        tie_break TEXT,
        created_at TEXT NOT NULL
    );
    """

    def test_v03_verdicts_table_gains_route_hold(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db_path = Path(tmp.name) / "v03.db"
            conn = sqlite3.connect(str(db_path))
            try:
                conn.executescript(self._V03_VERDICTS)
                conn.execute(
                    "INSERT INTO verdicts (id, contract_id, attempt, winner, hard,"
                    " measured, escalated, human_gate, created_at) VALUES"
                    " ('vrd_0001', 'ctr_old', 1, 'ctr_old_a', 'pass', 'pass', 0,"
                    " 'approved', '2026-01-01T00:00:00+00:00')")
                conn.commit()
            finally:
                conn.close()
            store = Store(db_path)
            try:
                check = sqlite3.connect(str(db_path))
                try:
                    cols = {r[1] for r in check.execute("PRAGMA table_info(verdicts)").fetchall()}
                finally:
                    check.close()
                self.assertIn("route_hold_json", cols)
                old = store.latest_verdict("ctr_old")
                self.assertEqual(old["id"], "vrd_0001")
                self.assertIsNone(old["route_hold"])
                store.set_verdict_route_hold("vrd_0001", {"status": "held"})
                self.assertEqual(store.latest_verdict("ctr_old")["route_hold"], {"status": "held"})
            finally:
                store.close()
        finally:
            tmp.cleanup()


class TestLessonsForDomainFamily(StoreTestCase):
    """lessons_for ranks by domain match — exact first, then the same family
    (sub-domains and ancestors along the dotted path), siblings excluded —
    then tag overlap, then newest (fix pass Wave 2.3, H10 / F4)."""

    def _lesson(self, domain, rule, tags=()):
        return self.store.create_lesson(
            contract_id=f"c-{rule}", domain=domain, tags=list(tags), outcome="o",
            cause="c", rule=rule, route_change=None, evidence=["verdict:c"],
            written_to="plugin_store")

    def setUp(self):
        super().setUp()
        self._lesson("code", "parent-rule")
        self._lesson("code.game", "game-rule-old")
        self._lesson("code.game", "game-rule-tagged", tags=["hot-loop"])
        self._lesson("code.web", "web-rule")
        self._lesson("ops", "ops-rule")

    def rules(self, **kw):
        return [l["rule"] for l in self.store.lessons_for(**kw)]

    def test_requested_parent_sees_its_subdomains_after_exact(self):
        self.assertEqual(
            self.rules(domain="code", limit=None),
            ["parent-rule", "web-rule", "game-rule-tagged", "game-rule-old"],
        )  # exact first; family newest-first (ids ascend with time)

    def test_requested_subdomain_sees_exact_then_ancestor_never_siblings(self):
        rules = self.rules(domain="code.game", limit=None)
        self.assertEqual(rules[:2], ["game-rule-tagged", "game-rule-old"])
        self.assertEqual(rules[2], "parent-rule")
        self.assertNotIn("web-rule", rules)
        self.assertNotIn("ops-rule", rules)

    def test_tags_rank_within_the_same_domain_match(self):
        self.assertEqual(
            self.rules(domain="code.game", tags=["hot-loop"], limit=None)[0], "game-rule-tagged")
        self.assertEqual(
            self.rules(domain="code", tags=["hot-loop"], limit=None)[:2],
            ["parent-rule", "game-rule-tagged"],  # exact beats tag overlap; then the tagged family lesson
        )

    def test_none_matches_everything_and_limit_applies_after_ranking(self):
        self.assertEqual(len(self.rules(domain=None, limit=None)), 5)
        self.assertEqual(self.rules(domain="code", limit=2), ["parent-rule", "web-rule"])
        self.assertEqual(self.rules(domain="finance", limit=None), [])

    def test_domain_match_ranks(self):
        from hybrid_critic_loop.state import domain_match

        self.assertEqual(domain_match("code", "code"), 2)
        self.assertEqual(domain_match("code", "code.game"), 1)
        self.assertEqual(domain_match("code.game", "code"), 1)
        self.assertEqual(domain_match("code.game", "code.web"), 0)
        self.assertEqual(domain_match("code", "coders"), 0)
        self.assertEqual(domain_match(None, "anything"), 1)
        self.assertEqual(domain_match("code", None), 0)


class TestStoreIsThreadSafe(StoreTestCase):
    """Hermes runs hooks and tool handlers on worker threads against one Store
    (fix pass Wave 2.6, review H8)."""

    def test_writes_from_worker_threads_succeed_and_ids_stay_unique(self):
        import threading

        errors = []

        def worker(i):
            try:
                for j in range(20):
                    self.store.create_lesson(
                        contract_id=f"c{i}-{j}", domain="code", tags=[], outcome="o",
                        cause="c", rule=f"r{i}-{j}", route_change=None, evidence=[],
                        written_to="plugin_store")
            except Exception as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        rows = self.store.lessons_for(domain="code", limit=None)
        self.assertEqual(len(rows), 80)
        self.assertEqual(len({r["id"] for r in rows}), 80)

    def test_set_verdict_human_gate_records_the_decision(self):
        cid = self._contract()
        vid = self.store.create_verdict(
            cid, 1, "w", [], "pass", "pass", None, False, None, None, "pending", None)
        self.assertEqual(self.store.latest_verdict(cid)["human_gate"], "pending")
        self.store.set_verdict_human_gate(vid, "rejected")
        self.assertEqual(self.store.latest_verdict(cid)["human_gate"], "rejected")


if __name__ == "__main__":
    unittest.main()
