"""Round-9 fix pass, Unit B (chain) regression tests — M3.

The property this file pins (fix spec M3; round-9 gate-core P3):
``chain._contract_completed`` must not rest on a ``final_answers``
row's mere existence. On the real Store a row counts toward
completion only when the contract's own gate record corroborates
it — the contract reached the terminal completed status, or the
latest verdict's ``human_gate`` is one of the answer-producing
decisions (approved, waived) and equals the decision the row
itself records, the same agreement the loop's convergence
validation demands of a row it trusts.

The lane-3 P3 shape is the head case: a measured-regression run
escalates (status "escalated", verdict gate "not_reached"), one
planted row carrying a genuine merged commit made
``_contract_completed`` return True while ``record_human_gate``
refused the same book. Genuine completions — the normal approval
path, a crash-converged completion (decided verdict + genuine row,
terminal status never written), and a pre-ruling offered-waiver
completion — must stay completed.
"""

import sqlite3
import unittest

from hybrid_critic_loop import chain
from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.contracts import validate_contract
from hybrid_critic_loop.worktrees import WorktreeManager

from tests.test_fix_round4_loop import RealGitBase, _git


def _regression_contract(cid):
    """A contract every candidate regresses on the measured gate
    (ops 99 against <= 8): since the owner-decisions pass (Item 2
    = B) the run retries and escalates with the human gate never
    reached — the P3 run shape."""
    return validate_contract({
        "id": cid, "stage": "implement", "task": "Write solution.txt.",
        "domain": "code.game", "tags": ["fix-round9"],
        "acceptance": [
            {"class": "hard", "cmd": "test -f solution.txt",
             "expect": {"exit": 0}},
            {"class": "measured",
             "metrics": [{"name": "ops", "op": "<=", "value": 8}],
             "sensor": "ops_bench",
             "sensor_cmd": (
                 "python3 -c \"import json; "
                 "print(json.dumps({'ops': 99}))\"")},
        ],
    })


def _plant_answer_raw(store, cid, commit, decision):
    """One raw INSERT straight at the table — the lane-3 P3 plant,
    verbatim shape (the store primitive would be a writer; this is
    a row nobody's machinery produced)."""
    store._conn.execute(
        "INSERT INTO final_answers (target_id, contract_id,"
        " \"commit\", human_gate, created_at)"
        " VALUES (?, ?, ?, ?, datetime('now'))",
        (store.get_contract(cid)["target_id"], cid, commit, decision))
    store._conn.commit()


def _delete_answers(store, cid):
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute(
            "DELETE FROM final_answers WHERE contract_id=?", (cid,))
        conn.commit()
    finally:
        conn.close()


def _head(repo):
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


class TestPlantedRowOnEscalatedContract(RealGitBase):
    """M3's head case: the lane-3 P3 reproduction."""

    def test_escalated_plus_planted_row_is_not_completed(self):
        result = loop_mod.run_contract(
            self.store, _regression_contract("ctr_esc"), self.repo,
            self._executors(), human_decider=None)
        self.assertEqual(result["status"], "escalated", result)
        self.assertEqual(
            self.store.get_contract("ctr_esc")["status"], "escalated")
        self.assertEqual(
            self.store.latest_verdict("ctr_esc")["human_gate"],
            "not_reached")
        # The plant: a genuine merged commit (the repo's own HEAD)
        # and a plausible decision — existence plus a real commit
        # was enough for the old row leg.
        _plant_answer_raw(self.store, "ctr_esc",
                          f"git:{_head(self.repo)}", "approved")
        self.assertFalse(chain._contract_completed(self.store, "ctr_esc"))
        self.assertIsNone(chain.issue_certificate(self.store, "ctr_esc"))
        # The gate side refuses the same book — chain and gate now
        # agree on what this contract's record says.
        refused = loop_mod.record_human_gate(
            self.store, "ctr_esc", "approved")
        self.assertEqual(refused["status"], "error", refused)
        self.assertNotIn("certificate", refused)


class TestPlantedRowOnUndecidedGate(RealGitBase):
    """A stray row over a gate no decision has produced: pending,
    and claimed-but-mismatched."""

    def test_pending_gate_plus_planted_row_is_not_completed(self):
        self._run_to_gate("ctr_pend")
        self.assertEqual(
            self.store.latest_verdict("ctr_pend")["human_gate"],
            "pending")
        _plant_answer_raw(self.store, "ctr_pend",
                          f"git:{_head(self.repo)}", "approved")
        self.assertFalse(
            chain._contract_completed(self.store, "ctr_pend"))
        self.assertIsNone(
            chain.issue_certificate(self.store, "ctr_pend"))
        # Control: remove the plant and decide the gate for real —
        # the same book completes and certifies, so the refusal
        # above is the completion gate firing, not another
        # issuance gate's.
        _delete_answers(self.store, "ctr_pend")
        done = loop_mod.record_human_gate(
            self.store, "ctr_pend", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertTrue(chain._contract_completed(self.store, "ctr_pend"))
        self.assertIsNotNone(
            chain.issue_certificate(self.store, "ctr_pend"))

    def test_claimed_gate_with_mismatched_row_is_not_completed(self):
        self._run_to_gate("ctr_mismatch")
        verdict = self.store.latest_verdict("ctr_mismatch")
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))
        # The verdict's claim records "approved"; the row claims
        # "waived". The gate side's convergence refuses exactly
        # this disagreement, so the row corroborates nothing here
        # either — a decided verdict alone does not carry a row
        # that records a different decision.
        _plant_answer_raw(self.store, "ctr_mismatch",
                          f"git:{_head(self.repo)}", "waived")
        self.assertFalse(
            chain._contract_completed(self.store, "ctr_mismatch"))
        self.assertIsNone(
            chain.issue_certificate(self.store, "ctr_mismatch"))


class TestGenuineCompletionsStayCompleted(RealGitBase):
    """The corroboration legs must keep every genuine shape True."""

    def test_normal_approval_completion(self):
        self._run_to_gate("ctr_ok")
        done = loop_mod.record_human_gate(
            self.store, "ctr_ok", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertTrue(chain._contract_completed(self.store, "ctr_ok"))
        self.assertIsNotNone(
            chain.issue_certificate(self.store, "ctr_ok"))

    def test_crash_converged_completion(self):
        # The round-8 ctr_real construction: claim, merge, answer —
        # then death before the terminal status write. The durable
        # shape is a decided verdict and a genuine row over a
        # contract still reading "awaiting_human_gate".
        result = self._run_to_gate("ctr_crash")
        winner = self.store.get_candidate(result["winner"])
        verdict = self.store.latest_verdict("ctr_crash")
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))
        merged = WorktreeManager(self.repo).merge_commit_into_main(
            winner["payload_hash"])
        contract = self.store.get_contract("ctr_crash")
        self.store.create_final_answer(
            target_id=contract["target_id"], contract_id="ctr_crash",
            commit=f"git:{merged}", human_gate="approved")
        self.assertEqual(
            self.store.get_contract("ctr_crash")["status"],
            "awaiting_human_gate")
        # The status leg reads False here by construction: the
        # verdict corroboration is the leg that carries this one.
        self.assertTrue(
            chain._contract_completed(self.store, "ctr_crash"))
        # And the shape still converges for real, staying completed.
        done = loop_mod.record_human_gate(
            self.store, "ctr_crash", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertTrue(
            chain._contract_completed(self.store, "ctr_crash"))
        self.assertIsNotNone(
            chain.issue_certificate(self.store, "ctr_crash"))

    def test_pre_ruling_offered_waiver_completion(self):
        # A verdict carrying a pre-ruling OFFERED waiver (placed on
        # the row the re-based fixtures' way) completes by an
        # explicit "waived": verdict and row both record the waiver
        # decision, and the completion must stay completed.
        result = self._run_to_gate("ctr_waive")
        winner = self.store.get_candidate(result["winner"])
        self.store.set_verdict_waiver(result["verdict_id"], {
            "gate": "measured", "status": "offered",
            "candidate": winner["id"]})
        done = loop_mod.record_human_gate(
            self.store, "ctr_waive", "waived", "owner accepts it")
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(
            self.store.latest_verdict("ctr_waive")["human_gate"],
            "waived")
        self.assertTrue(
            chain._contract_completed(self.store, "ctr_waive"))
        cert = chain.issue_certificate(self.store, "ctr_waive")
        self.assertIsNotNone(cert)
        self.assertTrue(cert["payload"]["waived"])


if __name__ == "__main__":
    unittest.main()
