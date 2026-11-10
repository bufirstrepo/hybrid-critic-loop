"""Round-11 fix pass, Unit B (chain + state) regression tests.

Spec: FIX-SPEC-ROUND11-2026-11-09.md §Unit B. Findings closed:

* HIGH-3 / testaudit F1 — the gate-record leg's commit EQUALITY
  (``obs.commit == bare row commit``) was unpinned: mutation N1b
  (drop the conjunct) left every test green. Pinned below on a
  genuine completion whose row commit is repointed.
* testaudit F3 — the leg's decision match was unpinned (N1a).
* testaudit F4 — the verdict leg's winner-contract pin could not
  discriminate (its fixture's baseline equalled the row commit,
  so the baseline leg refused it either way; N1d survived).
  Rebuilt below with the other candidate's baseline at ``0…0``.
* testaudit F5 — the settled-status guard was unpinned; its
  apparent pins were confounded by the baseline leg (N1g
  survived). Rebuilt below on the genuine crash shape, whose row
  commit is a real merge sha that is NOT the winner's baseline.
* testaudit F6 — the leg's ``kind == "gate_completion"`` check
  was unpinned (N1c). Pinned below by relabelling the record.
* testaudit F7 — the m3c "decision mismatch control" was
  triple-confounded (settled status AND baseline commit AND
  decision mismatch). Rebuilt below single-confound: the genuine
  crash shape with ONLY the row's decision broken, plus its
  one-write control.
* attack F1 / MEDIUM-1 (HIGH-3's sibling) — leg 1 accepted ANY
  decision string, any status, and a non-sha commit: a
  ``rejected`` / ``None`` / ``"banana"`` pair on a pending
  verdict minted a verifying certificate through public Store
  APIs alone. Leg 1 now requires an answer-producing decision,
  a full-length lowercase hex sha equal to the record's, and a
  settled-completed status.
* attack F2 / MEDIUM-2 — a genuine completion's only leg-1
  evidence lived in the 50-entry observation FIFO; 50 front-door
  appends revoked it. ``gate_completion`` records are now exempt
  from the trim (state.py), pinned with the lane's exact probe
  shape.
* attack F3 / MEDIUM-3 — ``verify_certificate`` never consulted
  completion; deleting the answer after issuance left the
  certificate verifying. Verify now requires
  ``_contract_completed`` at verify time.
* attack F4 — a store whose connection exists but whose
  ``final_answers`` read raises degraded to the abolished
  status-only leg. The error path now degrades to NOT completed;
  the no-connection duck fallback is preserved exactly.
* attack F5 — the settled guard compared raw strings;
  ``"escalated "`` / ``"ESCALATED"`` re-opened a settled book to
  the crash-window leg. The guard compares the normalized
  (stripped, lowercased) status.
* attack F6 — chain read the LATEST final_answers row while
  ``loop._final_answer_row`` read the FIRST (unordered
  ``LIMIT 1``). One shared helper now serves both.
* attack F7 — the baseline exclusion compared raw strings; a
  baseline stored ``git:``-prefixed or uppercase let a row
  naming the baseline itself pass. Both sides are normalized.

Deliberately NOT pinned as refused (the spec's documented N1
residual, unchanged by this unit): a winner-bound plant carrying
a fabricated full-length ``git:`` commit and no gate record on
an UNSETTLED book still completes through the verdict leg — the
chain has no repository to check the sha against. The control in
TestSettledGuardNormalized pins that shape as completing, so a
future change to the residual is a deliberate, visible act.
"""

import json
import unittest

from hybrid_critic_loop import chain
from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.worktrees import WorktreeManager

from tests.test_fix_round4_loop import RealGitBase
from tests.test_tightening_chain import TighteningTestCase

SHA = TighteningTestCase._COMPLETE_SHA
FAB = "b" * 40  # fabricated full-length lower-hex, no one's baseline


def _pending_verdict(store, cid):
    return store.create_verdict(
        contract_id=cid, attempt=1, winner="cand_1", eliminated=[],
        hard="pass", measured="pass", judged=None, escalated=False,
        escalation_reason=None, waiver=None, human_gate="pending",
        tie_break=None)


def _plant_pair(t, cid, decision, row_commit, obs_commit,
                obs_decision="__same__", status=None):
    """A final_answers row + gate_completion observation pair,
    written through public Store APIs only (the attack F1 plant
    shape). ``obs_decision="__same__"`` mirrors the row's decision;
    pass None to omit the observation's decision key entirely."""
    tgt = t.store.get_contract(cid)["target_id"]
    t.store.create_final_answer(tgt, cid, row_commit,
                                human_gate=decision)
    obs = {"kind": "gate_completion", "commit": obs_commit,
           "channel": "pin"}
    if obs_decision == "__same__":
        obs["decision"] = decision
    elif obs_decision is not None:
        obs["decision"] = obs_decision
    t.store.append_observation(cid, obs)
    if status is not None:
        t.store.set_contract_status(cid, status)


def _plant_raw(store, cid, commit, decision):
    tgt = store.get_contract(cid)["target_id"]
    store._conn.execute(
        'INSERT INTO final_answers (target_id, contract_id, "commit",'
        " human_gate, created_at) VALUES (?,?,?,?,datetime('now'))",
        (tgt, cid, commit, decision))
    store._conn.commit()


def _set_baseline(store, candidate_id, value):
    store._conn.execute(
        "UPDATE candidates SET baseline_hash=? WHERE id=?",
        (value, candidate_id))
    store._conn.commit()


class TestLeg1Discipline(TighteningTestCase):
    """Attack F1: leg 1 gains leg 2's discipline. Each plant
    satisfies every leg-1 conjunct but one, so severing exactly
    that conjunct flips the verdict — the pins discriminate."""

    def _book(self, cid="ctr_0001"):
        self._contract(cid)
        self._dispatch(cid)
        _pending_verdict(self.store, cid)
        return cid

    def test_rejected_pair_is_not_completed(self):
        cid = self._book()
        _plant_pair(self, cid, "rejected", f"git:{SHA}", SHA,
                    status="rejected")
        self.assertFalse(chain._contract_completed(self.store, cid))
        self.assertIsNone(chain.issue_certificate(self.store, cid))

    def test_null_decision_pair_is_not_completed(self):
        cid = self._book()
        _plant_pair(self, cid, None, f"git:{SHA}", SHA,
                    obs_decision=None)
        self.assertFalse(chain._contract_completed(self.store, cid))
        self.assertIsNone(chain.issue_certificate(self.store, cid))

    def test_banana_decision_and_non_sha_commit_is_not_completed(self):
        cid = self._book()
        _plant_pair(self, cid, "banana", "git:XYZ-not-a-sha",
                    "XYZ-not-a-sha")
        self.assertFalse(chain._contract_completed(self.store, cid))
        self.assertIsNone(chain.issue_certificate(self.store, cid))

    def test_rejected_decision_on_completed_book_is_not_completed(self):
        # Only the decision-set conjunct can refuse this plant:
        # the pair agrees, the sha is full-length hex, and the
        # status IS settled-completed — but "rejected" produces
        # no answer, so its record corroborates nothing.
        cid = self._book()
        _plant_pair(self, cid, "rejected", f"git:{SHA}", SHA,
                    status="completed")
        self.assertFalse(chain._contract_completed(self.store, cid))
        self.assertIsNone(chain.issue_certificate(self.store, cid))

    def test_arbitrary_decision_on_completed_book_is_not_completed(self):
        # Same single confound, arbitrary string: "banana" with a
        # genuine-form sha on a completed book.
        cid = self._book()
        _plant_pair(self, cid, "banana", f"git:{SHA}", SHA,
                    status="completed")
        self.assertFalse(chain._contract_completed(self.store, cid))
        self.assertIsNone(chain.issue_certificate(self.store, cid))

    def test_non_sha_commit_with_answer_decision_is_not_completed(self):
        # Only the sha-form conjunct can refuse this plant: the
        # decision is answer-producing, the pair agrees, and the
        # status is settled-completed.
        cid = self._book()
        _plant_pair(self, cid, "approved", "git:XYZ-not-a-sha",
                    "XYZ-not-a-sha", status="completed")
        self.assertFalse(chain._contract_completed(self.store, cid))

    def test_short_sha_commit_is_not_completed(self):
        cid = self._book()
        _plant_pair(self, cid, "approved", "git:" + "a" * 39,
                    "a" * 39, status="completed")
        self.assertFalse(chain._contract_completed(self.store, cid))

    def test_uppercase_sha_commit_is_not_completed(self):
        cid = self._book()
        _plant_pair(self, cid, "approved", "git:" + "A" * 40,
                    "A" * 40, status="completed")
        self.assertFalse(chain._contract_completed(self.store, cid))

    def test_gate_record_without_completed_status_is_not_completed(self):
        # Only the settled-status conjunct can refuse: the pair is
        # a genuine-form approved completion, but the contract
        # never settled (status still "open", verdict pending) —
        # leg 1 corroborates the loop's COMPLETED writes only.
        cid = self._book()
        _plant_pair(self, cid, "approved", f"git:{SHA}", SHA)
        self.assertEqual(
            self.store.get_contract(cid)["status"], "open")
        self.assertFalse(chain._contract_completed(self.store, cid))
        self.assertIsNone(chain.issue_certificate(self.store, cid))

    def test_waived_pair_is_completed_control(self):
        # The other answer-producing decision, in full genuine
        # form, still completes through leg 1.
        cid = self._book()
        _plant_pair(self, cid, "waived", f"git:{SHA}", SHA,
                    status="completed")
        self.assertTrue(chain._contract_completed(self.store, cid))
        cert = chain.issue_certificate(self.store, cid)
        self.assertIsNotNone(cert)
        self.assertTrue(
            chain.verify_certificate(self.store, cert["cert"])[0])

    def test_genuine_completion_control(self):
        cid = "ctr_0001"
        self._contract(cid)
        self._dispatch(cid)
        self._verdict(cid)
        self._complete(cid)
        self.assertTrue(chain._contract_completed(self.store, cid))
        cert = chain.issue_certificate(self.store, cid)
        self.assertIsNotNone(cert)
        self.assertTrue(
            chain.verify_certificate(self.store, cert["cert"])[0])


class TestLeg1MutationPins(TighteningTestCase):
    """Testaudit F1/F3/F6: the pins the round-11 mutations N1a
    (decision match), N1b (commit equality — HIGH-3) and N1c
    (kind check) survived. Each starts from the genuine fixture
    completion and breaks exactly one binding."""

    def _genuine(self, cid="ctr_0001"):
        self._contract(cid)
        self._dispatch(cid)
        self._verdict(cid)
        self._complete(cid)
        self.assertTrue(chain._contract_completed(self.store, cid))
        return cid

    def test_decision_match_pin(self):
        cid = self._genuine()
        # The row now claims "waived"; the gate record still says
        # "approved". Agreement is the binding — it is broken.
        self.store._conn.execute(
            "UPDATE final_answers SET human_gate='waived'"
            " WHERE contract_id=?", (cid,))
        self.store._conn.commit()
        self.assertFalse(chain._contract_completed(self.store, cid))

    def test_commit_equality_pin(self):
        cid = self._genuine()
        # The row is repointed at a different full-length sha; a
        # gate record with the right decision still exists.
        self.store._conn.execute(
            'UPDATE final_answers SET "commit"=? WHERE contract_id=?',
            ("git:" + "a" * 40, cid))
        self.store._conn.commit()
        self.assertFalse(chain._contract_completed(self.store, cid))
        self.assertIsNone(chain.issue_certificate(self.store, cid))

    def test_kind_check_pin(self):
        cid = self._genuine()
        contract = self.store.get_contract(cid)
        obs = contract["observations"]
        for entry in obs:
            if isinstance(entry, dict) \
                    and entry.get("kind") == "gate_completion":
                entry["kind"] = "operator_note"
        self.store._conn.execute(
            "UPDATE contracts SET observations_json=? WHERE id=?",
            (json.dumps(obs), cid))
        self.store._conn.commit()
        self.assertFalse(chain._contract_completed(self.store, cid))


class TestGateCompletionRecordIsDurable(TighteningTestCase):
    """Attack F2 / MEDIUM-2, the lane probe's exact shape."""

    def test_fifty_appends_do_not_revoke_completion(self):
        cid = "ctr_0001"
        self._contract(cid)
        self._dispatch(cid)
        self._verdict(cid)
        self._complete(cid)
        cert = chain.issue_certificate(self.store, cid)
        self.assertIsNotNone(cert)
        for i in range(50):
            self.store.append_observation(
                cid, {"kind": "probe_noise", "n": i})
        obs = self.store.get_contract(cid)["observations"]
        # The FIFO still caps the log at 50 — and the gate record
        # is still in it.
        self.assertEqual(len(obs), 50)
        self.assertTrue(any(
            isinstance(o, dict) and o.get("kind") == "gate_completion"
            for o in obs))
        self.assertTrue(chain._contract_completed(self.store, cid))
        self.assertIsNotNone(chain.issue_certificate(self.store, cid))
        self.assertTrue(
            chain.verify_certificate(self.store, cert["cert"])[0])


class TestVerifyRequiresCompletion(TighteningTestCase):
    """Attack F3 / MEDIUM-3: verify consults completion."""

    def _issued(self, cid="ctr_0001"):
        self._contract(cid)
        self._dispatch(cid)
        self._verdict(cid)
        self._complete(cid)
        cert = chain.issue_certificate(self.store, cid)
        self.assertIsNotNone(cert)
        self.assertTrue(
            chain.verify_certificate(self.store, cert["cert"])[0])
        return cid, cert

    def test_verify_fails_after_answer_row_deleted(self):
        cid, cert = self._issued()
        self.store._conn.execute(
            "DELETE FROM final_answers WHERE contract_id=?", (cid,))
        self.store._conn.commit()
        ok, reason = chain.verify_certificate(self.store, cert["cert"])
        self.assertFalse(ok)
        self.assertIn("_contract_completed", reason)

    def test_verify_fails_after_gate_record_cleared(self):
        cid, cert = self._issued()
        self.store._conn.execute(
            "UPDATE contracts SET observations_json='[]' WHERE id=?",
            (cid,))
        self.store._conn.commit()
        ok, reason = chain.verify_certificate(self.store, cert["cert"])
        self.assertFalse(ok)
        self.assertIn("_contract_completed", reason)


class _BoomConn:
    def execute(self, *args, **kwargs):
        raise RuntimeError("final_answers: no such table (simulated)")


class _BoomStore:
    """A connection surface exists; every read through it raises."""

    def __init__(self, status, with_contract=True):
        self._conn = _BoomConn()
        if with_contract:
            self.get_contract = lambda cid: {
                "status": status, "observations": []}


class _NoConnStore:
    def __init__(self, status):
        self.get_contract = lambda cid: {
            "status": status, "observations": []}


class TestErrorPathDegradation(unittest.TestCase):
    """Attack F4: a raising row read degrades to NOT completed,
    never to the status leg. The no-connection duck fallback —
    a store with no connection surface at all — is the pre-N1
    behavior and is preserved exactly."""

    def test_raising_read_with_completed_status_is_not_completed(self):
        self.assertFalse(chain._contract_completed(
            _BoomStore("completed"), "ctr_x"))

    def test_raising_read_with_awaiting_status_is_not_completed(self):
        self.assertFalse(chain._contract_completed(
            _BoomStore("awaiting_human_gate"), "ctr_x"))

    def test_raising_read_without_contract_surface_is_not_completed(self):
        self.assertFalse(chain._contract_completed(
            _BoomStore("completed", with_contract=False), "ctr_x"))

    def test_no_connection_duck_fallback_preserved(self):
        self.assertTrue(chain._contract_completed(
            _NoConnStore("completed"), "ctr_x"))
        self.assertFalse(chain._contract_completed(
            _NoConnStore("awaiting_human_gate"), "ctr_x"))


class TestDroppedTableIsNotCompletion(TighteningTestCase):
    """Attack F4 on the real Store: the table the read needs is
    gone, the status still says completed."""

    def test_dropped_final_answers_table_is_not_completed(self):
        cid = "ctr_0001"
        self._contract(cid)
        self._dispatch(cid)
        self._verdict(cid)
        self._complete(cid)
        self.assertTrue(chain._contract_completed(self.store, cid))
        self.store._conn.execute("DROP TABLE final_answers")
        self.store._conn.commit()
        self.assertFalse(chain._contract_completed(self.store, cid))


class TestSettledGuardNormalized(TighteningTestCase):
    """Attack F5 + testaudit F5 at store level: the crash shape
    (decided verdict, winner of this contract, fabricated
    full-length sha that is NOT the winner's baseline, no gate
    record) under status spellings a raw-string guard misses."""

    def _crash_book(self, cid="ctr_0001"):
        self._contract(cid)
        self._dispatch(cid)
        self._verdict(cid)  # human_gate="approved", winner="cand_1"
        _set_baseline(self.store, "cand_1", "a" * 40)
        _plant_raw(self.store, cid, f"git:{FAB}", "approved")
        return cid

    def test_settled_spellings_are_refused(self):
        for i, status in enumerate(("escalated", "escalated ",
                                    "ESCALATED", "completed ",
                                    "Refused")):
            cid = self._crash_book(f"ctr_s{i}")
            self.store.set_contract_status(cid, status)
            self.assertFalse(
                chain._contract_completed(self.store, cid),
                f"status {status!r} must read settled")

    def test_unsettled_control_still_completes(self):
        # The documented N1 residual, pinned as a control: the
        # same crash shape on a genuinely unsettled book completes
        # through the verdict leg. This unit does not close it.
        cid = self._crash_book()
        self.store.set_contract_status(cid, "awaiting_human_gate")
        self.assertTrue(chain._contract_completed(self.store, cid))


class TestBaselineExclusionNormalized(TighteningTestCase):
    """Attack F7: the row naming the winner's own baseline is
    refused however the baseline was stored."""

    def _crash_book(self, cid="ctr_0001"):
        self._contract(cid)
        self._dispatch(cid)
        self._verdict(cid)
        return cid

    def test_git_prefixed_baseline_is_still_the_baseline(self):
        cid = self._crash_book()
        _set_baseline(self.store, "cand_1", f"git:{'c' * 40}")
        _plant_raw(self.store, cid, f"git:{'c' * 40}", "approved")
        self.assertFalse(chain._contract_completed(self.store, cid))

    def test_uppercase_baseline_is_still_the_baseline(self):
        cid = self._crash_book()
        _set_baseline(self.store, "cand_1", "C" * 40)
        _plant_raw(self.store, cid, f"git:{'c' * 40}", "approved")
        self.assertFalse(chain._contract_completed(self.store, cid))

    def test_non_baseline_commit_control_completes(self):
        cid = self._crash_book()
        _set_baseline(self.store, "cand_1", f"git:{'c' * 40}")
        _plant_raw(self.store, cid, f"git:{FAB}", "approved")
        self.assertTrue(chain._contract_completed(self.store, cid))


class TestLoopAndChainReadSameRow(TighteningTestCase):
    """Attack F6: on a multi-row book both readers govern on the
    LATEST row by rowid."""

    def _book(self, cid="ctr_0001"):
        self._contract(cid)
        self._dispatch(cid)
        self._verdict(cid)
        _set_baseline(self.store, "cand_1", "a" * 40)
        return cid

    def test_latest_row_governs_both_readers(self):
        cid = self._book()
        _plant_raw(self.store, cid, "git:" + "d" * 40, "rejected")
        _plant_raw(self.store, cid, f"git:{FAB}", "approved")
        row = loop_mod._final_answer_row(self.store, cid)
        self.assertEqual(row["commit"], f"git:{FAB}")
        self.assertEqual(row["human_gate"], "approved")
        self.assertTrue(chain._contract_completed(self.store, cid))

    def test_latest_junk_row_governs_both_readers(self):
        cid = self._book()
        _plant_raw(self.store, cid, f"git:{SHA}", "approved")
        self.store.append_observation(cid, {
            "kind": "gate_completion", "decision": "approved",
            "commit": SHA})
        self.store.set_contract_status(cid, "completed")
        _plant_raw(self.store, cid, "git:" + "e" * 40, "rejected")
        row = loop_mod._final_answer_row(self.store, cid)
        self.assertEqual(row["commit"], "git:" + "e" * 40)
        self.assertEqual(row["human_gate"], "rejected")
        self.assertFalse(chain._contract_completed(self.store, cid))


class _CrashShapeBase(RealGitBase):
    """The genuine crash shape (round-10 TestVerdictLegBinding's
    fixture): claimed verdict, real merge, genuine-form row, no
    gate record, unsettled status."""

    def _crash_shape(self, cid):
        result = self._run_to_gate(cid)
        winner = self.store.get_candidate(result["winner"])
        verdict = self.store.latest_verdict(cid)
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))
        merged = WorktreeManager(self.repo).merge_commit_into_main(
            winner["payload_hash"])
        contract = self.store.get_contract(cid)
        self.store.create_final_answer(
            target_id=contract["target_id"], contract_id=cid,
            commit=f"git:{merged}", human_gate="approved")
        records = [o for o in contract["observations"]
                   if isinstance(o, dict)
                   and o.get("kind") == "gate_completion"]
        self.assertEqual(records, [])
        return result, winner, merged


class TestWinnerContractPinDiscriminating(_CrashShapeBase):
    """Testaudit F4: the winner-contract pin rebuilt so the
    baseline leg cannot rescue it — the other contract's
    candidate carries baseline ``0…0``, which is NOT the row's
    commit, so only the contract check refuses."""

    def test_winner_bound_to_another_contract_is_not_completed(self):
        _result, _winner, merged = self._crash_shape("ctr_wa11")
        # Control: bound to its own contract's winner, the crash
        # shape completes.
        self.assertTrue(
            chain._contract_completed(self.store, "ctr_wa11"))
        contract_a = self.store.get_contract("ctr_wa11")
        self.store.create_contract(
            contract_a["target_id"], "implement", "Write other.txt.",
            "code.game", ["fix-round11"], [], contract_id="ctr_wb11")
        other = self.store.create_candidate(
            "ctr_wb11", 1, "implement", "gen-b", "fam-b", "b", "",
            "0" * 40)
        verdict_a = self.store.latest_verdict("ctr_wa11")
        self.store._conn.execute(
            "UPDATE verdicts SET winner=? WHERE id=?",
            (other, verdict_a["id"]))
        self.store._conn.commit()
        self.assertFalse(
            chain._contract_completed(self.store, "ctr_wa11"))
        self.assertIsNone(
            chain.issue_certificate(self.store, "ctr_wa11"))


class TestSettledStatusPinDiscriminating(_CrashShapeBase):
    """Testaudit F5: the settled guard's pin rebuilt on the
    genuine crash shape — the row's commit is a real merge sha,
    NOT the winner's baseline, so the baseline leg cannot refuse
    this fixture; only the settled guard can."""

    def test_settled_status_refuses_the_crash_shape(self):
        _result, winner, _merged = self._crash_shape("ctr_set11")
        # The fixture discriminates: the row's commit is not the
        # winner candidate's recorded baseline.
        row = loop_mod._final_answer_row(self.store, "ctr_set11")
        self.assertNotEqual(
            row["commit"], f"git:{winner['baseline_hash']}")
        self.assertTrue(
            chain._contract_completed(self.store, "ctr_set11"))
        self.store.set_contract_status("ctr_set11", "rejected")
        self.assertFalse(
            chain._contract_completed(self.store, "ctr_set11"))
        # And the same settled status in a spelling the old
        # raw-string guard missed (attack F5).
        self.store.set_contract_status("ctr_set11", "rejected ")
        self.assertFalse(
            chain._contract_completed(self.store, "ctr_set11"))


class TestM3cControlSingleConfound(_CrashShapeBase):
    """Testaudit F7: the m3c decision-mismatch control rebuilt
    with exactly ONE broken binding. The round-10 original
    (test_fix_round10_chain.py) stacks three refusals — settled
    status, row commit == winner baseline, decision mismatch —
    so it discriminates none of them. Here the book is the
    genuine crash shape (unsettled, real merge sha, winner of
    this contract, no gate record); only the row's recorded
    decision disagrees with the verdict's claim."""

    def test_decision_mismatch_alone_refuses(self):
        _result, _winner, merged = self._crash_shape("ctr_m3c11")
        # Rebuild the row with the mismatched decision only.
        self.store._conn.execute(
            "DELETE FROM final_answers WHERE contract_id=?",
            ("ctr_m3c11",))
        self.store._conn.commit()
        contract = self.store.get_contract("ctr_m3c11")
        self.store.create_final_answer(
            target_id=contract["target_id"], contract_id="ctr_m3c11",
            commit=f"git:{merged}", human_gate="waived")
        self.assertFalse(
            chain._contract_completed(self.store, "ctr_m3c11"))
        self.assertIsNone(
            chain.issue_certificate(self.store, "ctr_m3c11"))
        # One-write control: the same row recording the verdict's
        # decision completes — nothing else about the book moved.
        self.store._conn.execute(
            "UPDATE final_answers SET human_gate='approved'"
            " WHERE contract_id=?", ("ctr_m3c11",))
        self.store._conn.commit()
        self.assertTrue(
            chain._contract_completed(self.store, "ctr_m3c11"))


if __name__ == "__main__":
    unittest.main()
