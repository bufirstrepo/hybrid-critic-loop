"""Round-4 fix pass, Unit 3 (loop/gate) regression tests — real git.

These are the properties the in-memory harness cannot express
(round-4 contest-core F1/F2/F4): the merge takes the verdict-time
snapshot, not the live worktree; two processes deciding one gate
produce exactly one completion and one final answer; refused and
rejected results carry no certificate; terminal states clean up
worktree dirs and branches.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.contracts import validate_contract
from hybrid_critic_loop.executors import Executor, GenResult
from hybrid_critic_loop.state import Store
from hybrid_critic_loop.worktrees import WorktreeManager

ROOT = Path(__file__).resolve().parents[1]


def _git(args, cwd):
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True,
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )


class ScriptedGen(Executor):
    """Writes solution.txt into the (real) worktree it is handed."""

    def __init__(self, expert_id, family, marker, duration_ms=10):
        self.expert_id = expert_id
        self.family = family
        self.marker = marker
        self.duration_ms = duration_ms

    def available(self):
        return (True, "scripted seat")

    def generate(self, worktree, contract, context):
        Path(worktree, "solution.txt").write_text(
            f"solution by {self.marker}\n", encoding="utf-8")
        return GenResult(
            status="completed", log=f"generated {self.marker}",
            tokens_in=100, tokens_out=50, cost_usd=None, request_ids=[],
            duration_ms=self.duration_ms, detail=f"marker={self.marker}")


def _contract(cid, measured=False, hard_cmd="test -f solution.txt"):
    acceptance = [{"class": "hard", "cmd": hard_cmd, "expect": {"exit": 0}}]
    if measured:
        acceptance.append({
            "class": "measured",
            "metrics": [{"name": "ops", "op": "<=", "value": 8}],
            "sensor": "ops_bench",
            "sensor_cmd": (
                "python3 -c \"import json; "
                "print(json.dumps({'ops': 99}))\""
            ),
        })
    return validate_contract({
        "id": cid, "stage": "implement", "task": "Write solution.txt.",
        "domain": "code.game", "tags": ["fix-round4"],
        "acceptance": acceptance,
    })


class RealGitBase(unittest.TestCase):
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

    def _executors(self):
        # Equal tokens: the faster seat (gen-b) wins the wall-clock tie.
        return {
            "gen-a": ScriptedGen("gen-a", "fam-a", "gen-a", duration_ms=10),
            "gen-b": ScriptedGen("gen-b", "fam-b", "gen-b", duration_ms=5),
        }

    def _run_to_gate(self, cid, **contract_kw):
        result = loop_mod.run_contract(
            self.store, _contract(cid, **contract_kw), self.repo,
            self._executors(), human_decider=None)
        self.assertEqual(result["status"], "awaiting_human_gate", result)
        return result

    def _branches(self, cid):
        return subprocess.run(
            ["git", "branch", "--list", f"critic/{cid}/*"],
            cwd=str(self.repo), capture_output=True, text=True).stdout.strip()


class TestVerdictSnapshotMerge(RealGitBase):
    """Fix spec G1 (round-4 contest-core F1): what merges is what was
    gated — the verdict-time snapshot commit, never the live tree."""

    def test_post_verdict_tamper_does_not_reach_the_merge(self):
        result = self._run_to_gate("ctr_snap")
        winner = self.store.get_candidate(result["winner"])
        self.assertEqual(winner["expert_id"], "gen-b")
        snapshot = winner["payload_hash"]
        mgr = WorktreeManager(self.repo)
        self.assertTrue(mgr.is_commit(snapshot), snapshot)
        wt = Path(winner["worktree"])
        (wt / "solution.txt").write_text(
            "TAMPERED AFTER VERDICT\n", encoding="utf-8")
        (wt / "stray.txt").write_text("stray\n", encoding="utf-8")
        done = loop_mod.record_human_gate(self.store, "ctr_snap", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["merge_source"], "verdict_snapshot")
        self.assertEqual(done["winner_commit"], f"git:{snapshot}")
        self.assertEqual(
            (self.repo / "solution.txt").read_text(encoding="utf-8"),
            "solution by gen-b\n")
        self.assertFalse((self.repo / "stray.txt").exists())


_GATE_HELPER = '''
import json, sys, time
from pathlib import Path
db, cid, go, delay = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
from hybrid_critic_loop.state import Store
from hybrid_critic_loop import loop as loop_mod
deadline = time.time() + 60
while not Path(go).exists():
    if time.time() > deadline:
        sys.exit(2)
    time.sleep(0.02)
time.sleep(delay)
store = Store(db)
try:
    result = loop_mod.record_human_gate(store, cid, "approved")
finally:
    store.close()
print(json.dumps({"status": result.get("status"),
                  "detail": result.get("detail")}), file=sys.__stdout__)
'''


class TestConcurrentGateClaim(RealGitBase):
    """Fix spec G1 (round-4 security F4, contest-core F2): the gate is
    claimed atomically — two processes deciding one gate yield exactly
    one completed result and exactly one final_answers row."""

    def test_two_processes_one_gate_one_final_answer(self):
        self._run_to_gate("ctr_race")
        base = Path(self.tmp.name)
        helper = base / "gate_helper.py"
        helper.write_text(_GATE_HELPER, encoding="utf-8")
        go = base / "go.flag"
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        procs = [
            subprocess.Popen(
                [sys.executable, str(helper), str(self.data_dir / "data.db"),
                 "ctr_race", str(go), str(0.3 * i)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env)
            for i in range(2)
        ]
        go.write_text("go", encoding="utf-8")
        outputs = []
        for p in procs:
            out, err = p.communicate(timeout=180)
            self.assertEqual(p.returncode, 0, err)
            self.assertTrue(out.strip(), f"gate helper produced no output; stderr: {err}")
            outputs.append(json.loads(out.strip().splitlines()[-1]))
        self.assertEqual(
            sorted(o["status"] for o in outputs), ["completed", "error"],
            outputs)
        loser = [o for o in outputs if o["status"] == "error"][0]
        self.assertIn("no pending human gate", loser["detail"])
        conn = sqlite3.connect(self.data_dir / "data.db")
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM final_answers WHERE contract_id=?",
                ("ctr_race",)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1)


class TestCertificatePlacement(RealGitBase):
    """Fix spec G1 (round-4 regression F1): the certificate attaches
    only to a completed, gate-decided result — pending, refused, and
    rejected results carry no stamp and no token."""

    def test_waiver_run_certifies_only_at_completion(self):
        result = self._run_to_gate("ctr_cert")
        # The gate flow below needs a verdict carrying an offered
        # measured waiver. A run no longer produces that state — a
        # measured regression fails the contract instead of reaching
        # the gate (owner decisions 2026-11-09, Item 2(a) = B) — so
        # the offer is placed on the verdict row directly: the state
        # a pre-ruling run left behind. What this test pins is the
        # GATE's handling of such a verdict, unchanged by the ruling:
        # approval refused, explicit waiver, certificate only at
        # completion.
        self.store.set_verdict_waiver(result["verdict_id"], {
            "gate": "measured", "status": "offered",
            "candidate": result["winner"],
        })
        self.assertNotIn("certificate", result)
        refused = loop_mod.record_human_gate(
            self.store, "ctr_cert", "approved")
        self.assertEqual(refused["status"], "error", refused)
        self.assertIn("waive", refused["detail"])
        self.assertNotIn("certificate", refused)
        verdict = self.store.latest_verdict("ctr_cert")
        self.assertEqual(verdict["human_gate"], "pending")
        done = loop_mod.record_human_gate(
            self.store, "ctr_cert", "waived", "sensor variance accepted")
        self.assertEqual(done["status"], "completed", done)
        self.assertIn("certificate", done)
        self.assertTrue(
            done["certificate"]["stamp"].startswith("MM CERTIFIED"),
            done["certificate"])
        verdict = self.store.latest_verdict("ctr_cert")
        self.assertEqual(verdict["human_gate"], "waived")
        # The granted waiver names the channel the decision arrived
        # through (round-4 security F7), not a hardcoded "human".
        self.assertEqual(verdict["waiver"]["by"], "cli")


class TestTerminalCleanup(RealGitBase):
    """Fix spec G9 (round-4 contest-core F4): rejection and escalation
    remove candidate worktree dirs and, at terminal states, branches."""

    def test_rejection_removes_worktrees_and_branches_and_certifies_nothing(self):
        self._run_to_gate("ctr_rej")
        worktrees = [Path(c["worktree"])
                     for c in self.store.candidates_for("ctr_rej")]
        self.assertTrue(worktrees)
        self.assertTrue(all(w.exists() for w in worktrees))
        rejected = loop_mod.record_human_gate(
            self.store, "ctr_rej", "rejected", "not this one")
        self.assertEqual(rejected["status"], "rejected", rejected)
        self.assertNotIn("certificate", rejected)
        self.assertTrue(all(not w.exists() for w in worktrees))
        self.assertEqual(self._branches("ctr_rej"), "")

    def test_escalated_run_leaves_no_worktrees_or_branches(self):
        result = loop_mod.run_contract(
            self.store,
            _contract("ctr_esc", hard_cmd="test -f never_written.txt"),
            self.repo, self._executors(), human_decider=None)
        self.assertEqual(result["status"], "escalated", result)
        worktrees = [Path(c["worktree"])
                     for c in self.store.candidates_for("ctr_esc")]
        self.assertTrue(worktrees)
        self.assertTrue(all(not w.exists() for w in worktrees))
        self.assertEqual(self._branches("ctr_esc"), "")


if __name__ == "__main__":
    unittest.main()
