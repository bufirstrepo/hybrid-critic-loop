"""Round-10 fix pass, Unit N1 (chain) regression tests.

The property this file pins (fix spec N1; round-10 lanes 1+3,
convergent MEDIUM): ``chain._contract_completed`` must bind a
``final_answers`` row's IDENTITY, not a decision string two
records merely agree on. Round 9 (M3) required the latest
verdict's ``human_gate`` to equal the row's recorded decision;
round 10 showed two plants still minting verifying certificates:

* one ``set_contract_status(cid, "completed")`` flip on an
  awaiting book — verdict pending, NO answer row at all —
  completed through the status leg alone;
* a verdict row planted in agreement with an answer row (the
  verdict written through the store's own ``create_verdict``, or
  the genuine verdict merely claimed) plus a fabricated commit
  (``git:deadbeefcafe``, or the repo's real main HEAD) read
  completed, wedging genuine convergence behind the one-row
  guard while certs minted.

The rule now: a row is REQUIRED, read deterministically (latest
by insertion governs), and it counts only when the gate's own
``gate_completion`` observation corroborates its decision AND
commit (the observation stores the merged sha BARE, the row
``git:<sha>``), or — the crash window — the gate never settled,
the latest verdict's answer-producing decision equals the row's,
the verdict's winner resolves to a candidate of this contract,
and the row's commit is a full-length ``git:`` sha that is not
the winner's own pre-merge baseline. Genuine completions —
fresh, converged, the winner-bound crash window, and the
pre-ruling offered waiver — must stay completed.

Deliberately NOT pinned (the spec's documented residual): a
winner-bound plant carrying a fabricated full-length ``git:``
commit and no gate record on an unsettled book — store-locally
indistinguishable from the crash window; the gate's own
convergence validation, which has the repository, refuses it.
"""

import unittest

from hybrid_critic_loop import chain
from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.worktrees import WorktreeManager

from tests.test_fix_round4_loop import RealGitBase
from tests.test_fix_round9_chain import (
    _head, _plant_answer_raw, _regression_contract)


def _latest_candidate_id(store, cid):
    row = store._conn.execute(
        "SELECT id FROM candidates WHERE contract_id=?"
        " ORDER BY rowid DESC LIMIT 1", (cid,)).fetchone()
    return row[0]


def _gate_records(store, cid):
    return [obs for obs in store.get_contract(cid)["observations"]
            if isinstance(obs, dict)
            and obs.get("kind") == "gate_completion"]


class TestStatusFlipOnlyIsNotCompletion(RealGitBase):
    """Lane 3 F1 / probe3 P1: the status leg stood alone."""

    def test_status_flip_without_any_row_is_not_completed(self):
        self._run_to_gate("ctr_flip")
        self.assertEqual(
            self.store.latest_verdict("ctr_flip")["human_gate"],
            "pending")
        # The plant is ONE write: the terminal status, nothing else.
        self.store.set_contract_status("ctr_flip", "completed")
        self.assertFalse(
            chain._contract_completed(self.store, "ctr_flip"))
        self.assertIsNone(
            chain.issue_certificate(self.store, "ctr_flip"))
        # Control: restore the awaiting status and decide the gate
        # for real — the same book completes and certifies, so the
        # refusal above is the completion rule firing.
        self.store.set_contract_status(
            "ctr_flip", "awaiting_human_gate")
        done = loop_mod.record_human_gate(
            self.store, "ctr_flip", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertTrue(
            chain._contract_completed(self.store, "ctr_flip"))
        self.assertIsNotNone(
            chain.issue_certificate(self.store, "ctr_flip"))


class TestTwoTablePlantIsNotCompletion(RealGitBase):
    """Lane 3 F2 + lane 1 F1 / probe3 P2: a claimed verdict and a
    planted row agreeing on the decision string "approved"."""

    def _claimed_book(self, cid):
        result = self._run_to_gate(cid)
        verdict = self.store.latest_verdict(cid)
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))
        return result

    def test_fabricated_short_commit_is_not_completed(self):
        self._claimed_book("ctr_fake")
        _plant_answer_raw(self.store, "ctr_fake",
                          "git:deadbeefcafe", "approved")
        self.assertFalse(
            chain._contract_completed(self.store, "ctr_fake"))
        self.assertIsNone(
            chain.issue_certificate(self.store, "ctr_fake"))

    def test_main_head_commit_is_not_completed(self):
        # The plant reaches for the repo's real HEAD — which is
        # the winner candidate's recorded baseline: the merge a
        # genuine completion records is a NEW commit (--no-ff),
        # never the pre-merge head.
        result = self._claimed_book("ctr_mainhead")
        winner = self.store.get_candidate(result["winner"])
        self.assertEqual(_head(self.repo), winner["baseline_hash"])
        _plant_answer_raw(self.store, "ctr_mainhead",
                          f"git:{_head(self.repo)}", "approved")
        self.assertFalse(
            chain._contract_completed(self.store, "ctr_mainhead"))
        self.assertIsNone(
            chain.issue_certificate(self.store, "ctr_mainhead"))


class TestEscalatedPlantStaysRefused(RealGitBase):
    """The m3/m3b two-table plant on a measured-regression book:
    status stays "escalated" — a gate-settled status the verdict
    leg can never corroborate through."""

    def test_verdict_update_plus_row_is_not_completed(self):
        result = loop_mod.run_contract(
            self.store, _regression_contract("ctr_m3a"), self.repo,
            self._executors(), human_decider=None)
        self.assertEqual(result["status"], "escalated", result)
        winner = _latest_candidate_id(self.store, "ctr_m3a")
        verdict = self.store.latest_verdict("ctr_m3a")
        self.store._conn.execute(
            "UPDATE verdicts SET human_gate='approved', winner=?"
            " WHERE id=?", (winner, verdict["id"]))
        self.store._conn.commit()
        _plant_answer_raw(self.store, "ctr_m3a",
                          f"git:{_head(self.repo)}", "approved")
        self.assertFalse(
            chain._contract_completed(self.store, "ctr_m3a"))
        self.assertIsNone(
            chain.issue_certificate(self.store, "ctr_m3a"))

    def test_created_verdict_plus_row_is_not_completed(self):
        result = loop_mod.run_contract(
            self.store, _regression_contract("ctr_m3b"), self.repo,
            self._executors(), human_decider=None)
        self.assertEqual(result["status"], "escalated", result)
        winner = _latest_candidate_id(self.store, "ctr_m3b")
        # The store's OWN public writer plants the verdict half.
        self.store.create_verdict(
            "ctr_m3b", 3, winner, [], "pass", "pass", None, False,
            None, None, "approved", None)
        contract = self.store.get_contract("ctr_m3b")
        self.store.create_final_answer(
            target_id=contract["target_id"], contract_id="ctr_m3b",
            commit=f"git:{_head(self.repo)}", human_gate="approved")
        self.assertFalse(
            chain._contract_completed(self.store, "ctr_m3b"))
        self.assertIsNone(
            chain.issue_certificate(self.store, "ctr_m3b"))

    def test_decision_mismatch_control_stays_refused(self):
        # m3's control: verdict approved, row records "waived" —
        # disagreement corroborates nothing on any leg.
        result = loop_mod.run_contract(
            self.store, _regression_contract("ctr_m3c"), self.repo,
            self._executors(), human_decider=None)
        self.assertEqual(result["status"], "escalated", result)
        winner = _latest_candidate_id(self.store, "ctr_m3c")
        verdict = self.store.latest_verdict("ctr_m3c")
        self.store._conn.execute(
            "UPDATE verdicts SET human_gate='approved', winner=?"
            " WHERE id=?", (winner, verdict["id"]))
        self.store._conn.commit()
        _plant_answer_raw(self.store, "ctr_m3c",
                          f"git:{_head(self.repo)}", "waived")
        self.assertFalse(
            chain._contract_completed(self.store, "ctr_m3c"))
        self.assertIsNone(
            chain.issue_certificate(self.store, "ctr_m3c"))


class TestVerdictLegBinding(RealGitBase):
    """The crash window's identity checks, one severed at a time.
    Each fixture is the genuine crash shape — claimed verdict,
    real merge, genuine-form row, no gate record, unsettled
    status — with exactly one binding broken."""

    def _crash_shape(self, cid):
        result = self._run_to_gate(cid)
        winner = self.store.get_candidate(result["winner"])
        verdict = self.store.latest_verdict(cid)
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))
        merged = WorktreeManager(self.repo).merge_commit_into_main(
            winner["payload_hash"])
        self.assertEqual(_gate_records(self.store, cid), [])
        return result, winner, merged

    def test_winner_bound_to_another_contract_is_not_completed(self):
        result_a, _winner_a, merged_a = self._crash_shape("ctr_wa")
        # A candidate of a DIFFERENT contract, seeded at store
        # level (a second full run is impossible here: A's merge
        # put solution.txt on main, so B's identical contract
        # would be refused as vacuous).
        contract_a = self.store.get_contract("ctr_wa")
        self.store.create_contract(
            contract_a["target_id"], "implement", "Write other.txt.",
            "code.game", ["fix-round10"], [], contract_id="ctr_wb")
        other = self.store.create_candidate(
            "ctr_wb", 1, "implement", "gen-b", "fam-b", "b", "",
            _head(self.repo))
        # The verdict on A now names B's candidate: the row on A
        # is bound to a candidate of a DIFFERENT contract.
        verdict_a = self.store.latest_verdict("ctr_wa")
        self.store._conn.execute(
            "UPDATE verdicts SET winner=? WHERE id=?",
            (other, verdict_a["id"]))
        self.store._conn.commit()
        contract = self.store.get_contract("ctr_wa")
        self.store.create_final_answer(
            target_id=contract["target_id"], contract_id="ctr_wa",
            commit=f"git:{merged_a}", human_gate="approved")
        self.assertFalse(
            chain._contract_completed(self.store, "ctr_wa"))
        self.assertIsNone(
            chain.issue_certificate(self.store, "ctr_wa"))

    def test_winner_bound_to_nothing_is_not_completed(self):
        result, _winner, merged = self._crash_shape("ctr_ghost")
        verdict = self.store.latest_verdict("ctr_ghost")
        self.store._conn.execute(
            "UPDATE verdicts SET winner='cand_ghost' WHERE id=?",
            (verdict["id"],))
        self.store._conn.commit()
        contract = self.store.get_contract("ctr_ghost")
        self.store.create_final_answer(
            target_id=contract["target_id"],
            contract_id="ctr_ghost",
            commit=f"git:{merged}", human_gate="approved")
        self.assertFalse(
            chain._contract_completed(self.store, "ctr_ghost"))

    def test_non_git_commit_is_not_completed(self):
        cid = "ctr_bare"
        _result, _winner, merged = self._crash_shape(cid)
        contract = self.store.get_contract(cid)
        # The genuine merge's sha, but NOT in git: form — no
        # legitimate writer produces this row.
        self.store.create_final_answer(
            target_id=contract["target_id"], contract_id=cid,
            commit=merged, human_gate="approved")
        self.assertFalse(chain._contract_completed(self.store, cid))
        self.assertIsNone(chain.issue_certificate(self.store, cid))

    def test_decision_mismatch_is_not_completed(self):
        cid = "ctr_mismatch10"
        _result, _winner, merged = self._crash_shape(cid)
        contract = self.store.get_contract(cid)
        # Verdict claimed "approved"; the row records "waived".
        self.store.create_final_answer(
            target_id=contract["target_id"], contract_id=cid,
            commit=f"git:{merged}", human_gate="waived")
        self.assertFalse(chain._contract_completed(self.store, cid))
        self.assertIsNone(chain.issue_certificate(self.store, cid))


class TestLatestRowGoverns(RealGitBase):
    """Lane 3 LOW: the row read was LIMIT 1 unordered. With two
    planted rows, the LATEST by insertion governs — both orders."""

    def _two_row_book(self, cid, first, second):
        result = self._run_to_gate(cid)
        winner = self.store.get_candidate(result["winner"])
        verdict = self.store.latest_verdict(cid)
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))
        merged = WorktreeManager(self.repo).merge_commit_into_main(
            winner["payload_hash"])
        _plant_answer_raw(self.store, cid, f"git:{merged}", first)
        _plant_answer_raw(self.store, cid, f"git:{merged}", second)
        return merged

    def test_latest_row_approved_completes(self):
        self._two_row_book("ctr_rows_ok", "waived", "approved")
        self.assertTrue(
            chain._contract_completed(self.store, "ctr_rows_ok"))

    def test_latest_row_waived_does_not_complete(self):
        self._two_row_book("ctr_rows_no", "approved", "waived")
        self.assertFalse(
            chain._contract_completed(self.store, "ctr_rows_no"))
        self.assertIsNone(
            chain.issue_certificate(self.store, "ctr_rows_no"))


class TestGenuineCompletionsStayCompleted(RealGitBase):
    """Every genuine shape the new legs must keep True."""

    def test_fresh_completion_via_gate_record(self):
        self._run_to_gate("ctr_fresh")
        done = loop_mod.record_human_gate(
            self.store, "ctr_fresh", "approved")
        self.assertEqual(done["status"], "completed", done)
        # The gate record is on the book and matches the row.
        records = _gate_records(self.store, "ctr_fresh")
        self.assertEqual(len(records), 1)
        self.assertTrue(
            chain._contract_completed(self.store, "ctr_fresh"))
        self.assertIsNotNone(
            chain.issue_certificate(self.store, "ctr_fresh"))

    def test_crash_window_is_completed(self):
        # Row + decided verdict + winner bound + git: commit,
        # status non-terminal, NO gate record — the verdict leg
        # carries exactly this shape.
        result = self._run_to_gate("ctr_crash10")
        winner = self.store.get_candidate(result["winner"])
        verdict = self.store.latest_verdict("ctr_crash10")
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))
        merged = WorktreeManager(self.repo).merge_commit_into_main(
            winner["payload_hash"])
        contract = self.store.get_contract("ctr_crash10")
        self.store.create_final_answer(
            target_id=contract["target_id"],
            contract_id="ctr_crash10",
            commit=f"git:{merged}", human_gate="approved")
        self.assertEqual(
            self.store.get_contract("ctr_crash10")["status"],
            "awaiting_human_gate")
        self.assertEqual(_gate_records(self.store, "ctr_crash10"), [])
        self.assertTrue(
            chain._contract_completed(self.store, "ctr_crash10"))
        self.assertIsNotNone(
            chain.issue_certificate(self.store, "ctr_crash10"))

    def test_converged_completion_stays_completed(self):
        # The crash shape, then the gate converges on it for real:
        # the gate record lands and the status settles.
        result = self._run_to_gate("ctr_conv10")
        winner = self.store.get_candidate(result["winner"])
        verdict = self.store.latest_verdict("ctr_conv10")
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))
        merged = WorktreeManager(self.repo).merge_commit_into_main(
            winner["payload_hash"])
        contract = self.store.get_contract("ctr_conv10")
        self.store.create_final_answer(
            target_id=contract["target_id"],
            contract_id="ctr_conv10",
            commit=f"git:{merged}", human_gate="approved")
        done = loop_mod.record_human_gate(
            self.store, "ctr_conv10", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(
            self.store.get_contract("ctr_conv10")["status"],
            "completed")
        self.assertTrue(
            chain._contract_completed(self.store, "ctr_conv10"))
        self.assertIsNotNone(
            chain.issue_certificate(self.store, "ctr_conv10"))


if __name__ == "__main__":
    unittest.main()
