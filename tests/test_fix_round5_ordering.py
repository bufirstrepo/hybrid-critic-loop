"""Round-5 fix pass, Unit 6 — ordering proofs the round-5 test audit
asked for (spec H6, test hygiene): the slot-capacity guard fires
BEFORE any worktree creation, and the run claim precedes worktree
creation. Written against the loop's real-git harness pattern
(tests/test_fix_round4_loop.py) — behavioral: a counting wrapper on
WorktreeManager.create observes what the run actually does, in what
order, instead of pinning source shape."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.contracts import validate_contract
from hybrid_critic_loop.executors import Executor, GenResult
from hybrid_critic_loop.state import Store
from hybrid_critic_loop.worktrees import WorktreeManager


def _git(args, cwd):
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True,
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )


class ScriptedGen(Executor):
    """Writes solution.txt into the (real) worktree it is handed."""

    def __init__(self, expert_id, family):
        self.expert_id = expert_id
        self.family = family
        self.duration_ms = 5

    def available(self):
        return (True, "scripted seat")

    def generate(self, worktree, contract, context):
        Path(worktree, "solution.txt").write_text(
            f"solution by {self.expert_id}\n", encoding="utf-8")
        return GenResult(
            status="completed", log=f"generated {self.expert_id}",
            tokens_in=100, tokens_out=50, cost_usd=None, request_ids=[],
            duration_ms=self.duration_ms, detail="")


def _contract(cid):
    return validate_contract({
        "id": cid, "stage": "implement", "task": "Write solution.txt.",
        "domain": "code.game", "tags": ["fix-round5"],
        "acceptance": [{"class": "hard", "cmd": "test -f solution.txt",
                        "expect": {"exit": 0}}],
    })


class CreateSpy:
    """Counts WorktreeManager.create calls for CANDIDATE slots (the
    baseline checkout is verify_baseline's own machinery, not a
    contestant worktree) and snapshots, at the FIRST candidate call,
    the two facts the ordering claims rest on: the run claim file
    exists, and the contract row already reads "running".

    Installed as a plain function on the class so the descriptor
    protocol binds the manager instance exactly as it does for the
    real method.
    """

    BASELINE_SLOT = "baseline"

    def __init__(self, store, repo, contract_id):
        self.store = store
        self.repo = Path(repo)
        self.contract_id = contract_id
        self.calls = 0
        self.first_call_state = None
        self._real = WorktreeManager.create

    def __call__(self, mgr, contract_id, slot):
        if slot != self.BASELINE_SLOT:
            self.calls += 1
            if self.first_call_state is None:
                claim = (self.repo / "wt" / str(self.contract_id)
                         / ".critic-run-claim")
                row = self.store.get_contract(self.contract_id) or {}
                self.first_call_state = {
                    "claim_file_exists": claim.exists(),
                    "contract_status": row.get("status"),
                }
        return self._real(mgr, contract_id, slot)

    def install(self):
        spy = self

        def create(mgr, contract_id, slot):
            return spy(mgr, contract_id, slot)

        WorktreeManager.create = create
        return self

    def restore(self):
        WorktreeManager.create = self._real


class OrderingBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        _git(["init", "-q", "."], self.repo)
        (self.repo / "README.txt").write_text("baseline\n", encoding="utf-8")
        _git(["add", "-A"], self.repo)
        _git(["-c", "user.email=t@local", "-c", "user.name=t",
              "commit", "-q", "-m", "baseline"], self.repo)
        self.data_dir = base / "data"
        self.store = Store(self.data_dir / "data.db")
        self.store.upsert_route(
            "code", "gen-a", "gen-b", [], [],
            "peer_matrix; no self-scoring; blind")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _branches(self, cid):
        return subprocess.run(
            ["git", "branch", "--list", f"critic/{cid}/*"],
            cwd=str(self.repo), capture_output=True, text=True).stdout.strip()


class TestSlotGuardPrecedesWorktrees(OrderingBase):
    """Fix spec G9 pinned the guard before the wave (round-4 pass);
    round-5 test audit: the ORDERING itself was unpinned. A field
    larger than the slot capacity must escalate with the capacity
    reason having created no candidate worktree and no branch. (The
    baseline checkout is not a candidate worktree: verify_baseline
    creates it before the panel is even composed, and the guard
    cannot precede it — the guard's claim is about the wave.)"""

    def test_oversized_field_escalates_without_creating_a_worktree(self):
        cid = "ctr_too_many"
        executors = {
            f"exp-{i:02d}": ScriptedGen(f"exp-{i:02d}", f"fam-{i:02d}")
            for i in range(len(loop_mod.SLOTS) + 1)
        }
        spy = CreateSpy(self.store, self.repo, cid).install()
        try:
            result = loop_mod.run_contract(
                self.store, _contract(cid), self.repo, executors,
                human_decider=None)
        finally:
            spy.restore()
        self.assertEqual(result["status"], "escalated", result)
        self.assertIn("slot capacity", result.get("detail") or "")
        self.assertEqual(spy.calls, 0)
        self.assertEqual(self._branches(cid), "")
        wt_dir = self.repo / "wt" / cid
        slot_dirs = ([p for p in wt_dir.iterdir()
                      if p.is_dir() and p.name != CreateSpy.BASELINE_SLOT]
                     if wt_dir.exists() else [])
        self.assertEqual(slot_dirs, [])


class TestRunClaimPrecedesWorktrees(OrderingBase):
    """The run claim (fix spec G9; round-4 contest-core F7) is taken
    before any worktree is created: when the first candidate worktree
    is created, the claim file already exists and the contract row
    already reads "running" — so a racing second run is answered by
    the claim, not by colliding with half-built worktrees."""

    def test_claim_and_running_status_precede_first_worktree(self):
        cid = "ctr_claim_order"
        executors = {
            "gen-a": ScriptedGen("gen-a", "fam-a"),
            "gen-b": ScriptedGen("gen-b", "fam-b"),
        }
        spy = CreateSpy(self.store, self.repo, cid).install()
        try:
            result = loop_mod.run_contract(
                self.store, _contract(cid), self.repo, executors,
                human_decider=None)
        finally:
            spy.restore()
        self.assertEqual(result["status"], "awaiting_human_gate", result)
        # Both seats were dispatched (baseline checkout excluded).
        self.assertEqual(spy.calls, 2, spy.calls)
        self.assertIsNotNone(spy.first_call_state)
        self.assertTrue(spy.first_call_state["claim_file_exists"],
                        spy.first_call_state)
        self.assertEqual(spy.first_call_state["contract_status"],
                         "running", spy.first_call_state)


if __name__ == "__main__":
    unittest.main()
