"""End-to-end loop integration tests with the local solver executors.

Runs the real loop (baseline check -> wave -> gates -> verdict -> human
gate -> merge -> lesson -> route change) against a temp copy of the
python-sampler fixture. No model CLIs are involved; the generators are
the deterministic local solvers (family "local").
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.contracts import load_contract_file
from hybrid_critic_loop.executors import LocalSolverExecutor
from hybrid_critic_loop.state import Store

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixture" / "python-sampler"


def _git(args, cwd):
    subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True,
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )


def _local_executors():
    return {
        "solver-naive": LocalSolverExecutor("solver-naive", "naive"),
        "solver-ring": LocalSolverExecutor("solver-ring", "ring"),
    }


class LoopTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        shutil.copytree(FIXTURE, self.repo)
        _git(["init", "-q", "."], self.repo)
        _git(["add", "-A"], self.repo)
        _git(
            ["-c", "user.email=t@local", "-c", "user.name=t", "commit", "-q", "-m", "baseline"],
            self.repo,
        )
        self.data_dir = base / "data"
        self.store = Store(self.data_dir / "data.db")
        self.store.upsert_route(
            "code", "solver-naive", "solver-ring", [], ["gemini", "beam"],
            "judge.family not in contest.families; panel recomputed on fallback",
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class TestRunOne(LoopTestBase):
    def test_contract1_full_loop(self):
        contract = load_contract_file(ROOT / "contracts" / "demo1.json")
        result = loop_mod.run_contract(
            self.store, contract, self.repo, _local_executors(),
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "completed", json.dumps(result, indent=2))
        # The ring solver wins: the naive one dies on the measured gate.
        winner = self.store.get_candidate(result["winner"])
        self.assertEqual(winner["expert_id"], "solver-ring")
        # Winner merged into the repo: implementation present on main.
        merged = (self.repo / "src" / "speed_sampler.py").read_text()
        self.assertNotIn("NotImplementedError", merged)
        # Lesson written from the real measurement row, with a route change.
        lessons = self.store.lessons_for(domain="code.game")
        self.assertEqual(len(lessons), 1)
        lesson = lessons[0]
        self.assertIn("ops_per_tick", lesson["outcome"])
        self.assertEqual(lesson["route_change"]["demote"], "solver-naive")
        self.assertEqual(lesson["written_to"], "plugin_store")
        # Route table now reflects the demotion.
        route = self.store.get_route("code")
        self.assertEqual(route["primary"], "solver-ring")
        self.assertEqual(route["compete_with"], "solver-naive")
        # Ledger has real rows for both dispatches.
        outcomes = {(r["expert_id"], r["outcome"]) for r in self.store.ledger_rows()}
        self.assertIn(("solver-naive", "completed"), outcomes)
        self.assertIn(("solver-ring", "completed"), outcomes)
        # Human-gate event fired.
        self.assertIn("human_gate", [e["type"] for e in result["events"]])
        # Loser kept as a ref.
        branches = subprocess.run(
            ["git", "branch", "--list", "critic/ctr_demo_1/*"],
            cwd=str(self.repo), capture_output=True, text=True,
        ).stdout
        self.assertIn("critic/ctr_demo_1/a", branches)

    def test_contract2_after_lesson_both_pass_measured(self):
        # v0.3 AMENDMENT (logged in BUILD-LOG): demo2 now carries the
        # validity_matrix_v1 judged item, so run 2 goes through the
        # peer matrix. The executor set here has NO judge-capable
        # executor (LocalSolverExecutor.judge returns None — the local
        # family never judges), so no peer cells exist, nobody passes,
        # and the queue escalates honestly instead of the v1
        # wall-clock tie-break. The v1 assertion (both candidates pass
        # the measured gate) is kept below — it is the anchor fact the
        # lesson depends on. The full matrix path with local demo
        # judges is exercised by the demo script and test_loop_v03.
        c1 = load_contract_file(ROOT / "contracts" / "demo1.json")
        loop_mod.run_contract(
            self.store, c1, self.repo, _local_executors(),
            human_decider=lambda info: "approved",
        )
        c2 = load_contract_file(ROOT / "contracts" / "demo2.json")
        result = loop_mod.run_contract(
            self.store, c2, self.repo, _local_executors(),
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "escalated", json.dumps(result, indent=2))
        self.assertIn("matrix", result["detail"])
        for cand in self.store.candidates_for("ctr_demo_2"):
            for m in self.store.measurements_for(cand["id"]):
                self.assertTrue(m["pass"], f"{cand['id']} {m}")

    def test_human_rejection_creates_new_contract(self):
        contract = load_contract_file(ROOT / "contracts" / "demo1.json")
        result = loop_mod.run_contract(
            self.store, contract, self.repo, _local_executors(),
            human_decider=lambda info: "rejected",
        )
        self.assertEqual(result["status"], "rejected")
        new_contract = self.store.get_contract(result["new_contract_id"])
        self.assertIsNotNone(new_contract)
        kinds = [o["kind"] for o in new_contract["observations"]]
        self.assertIn("human_rejection", kinds)

    def test_awaiting_human_gate_without_decider(self):
        contract = load_contract_file(ROOT / "contracts" / "demo1.json")
        result = loop_mod.run_contract(
            self.store, contract, self.repo, _local_executors(), human_decider=None
        )
        self.assertEqual(result["status"], "awaiting_human_gate")
        # Completing the gate afterwards merges and finishes the contract.
        done = loop_mod.record_human_gate(self.store, "ctr_demo_1", "approved")
        self.assertEqual(done["status"], "completed")
        merged = (self.repo / "src" / "speed_sampler.py").read_text()
        self.assertNotIn("NotImplementedError", merged)


class TestRepositoryPathWithWhitespace(unittest.TestCase):
    """A repository path containing a space must survive every git call.

    The owner's own workspace path has one; the first real run there died
    in ``WorktreeManager.baseline_hash`` (fix pass, Wave 2.0).
    """

    def _init_repo(self, repo):
        repo.mkdir(parents=True)
        _git(["init", "-q", "."], repo)
        (repo / "a.txt").write_text("baseline\n", encoding="utf-8")
        _git(["add", "-A"], repo)
        _git(
            ["-c", "user.email=t@local", "-c", "user.name=t", "commit", "-q", "-m", "baseline"],
            repo,
        )

    def test_worktree_manager_round_trips_in_a_spaced_path(self):
        from hybrid_critic_loop.worktrees import WorktreeManager

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo with space"
            self._init_repo(repo)
            mgr = WorktreeManager(repo)
            head = mgr.baseline_hash()
            self.assertEqual(len(head), 40, head)
            wt = Path(mgr.create("ctr_space", "a"))
            self.assertTrue(wt.is_dir())
            (wt / "b.txt").write_text("candidate\n", encoding="utf-8")
            self.assertIn("b.txt", mgr.diff(wt))
            mgr.commit_all(wt, "critic: spaced path")
            merged = mgr.merge_into_main(wt)
            self.assertEqual(len(merged), 40, merged)
            self.assertTrue((repo / "b.txt").exists())
            mgr.remove(wt)
            self.assertFalse(wt.exists())

    def test_full_loop_completes_in_a_spaced_path(self):
        # The demo's own failure shape: a whole contract run against a
        # repository (and a data dir) under a path with a space.
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "work space"
            repo = base / "repo"
            base.mkdir()
            shutil.copytree(FIXTURE, repo)
            _git(["init", "-q", "."], repo)
            _git(["add", "-A"], repo)
            _git(
                ["-c", "user.email=t@local", "-c", "user.name=t", "commit", "-q", "-m", "baseline"],
                repo,
            )
            store = Store(base / "data" / "data.db")
            try:
                store.upsert_route(
                    "code", "solver-naive", "solver-ring", [], ["gemini", "beam"],
                    "judge.family not in contest.families; panel recomputed on fallback",
                )
                contract = load_contract_file(ROOT / "contracts" / "demo1.json")
                result = loop_mod.run_contract(
                    store, contract, repo, _local_executors(),
                    human_decider=lambda info: "approved",
                )
            finally:
                store.close()
            self.assertEqual(result["status"], "completed", json.dumps(result, indent=2))
            self.assertNotIn(
                "NotImplementedError", (repo / "src" / "speed_sampler.py").read_text()
            )


class TestBaselineRefusal(LoopTestBase):
    def test_vacuous_contract_refused(self):
        # Pre-implement the sampler so acceptance passes on the baseline.
        impl = (FIXTURE / "src" / "speed_sampler.py").read_text()
        ring = (
            "class SpeedSampler:\n"
            "    CAPACITY = 120\n"
            "    def __init__(self, capacity=CAPACITY):\n"
            "        self._capacity = capacity\n"
            "        self._buf = [0.0] * capacity\n"
            "        self._idx = 0\n"
            "        self._count = 0\n"
            "        self._total = 0.0\n"
            "    def push(self, mps):\n"
            "        if self._count == self._capacity:\n"
            "            self._total -= self._buf[self._idx]\n"
            "        else:\n"
            "            self._count += 1\n"
            "        self._buf[self._idx] = mps\n"
            "        self._total += mps\n"
            "        self._idx = (self._idx + 1) % self._capacity\n"
            "    def average(self):\n"
            "        return self._total / self._count if self._count else 0.0\n"
        )
        assert impl  # fixture stub read for shape only
        (self.repo / "src" / "speed_sampler.py").write_text(ring)
        _git(["add", "-A"], self.repo)
        _git(
            ["-c", "user.email=t@local", "-c", "user.name=t", "commit", "-q", "-m", "pre-implemented"],
            self.repo,
        )
        contract = load_contract_file(ROOT / "contracts" / "demo1.json")
        result = loop_mod.run_contract(
            self.store, contract, self.repo, _local_executors(),
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "refused_baseline")
        self.assertEqual(self.store.candidates_for("ctr_demo_1"), [])


if __name__ == "__main__":
    unittest.main()
