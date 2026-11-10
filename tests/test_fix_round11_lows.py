"""Round-11 fix pass, Unit D — the LOWs (FIX-SPEC-ROUND11-2026-11-09
§Unit D).

* store F1 — ``verdict_report`` passed no store to
  ``_unavailable_line``, so an older-form book was misdiagnosed there
  as "chain broken" while every other surface named the older form.
  It now passes the store, as ``ledger_report`` already did.
* store F3 — the router's honest-record branch wrote the zero-passer
  reason ("no seat that passed the deciding gate") even when a
  passer existed and the demotee already sat behind it. The store
  now reports ``no_change_cause`` ("no_passer" / "already_lawful")
  and the line AND the ledger note distinguish the two.
* gatecore F2 — the legacy judged gate retried TWICE: after its one
  documented retry (``JUDGED_RETRIES = 1``; module docstring: "a
  legacy rubric retries once, then escalates") the code fell through
  to the generic hard/measured retry and granted a second retry.
  The CODE was corrected to the spec text: a spent judged retry
  escalates. (Evidence for correcting the code, not the docstring:
  the docstring is the queue spec, and the constant existed solely
  for this branch yet never changed any outcome.)
* gatecore F3 — the bracket tie-break read a missing duration as
  0 ms (fastest). Missing now sorts slowest (+inf), the same
  convention as ``_tokens_total``'s missing tokens; the legacy
  winner pick's sibling site in ``loop._pick_winner`` likewise.
* gatecore F4 — the matrix/judges presentation shuffle seeded its
  RNG with ``hash((seed, judge_id))``, randomised per process by
  PYTHONHASHSEED, so a seeded shuffle never reproduced. Both sites
  now share ``judges.seeded_shuffle_rng`` (SHA-256 over the string
  form); pinned across two processes with different hash seeds.
* performance F1 — DOCUMENTED as a named residual, not fixed:
  ``lessons_for`` reads and ranks the whole lessons table per call
  (measured trajectory in the README and the ``lessons_for``
  docstring). The ranking cannot be pushed into SQL bounded-ly and
  a domain index would not move the single-domain trajectory, so
  there is no code pin for it here beyond the residual text's
  presence, pinned below so it cannot silently vanish.
* documents LOWs (lane 2 F4/F5) — README string corrections, pinned
  below at string level.
"""

import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

from hybrid_critic_loop import bracket, chain, judges, reports, router
from hybrid_critic_loop.state import Store

from tests.test_loop_v03 import FakeGen, FakeJudge, LoopV03Base
from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.contracts import validate_contract

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# store F1 — verdict_report names the older form, like every other surface
# ---------------------------------------------------------------------------

class TestVerdictReportOlderForm(unittest.TestCase):
    def test_older_form_book_is_not_called_broken(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "book.db")
            s = Store(path)
            tid = s.create_target("t", "code")
            s.create_contract(tid, "solve", "task", "code", [], [],
                              contract_id="ctr_1")
            cand = s.create_candidate("ctr_1", 1, "solve", "exp-1",
                                      "fam", "a", None, "base")
            for _ in range(3):
                s.add_ledger(tid, "ctr_1", cand, "solve", "exp-1", 10, 5,
                             1.0, "completed", None, provenance="generated")
            s.create_verdict("ctr_1", 1, cand, [], "pass", "pass", None,
                             False, None, None, "approved", None)
            rows = s.ledger_rows()
            s.close()
            # Interior row 2 re-sealed under the LEGACY canonical
            # form; row 3 re-sealed (current form) over the new link —
            # the lane probe's exact shape (p6_olderform.py).
            conn = sqlite3.connect(path)
            r2 = dict(rows[1])
            h2 = chain.row_hash_legacy(r2["prev_hash"], r2)
            conn.execute("UPDATE ledger SET row_hash=? WHERE id=2", (h2,))
            r3 = dict(rows[2])
            r3["prev_hash"] = h2
            h3 = chain.row_hash(h2, r3)
            conn.execute(
                "UPDATE ledger SET prev_hash=?, row_hash=? WHERE id=3",
                (h2, h3))
            conn.commit()
            conn.close()
            s = Store(path)
            try:
                text = reports.verdict_report(s, "ctr_1")
            finally:
                s.close()
        self.assertIn("sealed under an older form at row 2", text)
        self.assertNotIn("chain broken", text)


# ---------------------------------------------------------------------------
# store F3 — no-passer vs already-lawful, in the line AND the ledger note
# ---------------------------------------------------------------------------

class TestRouterNoChangeCause(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.upsert_route("code", "A", "B", ["C"], [], "r")

    def tearDown(self):
        self.store.close()

    def test_already_lawful_demotion_records_the_true_reason(self):
        # A passed the deciding gate and is already primary; B (the
        # demotee) already sits behind it. Nothing can move — but a
        # passer EXISTS, so the zero-passer reason is false.
        line = router.apply_lesson_route_change(
            self.store, "code", {"demote": "B", "reason": "lost on gate"},
            "ctr_9", gate_passed={"A"})
        self.assertIn("already behind every seat that passed", line)
        self.assertNotIn("no seat that passed the deciding gate", line)
        notes = [r["note"] for r in self.store.ledger_rows()]
        self.assertEqual(len(notes), 1)
        self.assertIn("already behind every seat that passed", notes[0])
        self.assertNotIn("no seat that passed the deciding gate", notes[0])
        route = self.store.get_route("code")
        self.assertEqual(
            (route["primary"], route["compete_with"], route["fallback"]),
            ("A", "B", ["C"]))

    def test_store_reports_already_lawful_cause(self):
        result = self.store.apply_route_demotion(
            "code", "B", None, "lost", passed_experts={"A"})
        self.assertFalse(result["changed"])
        self.assertEqual(result["no_change_cause"], "already_lawful")

    def test_zero_passers_still_records_the_zero_passer_reason(self):
        line = router.apply_lesson_route_change(
            self.store, "code", {"demote": "B", "reason": "lost on gate"},
            "ctr_9", gate_passed=set())
        self.assertIn("no seat that passed the deciding gate", line)
        notes = [r["note"] for r in self.store.ledger_rows()]
        self.assertEqual(len(notes), 1)
        self.assertIn("no seat that passed the deciding gate", notes[0])

    def test_store_reports_no_passer_cause(self):
        result = self.store.apply_route_demotion(
            "code", "B", None, "lost", passed_experts=set())
        self.assertFalse(result["changed"])
        self.assertEqual(result["no_change_cause"], "no_passer")


# ---------------------------------------------------------------------------
# gatecore F2 — the legacy judged gate retries once, then escalates
# ---------------------------------------------------------------------------

class TestLegacyJudgedRetriesOnce(LoopV03Base):
    def test_legacy_judged_failure_escalates_after_one_retry(self):
        # The lane probe's exact shape (probe_legacy_retry.py):
        # legacy rubric, all judges scoring below the minimum, every
        # candidate passing hard/measured.
        self.markers.update({"gen-a": "m-a", "gen-b": "m-b"})
        low = {"m-a": 3.0, "m-b": 3.0}
        executors = {
            "gen-a": FakeGen("gen-a", "fam-a", "m-a"),
            "gen-b": FakeGen("gen-b", "fam-b", "m-b"),
            "judge-x": FakeJudge("judge-x", "fam-x", low),
            "judge-y": FakeJudge("judge-y", "fam-y", low),
            "judge-z": FakeJudge("judge-z", "fam-z", low),
        }
        self.panel("gen-a", "gen-b", [], ["gen-a", "gen-b"])
        self.store.upsert_route(
            "code", "gen-a", "gen-b", [],
            ["judge-x", "judge-y", "judge-z"], "peer")
        raw = {
            "id": "ctr_fake", "stage": "implement",
            "task": "Implement the fake thing.", "domain": "code.game",
            "tags": ["fake"],
            "acceptance": [
                {"class": "hard", "cmd": "true", "expect": {"exit": 0}},
                {"class": "judged", "rubric": "code_quality_v1",
                 "min": 7, "judges": 3},
            ],
        }
        contract = validate_contract(raw)
        result = loop_mod.run_contract(
            self.store, contract, "repo://fake", executors,
            human_decider=lambda info: "approved")
        self.assertEqual(result.get("status"), "escalated")
        attempts = sorted(
            {c["attempt"] for c in self.store.candidates_for("ctr_fake")})
        # Attempt 1 + the ONE documented retry — not the generic cap.
        self.assertEqual(attempts, [1, 2])
        self.assertEqual(executors["judge-x"].calls, 2)


# ---------------------------------------------------------------------------
# gatecore F3 — a missing duration is the slowest, not 0 ms
# ---------------------------------------------------------------------------

class TestMissingDurationSortsLast(unittest.TestCase):
    def test_missing_duration_loses_the_tie(self):
        field = [
            {"id": "known", "tokens_in": 10, "tokens_out": 10},
            {"id": "unknown", "tokens_in": 10, "tokens_out": 10},
        ]
        validity = {"known": 8.0, "unknown": 8.0}
        ranked = bracket.rank_candidates(
            field, validity, durations={"known": 1200})
        self.assertEqual([c["id"] for c in ranked], ["known", "unknown"])

    def test_recorded_zero_still_beats_a_real_duration(self):
        # A RECORDED 0 ms is a measurement and keeps its meaning;
        # only the missing entry changed.
        field = [
            {"id": "slow", "tokens_in": 10, "tokens_out": 10},
            {"id": "zero", "tokens_in": 10, "tokens_out": 10},
        ]
        validity = {"slow": 8.0, "zero": 8.0}
        ranked = bracket.rank_candidates(
            field, validity, durations={"slow": 1200, "zero": 0})
        self.assertEqual([c["id"] for c in ranked], ["zero", "slow"])


# ---------------------------------------------------------------------------
# gatecore F4 — the seeded shuffle reproduces across processes
# ---------------------------------------------------------------------------

class TestSeededShuffleReproduces(unittest.TestCase):
    EXPECTED = ["cand-f", "cand-c", "cand-d", "cand-b", "cand-a", "cand-e"]

    def test_helper_matches_the_recorded_order(self):
        order = ["cand-a", "cand-b", "cand-c", "cand-d", "cand-e", "cand-f"]
        judges.seeded_shuffle_rng(1, "judge-a").shuffle(order)
        self.assertEqual(order, self.EXPECTED)

    def test_two_processes_with_different_hash_seeds_agree(self):
        snippet = (
            "import sys; sys.path.insert(0, {root!r})\n"
            "from hybrid_critic_loop.judges import seeded_shuffle_rng\n"
            "order = ['cand-a','cand-b','cand-c','cand-d','cand-e','cand-f']\n"
            "seeded_shuffle_rng(1, 'judge-a').shuffle(order)\n"
            "print(order)\n"
        ).format(root=str(ROOT))
        import os
        outputs = []
        for hash_seed in ("0", "42"):
            env = dict(os.environ, PYTHONHASHSEED=hash_seed)
            proc = subprocess.run(
                [sys.executable, "-c", snippet], capture_output=True,
                text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outputs.append(proc.stdout.strip())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], str(self.EXPECTED))

    def test_no_site_seeds_from_the_builtin_hash(self):
        for name in ("matrix.py", "judges.py"):
            source = (ROOT / "hybrid_critic_loop" / name).read_text()
            self.assertNotIn("Random(hash((seed, judge_id))", source, name)


# ---------------------------------------------------------------------------
# performance F1 (documented residual) + documents LOWs — text pins
# ---------------------------------------------------------------------------

class TestDocumentedResidualAndDocLows(unittest.TestCase):
    def test_lessons_cost_residual_is_named_with_its_trajectory(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("Named residual — lesson lookup cost", readme)
        self.assertIn("50,000 lessons", readme)
        doc = Store.lessons_for.__doc__
        self.assertIn("Named residual", doc)
        self.assertIn("50,000", doc)

    def test_readme_seat_count_low(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("adapters for nine seats across the eight families",
                      readme)
        self.assertNotIn("adapters for eight family seats", readme)

    def test_readme_tie_break_wording_low(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("fewer tokens wins, then shorter generation duration",
                      readme)
        self.assertNotIn("then earlier completion (the Phase-0 tie rule)",
                         readme)


if __name__ == "__main__":
    unittest.main()
