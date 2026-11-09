"""Round-8 fix pass, Unit 1 (gate) regression tests — real git.

The properties this file pins (fix spec L1/L4/L5; round-8 attack
F1/F4/F5, security F1/F2):

* convergence validates IDENTITY, not just existence: a planted
  row carrying the winner's own snapshot hash — a real commit, a
  matching decision, the round-8 HIGH — completes NOTHING, because
  the snapshot was never merged into main; neither does a plant
  naming the repository's root commit (the snapshot does not
  descend into it), a plant naming a real commit on an unmerged
  line, or a plant in a non-git commit form (the form leg: the
  round-7 acceptance of non-git forms is withdrawn — no
  legitimate writer produces one). Every refusal names the leg
  that failed;
* a genuine row — the merged head a real completion writes —
  still converges, and the completion result reports the row's
  commit AS STORED, byte for byte (L5: no strip-and-rewrap, the
  construction that once re-issued a plant's free text under a
  "git:" prefix);
* refusal recovery writes no process-death record (L4): present
  over a refused plant, present again, remove the plant, present
  to completion — the recovered book carries ZERO gate_failure
  observations, and so does the recovery of a plant that sat on an
  already-claimed gate. A genuine post-answer death still leaves
  exactly one gate_failure, with the post-answer reason.
"""

import sqlite3
import unittest

from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.worktrees import WorktreeManager

from tests.test_fix_round4_loop import RealGitBase, _git
from tests.test_fix_round5_loop import _final_answer_count, _observations


def _gate_failures(store, cid):
    return [o for o in _observations(store, cid)
            if o.get("kind") == "gate_failure"]


def _completions(store, cid):
    return [o for o in _observations(store, cid)
            if o.get("kind") == "gate_completion"]


def _plant_answer(store, cid, commit, human_gate):
    """A raw final_answers row, written straight through the store
    primitive — the plant construction of rounds 7 and 8."""
    contract = store.get_contract(cid)
    return store.create_final_answer(
        target_id=contract["target_id"], contract_id=cid,
        commit=commit, human_gate=human_gate)


def _delete_answers(store, cid):
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute(
            "DELETE FROM final_answers WHERE contract_id=?", (cid,))
        conn.commit()
    finally:
        conn.close()


class TestConvergenceIdentity(RealGitBase):
    """Spec L1 (round-8 attack F1 HIGH, security F1): the answer
    convergence trusts must be the merge it claims to record."""

    def test_snapshot_hash_plant_refused(self):
        result = self._run_to_gate("ctr_snap")
        winner = self.store.get_candidate(result["winner"])
        snapshot = winner["payload_hash"]
        # The round-8 plant, verbatim: the winner's REAL snapshot
        # commit and the matching decision. Round 7's checks pass
        # it (real commit, decision matches) — and the merge it
        # claims never landed: the snapshot sits on its ref and
        # branch only, unreachable from main.
        _plant_answer(self.store, "ctr_snap",
                      f"git:{snapshot}", "approved")

        refused = loop_mod.record_human_gate(
            self.store, "ctr_snap", "approved")
        self.assertEqual(refused["status"], "error", refused)
        self.assertIn("does not check out", refused["detail"])
        # The reachability leg is the one that fires: a commit is
        # its own ancestor, so ancestry passes; main never
        # received it.
        self.assertIn("not reachable", refused["detail"])
        self.assertNotIn("certificate", refused)
        self.assertEqual(_gate_failures(self.store, "ctr_snap"), [])
        self.assertEqual(_completions(self.store, "ctr_snap"), [])
        self.assertEqual(
            self.store.get_contract("ctr_snap")["status"],
            "awaiting_human_gate")
        self.assertEqual(_final_answer_count(self.store, "ctr_snap"), 1)
        # The winner's work never entered main.
        self.assertFalse((self.repo / "solution.txt").exists())

    def test_root_commit_plant_refused(self):
        self._run_to_gate("ctr_root")
        root = _git(["rev-list", "--max-parents=0", "HEAD"],
                    self.repo).stdout.strip()
        # A real commit, reachable from main — but the winner's
        # snapshot does not descend into it: it predates the
        # contract entirely. The ancestry leg refuses it.
        _plant_answer(self.store, "ctr_root", f"git:{root}", "approved")

        refused = loop_mod.record_human_gate(
            self.store, "ctr_root", "approved")
        self.assertEqual(refused["status"], "error", refused)
        self.assertIn("does not check out", refused["detail"])
        self.assertIn("does not descend", refused["detail"])
        self.assertNotIn("certificate", refused)
        self.assertEqual(_completions(self.store, "ctr_root"), [])
        self.assertEqual(
            self.store.get_contract("ctr_root")["status"],
            "awaiting_human_gate")
        self.assertFalse((self.repo / "solution.txt").exists())

    def test_unrelated_commit_plant_refused(self):
        self._run_to_gate("ctr_unrel")
        tree = _git(["rev-parse", "HEAD^{tree}"],
                    self.repo).stdout.strip()
        root = _git(["rev-list", "--max-parents=0", "HEAD"],
                    self.repo).stdout.strip()
        # A real commit object on no merged line: parented on the
        # root, carrying the root tree, reachable from nothing.
        unrelated = _git(
            ["-c", "user.email=t@local", "-c", "user.name=t",
             "commit-tree", tree, "-p", root, "-m", "unrelated"],
            self.repo).stdout.strip()
        _plant_answer(self.store, "ctr_unrel",
                      f"git:{unrelated}", "approved")

        refused = loop_mod.record_human_gate(
            self.store, "ctr_unrel", "approved")
        self.assertEqual(refused["status"], "error", refused)
        self.assertIn("does not check out", refused["detail"])
        self.assertIn("does not descend", refused["detail"])
        self.assertNotIn("certificate", refused)
        self.assertEqual(_completions(self.store, "ctr_unrel"), [])
        self.assertEqual(
            self.store.get_contract("ctr_unrel")["status"],
            "awaiting_human_gate")

    def test_non_git_commit_plant_refused(self):
        self._run_to_gate("ctr_form")
        # Round 7 accepted a non-git form as recorded — and the
        # completion then announced it as 'git:manual:xyz'
        # (round-8 attack F5). No legitimate writer produces a
        # non-git row: the form itself now fails validation.
        _plant_answer(self.store, "ctr_form", "manual:xyz", "approved")

        refused = loop_mod.record_human_gate(
            self.store, "ctr_form", "approved")
        self.assertEqual(refused["status"], "error", refused)
        self.assertIn("does not check out", refused["detail"])
        self.assertIn("does not carry", refused["detail"])
        self.assertNotIn("certificate", refused)
        self.assertEqual(_completions(self.store, "ctr_form"), [])
        self.assertEqual(
            self.store.get_contract("ctr_form")["status"],
            "awaiting_human_gate")
        self.assertEqual(_final_answer_count(self.store, "ctr_form"), 1)

    def test_merged_head_row_converges_and_reports_stored_commit(self):
        result = self._run_to_gate("ctr_genuine")
        winner = self.store.get_candidate(result["winner"])
        # The genuine row: the merge a real completion performs,
        # and the merged head it records.
        merged = WorktreeManager(self.repo).merge_commit_into_main(
            winner["payload_hash"])
        _plant_answer(self.store, "ctr_genuine",
                      f"git:{merged}", "approved")

        done = loop_mod.record_human_gate(
            self.store, "ctr_genuine", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["merge_source"], "already_completed")
        self.assertIn("certificate", done)
        # L5's pin: the result's commit byte-equals the stored
        # row's commit — reported as stored, never re-wrapped.
        stored = loop_mod._final_answer_row(self.store, "ctr_genuine")
        self.assertEqual(done["commit"], stored["commit"])
        self.assertEqual(done["commit"], f"git:{merged}")
        self.assertEqual(
            _final_answer_count(self.store, "ctr_genuine"), 1)
        self.assertEqual(
            self.store.get_contract("ctr_genuine")["status"],
            "completed")


class TestRefusalRecoveryLeavesNoDeathRecord(RealGitBase):
    """Spec L4 (round-8 attack F4, security F2): the detector must
    not write "process death" records over live refusals — neither
    when a presentation re-presents over a refused plant, nor when
    the prescribed recovery completes the gate afterwards."""

    def test_refused_plant_recovery_leaves_zero_gate_failures(self):
        # The attack's e4 construction, verbatim flow: a
        # decision-mismatch plant on a pending gate.
        result = self._run_to_gate("ctr_e4")
        winner = self.store.get_candidate(result["winner"])
        _plant_answer(self.store, "ctr_e4",
                      f"git:{winner['payload_hash']}", "rejected")

        first = loop_mod.record_human_gate(
            self.store, "ctr_e4", "approved")
        self.assertEqual(first["status"], "error", first)
        self.assertIn("does not check out", first["detail"])

        # Re-presentation over the refused plant: at the old order
        # the detector recorded a "process death after the final
        # answer" BEFORE convergence refused again.
        second = loop_mod.record_human_gate(
            self.store, "ctr_e4", "approved")
        self.assertEqual(second["status"], "error", second)
        self.assertIn("does not check out", second["detail"])
        self.assertEqual(_gate_failures(self.store, "ctr_e4"), [])

        # The prescribed recovery — remove the row, present again:
        # at the old order the refused claim it left standing read
        # as "process death after claim" and drew a second record.
        _delete_answers(self.store, "ctr_e4")
        done = loop_mod.record_human_gate(
            self.store, "ctr_e4", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["merge_source"], "verdict_snapshot")
        self.assertEqual(
            (self.repo / "solution.txt").read_text(encoding="utf-8"),
            f"solution by {winner['expert_id']}\n")
        # The healthy, genuinely merged book carries ZERO
        # process-death records from a flow in which nothing died.
        self.assertEqual(_gate_failures(self.store, "ctr_e4"), [])
        self.assertEqual(_final_answer_count(self.store, "ctr_e4"), 1)
        self.assertEqual(len(_completions(self.store, "ctr_e4")), 1)
        self.assertEqual(
            self.store.get_contract("ctr_e4")["status"], "completed")

    def test_claimed_gate_plant_refusal_records_no_death(self):
        # The attack's e1b construction: the claim landed (the
        # crash signature), then a bogus-commit plant was inserted
        # over it.
        self._run_to_gate("ctr_e1b")
        verdict = self.store.latest_verdict("ctr_e1b")
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))
        _plant_answer(self.store, "ctr_e1b",
                      "git:deadbeefcafe", "approved")

        refused = loop_mod.record_human_gate(
            self.store, "ctr_e1b", "approved")
        self.assertEqual(refused["status"], "error", refused)
        self.assertIn("does not check out", refused["detail"])
        self.assertIn("not a commit", refused["detail"])
        self.assertEqual(_gate_failures(self.store, "ctr_e1b"), [])
        # Nothing flipped either: at the old order the book rested
        # in the failure state (contract gate_failed) over a live
        # refusal; the gate is simply still claimed.
        self.assertEqual(
            self.store.get_contract("ctr_e1b")["status"],
            "awaiting_human_gate")
        self.assertEqual(
            self.store.latest_verdict("ctr_e1b")["human_gate"],
            "approved")

        _delete_answers(self.store, "ctr_e1b")
        done = loop_mod.record_human_gate(
            self.store, "ctr_e1b", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(_gate_failures(self.store, "ctr_e1b"), [])
        self.assertEqual(_final_answer_count(self.store, "ctr_e1b"), 1)

    def test_genuine_post_answer_death_still_leaves_exactly_one(self):
        # The other half of L4's acceptance: a REAL interruption —
        # claim, merge, answer, death before the status write —
        # still records exactly one gate_failure, with the
        # post-answer reason, and converges.
        result = self._run_to_gate("ctr_real")
        winner = self.store.get_candidate(result["winner"])
        verdict = self.store.latest_verdict("ctr_real")
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))
        merged = WorktreeManager(self.repo).merge_commit_into_main(
            winner["payload_hash"])
        contract = self.store.get_contract("ctr_real")
        self.store.create_final_answer(
            target_id=contract["target_id"], contract_id="ctr_real",
            commit=f"git:{merged}", human_gate="approved")

        done = loop_mod.record_human_gate(
            self.store, "ctr_real", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertIn("certificate", done)
        failures = _gate_failures(self.store, "ctr_real")
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("after the final answer", failures[0]["reason"])
        self.assertEqual(_final_answer_count(self.store, "ctr_real"), 1)
        self.assertEqual(
            self.store.get_contract("ctr_real")["status"], "completed")


if __name__ == "__main__":
    unittest.main()
