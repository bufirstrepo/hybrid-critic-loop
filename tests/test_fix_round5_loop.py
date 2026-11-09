"""Round-5 fix pass, Unit 3 (loop/gate) regression tests — real git.

The properties this file pins (fix spec H1/H5/H6; round-5 gate-core
F1/F3/F4/F5/F7, security F1/F6):

* a post-claim completion failure lands in the recorded failure
  state — verdict "failed", contract "gate_failed", a gate_failure
  observation — and presenting the decision again retries completion
  from the snapshot (the round-5 stranding repro);
* a waiver-grant write failure aborts BEFORE any merge;
* the run claim is released on every exit, and a stale claim over a
  non-running contract is broken with a named note;
* the verdict snapshot is pinned under refs/critic-snapshots/<cid>/
  <candidate> and the gate resolves through it even after the winner
  branch is deleted and gc'd;
* eliminations persist their kind on the candidate row.
"""

import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.contracts import validate_contract
from hybrid_critic_loop.executors import Executor, GenResult
from hybrid_critic_loop.state import Store
from hybrid_critic_loop.worktrees import WorktreeManager

from tests.test_fix_round4_loop import (
    RealGitBase, ScriptedGen, _contract, _git)


class ContractGen(Executor):
    """Writes code.txt naming the contract it was generated for —
    two contracts' winners therefore conflict on the same line."""

    def __init__(self, expert_id, family, marker, duration_ms=10):
        self.expert_id = expert_id
        self.family = family
        self.marker = marker
        self.duration_ms = duration_ms

    def available(self):
        return (True, "scripted seat")

    def generate(self, worktree, contract, context):
        Path(worktree, "code.txt").write_text(
            f"change for {contract['id']} by {self.marker}\n",
            encoding="utf-8")
        return GenResult(
            status="completed", log=f"generated {self.marker}",
            tokens_in=100, tokens_out=50, cost_usd=None, request_ids=[],
            duration_ms=self.duration_ms, detail=f"marker={self.marker}")


class LazyGen(Executor):
    """Completes without writing anything: a hard-gate failure."""

    def __init__(self, expert_id, family):
        self.expert_id = expert_id
        self.family = family

    def available(self):
        return (True, "scripted seat")

    def generate(self, worktree, contract, context):
        return GenResult(
            status="completed", log="wrote nothing",
            tokens_in=10, tokens_out=5, cost_usd=None, request_ids=[],
            duration_ms=10, detail="lazy")


def _code_contract(cid):
    return validate_contract({
        "id": cid, "stage": "implement", "task": "Change code.txt.",
        "domain": "code.game", "tags": ["fix-round5"],
        "acceptance": [{
            "class": "hard",
            "cmd": "grep -q 'change for' code.txt",
            "expect": {"exit": 0},
        }],
    })


def _final_answer_count(store, cid):
    conn = sqlite3.connect(store.db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM final_answers WHERE contract_id=?",
            (cid,)).fetchone()[0]
    finally:
        conn.close()


def _observations(store, cid):
    contract = store.get_contract(cid) or {}
    return contract.get("observations") or []


class StrandingBase(RealGitBase):
    def setUp(self):
        super().setUp()
        (self.repo / "code.txt").write_text("base line\n", encoding="utf-8")
        _git(["add", "-A"], self.repo)
        _git(["-c", "user.email=t@local", "-c", "user.name=t",
              "commit", "-q", "-m", "code baseline"], self.repo)

    def _contract_executors(self):
        return {
            "gen-a": ContractGen("gen-a", "fam-a", "gen-a", duration_ms=10),
            "gen-b": ContractGen("gen-b", "fam-b", "gen-b", duration_ms=5),
        }

    def _run_code_contract(self, cid):
        result = loop_mod.run_contract(
            self.store, _code_contract(cid), self.repo,
            self._contract_executors(), human_decider=None)
        self.assertEqual(result["status"], "awaiting_human_gate", result)
        return result


class TestGateFailureRecovery(StrandingBase):
    """Spec H1 (round-5 gate-core F1, security F1): the stranding
    repro — two contracts pending on one repo; approving the second
    moves main so the first's post-claim merge conflicts. The first
    gate must land in gate_failed with a gate_failure observation —
    NOT stuck at awaiting_human_gate with its claim consumed — and a
    retry after resolving main must complete it."""

    def test_conflicting_approval_fails_recorded_and_retries(self):
        self._run_code_contract("ctr_a")
        self._run_code_contract("ctr_b")

        done_b = loop_mod.record_human_gate(self.store, "ctr_b", "approved")
        self.assertEqual(done_b["status"], "completed", done_b)
        self.assertEqual(
            (self.repo / "code.txt").read_text(encoding="utf-8"),
            "change for ctr_b by gen-b\n")

        failed = loop_mod.record_human_gate(self.store, "ctr_a", "approved")
        self.assertEqual(failed["status"], "error", failed)
        self.assertEqual(failed["human_gate"], "failed", failed)
        self.assertNotIn("certificate", failed)
        self.assertIn("completion failed", failed["detail"])
        verdict = self.store.latest_verdict("ctr_a")
        self.assertEqual(verdict["human_gate"], "failed", verdict)
        contract = self.store.get_contract("ctr_a")
        self.assertEqual(contract["status"], "gate_failed", contract)
        failures = [o for o in _observations(self.store, "ctr_a")
                    if o.get("kind") == "gate_failure"]
        self.assertEqual(len(failures), 1, failures)
        self.assertEqual(failures[0]["decision"], "approved")
        self.assertEqual(failures[0]["channel"], "cli")
        self.assertTrue(failures[0]["reason"])
        # Nothing merged and no final answer was written for ctr_a.
        self.assertEqual(_final_answer_count(self.store, "ctr_a"), 0)
        self.assertEqual(
            (self.repo / "code.txt").read_text(encoding="utf-8"),
            "change for ctr_b by gen-b\n")

        # Resolve the conflict on main: code.txt back to the baseline
        # line, so the stranded snapshot merges cleanly.
        (self.repo / "code.txt").write_text("base line\n", encoding="utf-8")
        _git(["add", "-A"], self.repo)
        _git(["-c", "user.email=t@local", "-c", "user.name=t",
              "commit", "-q", "-m", "restore baseline line"], self.repo)

        retried = loop_mod.record_human_gate(self.store, "ctr_a", "approved")
        self.assertEqual(retried["status"], "completed", retried)
        self.assertEqual(retried["merge_source"], "verdict_snapshot")
        self.assertIn("certificate", retried)
        verdict = self.store.latest_verdict("ctr_a")
        self.assertEqual(verdict["human_gate"], "approved", verdict)
        self.assertEqual(self.store.get_contract("ctr_a")["status"],
                         "completed")
        self.assertEqual(_final_answer_count(self.store, "ctr_a"), 1)
        self.assertEqual(
            (self.repo / "code.txt").read_text(encoding="utf-8"),
            "change for ctr_a by gen-b\n")
        # The merge source is durable on the contract, not only in
        # the result (spec H1).
        completions = [o for o in _observations(self.store, "ctr_a")
                       if o.get("kind") == "gate_completion"]
        self.assertEqual(len(completions), 1, completions)
        self.assertEqual(completions[0]["merge_source"], "verdict_snapshot")


class WaiverFailingStore(Store):
    """A store whose waiver-grant write always fails."""

    def set_verdict_waiver(self, verdict_id, waiver):
        raise RuntimeError("waiver write exploded (test double)")


class TestWaiverWriteFailure(RealGitBase):
    """Spec H1 (round-5 gate-core F7): the waiver-grant record is
    written BEFORE the merge attempt — its failure aborts pre-merge
    into the recorded failure state, leaving HEAD untouched."""

    def test_waiver_write_failure_aborts_before_merge(self):
        result = self._run_to_gate("ctr_wfail")
        # The verdict must carry an offered measured waiver for the
        # "waived" completion below to reach the grant write. A run
        # no longer produces that state — a measured regression fails
        # the contract instead of reaching the gate (owner decisions
        # 2026-11-09, Item 2(a) = B) — so the offer is placed on the
        # verdict row directly, before the store is swapped for the
        # failing double.
        self.store.set_verdict_waiver(result["verdict_id"], {
            "gate": "measured", "status": "offered",
            "candidate": result["winner"],
        })
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(self.repo),
            capture_output=True, text=True).stdout.strip()
        self.store.close()
        self.store = WaiverFailingStore(self.data_dir / "data.db")
        result = loop_mod.record_human_gate(
            self.store, "ctr_wfail", "waived", "accepting the sensor cost")
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["human_gate"], "failed", result)
        self.assertNotIn("certificate", result)
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(self.repo),
            capture_output=True, text=True).stdout.strip()
        self.assertEqual(head_after, head_before)
        verdict = self.store.latest_verdict("ctr_wfail")
        self.assertEqual(verdict["human_gate"], "failed", verdict)
        self.assertEqual(self.store.get_contract("ctr_wfail")["status"],
                         "gate_failed")
        self.assertEqual(_final_answer_count(self.store, "ctr_wfail"), 0)


class TestRunClaimLifecycle(RealGitBase):
    """Spec H6 (round-5 gate-core F4): the claim file guards a live
    run only — released on every exit including exceptions, and a
    leftover claim over a contract that is not running is stale and
    broken with a named note."""

    def _claim_path(self, cid):
        mgr = WorktreeManager(self.repo)
        return mgr.root / cid / ".critic-run-claim"

    def test_exception_mid_run_releases_the_claim(self):
        with mock.patch.object(
                loop_mod, "verify_baseline",
                side_effect=RuntimeError("baseline boom")):
            with self.assertRaises(RuntimeError):
                loop_mod.run_contract(
                    self.store, _contract("ctr_boom"), self.repo,
                    self._executors(), human_decider=None)
        self.assertFalse(self._claim_path("ctr_boom").exists())

    def test_stale_claim_is_broken_with_a_note(self):
        mgr = WorktreeManager(self.repo)
        self.assertTrue(mgr.claim_run("ctr_stale"))
        result = loop_mod.run_contract(
            self.store, _contract("ctr_stale"), self.repo,
            self._executors(), human_decider=None)
        self.assertEqual(result["status"], "awaiting_human_gate", result)
        self.assertIn("stale run claim", result.get("claim_note", ""),
                      result)
        # And the successful run released its own claim on exit.
        self.assertFalse(self._claim_path("ctr_stale").exists())

    def test_claim_over_a_running_contract_answers_already_run(self):
        contract = _contract("ctr_busy")
        self.store.create_contract(
            target_id="tgt", stage=contract["stage"], task=contract["task"],
            domain=contract["domain"], tags=contract["tags"],
            acceptance=contract["acceptance"], depends_on=[],
            repo_path=str(self.repo), contract_id="ctr_busy")
        self.store.set_contract_status("ctr_busy", "running")
        mgr = WorktreeManager(self.repo)
        self.assertTrue(mgr.claim_run("ctr_busy"))
        result = loop_mod.run_contract(
            self.store, contract, self.repo,
            self._executors(), human_decider=None)
        self.assertEqual(result["status"], "already_run", result)


class TestSnapshotRef(RealGitBase):
    """Spec H6 (round-5 gate-core F3): the snapshot is pinned under
    its own ref at verdict time; the gate resolves through the ref
    after the winner branch is deleted and the object store gc'd;
    terminal completion deletes the ref."""

    def test_snapshot_survives_branch_deletion_via_its_ref(self):
        result = self._run_to_gate("ctr_ref")
        winner = self.store.get_candidate(result["winner"])
        snapshot = winner["payload_hash"]
        mgr = WorktreeManager(self.repo)
        self.assertEqual(
            mgr.snapshot_ref_commit("ctr_ref", winner["id"]), snapshot)

        # Detach the winner worktree, delete its branch, and gc: the
        # snapshot ref is the commit's only remaining anchor.
        wt = Path(winner["worktree"])
        baseline = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(self.repo),
            capture_output=True, text=True).stdout.strip()
        _git(["-C", str(wt), "checkout", "-q", baseline], self.repo)
        _git(["update-ref", "-d", f"refs/heads/critic/ctr_ref/{wt.name}"],
             self.repo)
        _git(["reflog", "expire", "--expire=now", "--all"], self.repo)
        _git(["gc", "--prune=now", "-q"], self.repo)
        probe = subprocess.run(
            ["git", "cat-file", "-e", snapshot], cwd=str(self.repo),
            capture_output=True, text=True)
        self.assertEqual(probe.returncode, 0, probe.stderr)

        done = loop_mod.record_human_gate(self.store, "ctr_ref", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["merge_source"], "verdict_snapshot")
        self.assertEqual(done["winner_commit"], f"git:{snapshot}")
        # Terminal cleanup spent the ref.
        self.assertIsNone(mgr.snapshot_ref_commit("ctr_ref", winner["id"]))


class TestEliminatedKindPersisted(RealGitBase):
    """Spec H5 (round-5 gate-core F5): an elimination's kind is
    persisted on the candidate row at the elimination site."""

    def test_hard_and_measured_kinds_are_recorded(self):
        result = loop_mod.run_contract(
            self.store, _contract("ctr_kinds", measured=True), self.repo,
            {"gen-a": LazyGen("gen-a", "fam-a"),
             "gen-b": ScriptedGen("gen-b", "fam-b", "gen-b", duration_ms=5)},
            human_decider=None)
        # Owner decisions 2026-11-09 (Item 2(a) = B): gen-b's
        # measured-only failure no longer waives it through to a
        # verdict — the run retries and escalates, so BOTH kinds stay
        # recorded on the deciding attempt's rows, gen-b's "measured"
        # included (there is no winner to strip it from). The H5
        # property is unchanged: each kind is persisted at its
        # elimination site, in the candidate's evidence.
        self.assertEqual(result["status"], "escalated", result)
        verdict = self.store.latest_verdict("ctr_kinds")
        self.assertTrue(verdict["escalated"], verdict)
        self.assertIsNone(verdict["waiver"], verdict)
        deciding = [c for c in self.store.candidates_for("ctr_kinds")
                    if c["attempt"] == verdict["attempt"]]
        by_expert = {c["expert_id"]: c for c in deciding}
        hard_loser = self.store.get_candidate(by_expert["gen-a"]["id"])
        self.assertEqual(
            (hard_loser.get("evidence") or {}).get("eliminated_kind"),
            "hard", hard_loser)
        measured_loser = self.store.get_candidate(by_expert["gen-b"]["id"])
        self.assertEqual(
            (measured_loser.get("evidence") or {}).get("eliminated_kind"),
            "measured", measured_loser)


if __name__ == "__main__":
    unittest.main()
