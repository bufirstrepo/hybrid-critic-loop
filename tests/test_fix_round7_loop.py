"""Round-7 fix pass, Unit 1 (gate) regression tests — real git.

The properties this file pins (fix spec K1/K2/K3/K6; round-7 attack
F1, security F1/F2/F3, gate-core F1/F2/F3/F4/F5):

* convergence validates the final answer it trusts: a planted row
  whose recorded decision differs, or whose "git:" commit names no
  commit in the contract's repository, completes NOTHING — the
  presentation refuses naming the failed check, records no failure
  state, and does not consume the gate (remove the row and the same
  presentation completes through the real merge); a well-formed
  row (real commit, matching decision) still converges;
* death after the final-answer write — verdict decided, answer
  present, terminal status never written — is an interrupted
  completion: the recorded decision converges it (status, ref
  cleanup, certificate), and the retry binding refuses any other
  decision first;
* two presenters racing one gate produce exactly ONE final answer
  and one gate_completion record, with coherent results — the
  store's atomic insert makes the loser converge; the detector's
  conditional flip makes a racing recoverer lose cleanly and
  answer per the fresh state;
* the verdict's claim is the retry authority (a stale gate_failure
  log entry cannot outrank it); an interrupted WAIVED retry
  preserves the original grant record; a repeated interruption
  recording is deduped to one observation.
"""

import sqlite3
import threading
import unittest
from unittest import mock

from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.state import Store
from hybrid_critic_loop.worktrees import WorktreeManager

from tests.test_fix_round4_loop import RealGitBase
from tests.test_fix_round5_loop import _final_answer_count, _observations


def _gate_failures(store, cid):
    return [o for o in _observations(store, cid)
            if o.get("kind") == "gate_failure"]


def _completions(store, cid):
    return [o for o in _observations(store, cid)
            if o.get("kind") == "gate_completion"]


def _plant_answer(store, cid, commit, human_gate):
    """A raw final_answers row, written straight through the store
    primitive — the round-7 attack F1 construction."""
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


class TestPlantedAnswerRefused(RealGitBase):
    """Spec K1 (round-7 attack F1, security F3): convergence must
    validate the answer it converges on. A planted row is one raw
    INSERT away; it must complete nothing."""

    def test_mismatched_decision_plant_completes_nothing(self):
        result = self._run_to_gate("ctr_plant1")
        winner = self.store.get_candidate(result["winner"])
        # The commit is REAL — only the recorded decision is wrong,
        # so the decision check is the one that must fire.
        _plant_answer(self.store, "ctr_plant1",
                      f"git:{winner['payload_hash']}", "rejected")

        refused = loop_mod.record_human_gate(
            self.store, "ctr_plant1", "approved")
        self.assertEqual(refused["status"], "error", refused)
        self.assertIn("does not check out", refused["detail"])
        self.assertIn("'rejected'", refused["detail"])
        self.assertNotIn("certificate", refused)
        # Nothing was written by the refusal: no failure state, no
        # completion record, no status change, no second answer.
        # (The verdict carries the claim — the operator's own
        # presented decision, recorded as claim-first gating always
        # records it; the gate itself is not consumed.)
        self.assertEqual(_gate_failures(self.store, "ctr_plant1"), [])
        self.assertEqual(_completions(self.store, "ctr_plant1"), [])
        self.assertEqual(
            self.store.get_contract("ctr_plant1")["status"],
            "awaiting_human_gate")
        self.assertEqual(
            self.store.latest_verdict("ctr_plant1")["human_gate"],
            "approved")
        self.assertEqual(_final_answer_count(self.store, "ctr_plant1"), 1)

    def test_bogus_commit_plant_refused_and_gate_recovers(self):
        result = self._run_to_gate("ctr_plant2")
        winner = self.store.get_candidate(result["winner"])
        self.assertEqual(winner["expert_id"], "gen-b")
        _plant_answer(self.store, "ctr_plant2",
                      "git:deadbeefcafe", "approved")

        refused = loop_mod.record_human_gate(
            self.store, "ctr_plant2", "approved")
        self.assertEqual(refused["status"], "error", refused)
        self.assertIn("does not check out", refused["detail"])
        self.assertIn("not a commit", refused["detail"])
        self.assertEqual(_gate_failures(self.store, "ctr_plant2"), [])
        self.assertEqual(_completions(self.store, "ctr_plant2"), [])
        self.assertEqual(
            self.store.get_contract("ctr_plant2")["status"],
            "awaiting_human_gate")

        # The gate was not consumed: the operator removes the bogus
        # row and the same presentation completes through the REAL
        # merge (the first presentation's claim makes this the
        # interrupted-completion retry).
        _delete_answers(self.store, "ctr_plant2")
        done = loop_mod.record_human_gate(
            self.store, "ctr_plant2", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["merge_source"], "verdict_snapshot")
        self.assertEqual(
            (self.repo / "solution.txt").read_text(encoding="utf-8"),
            "solution by gen-b\n")
        self.assertEqual(_final_answer_count(self.store, "ctr_plant2"), 1)
        self.assertEqual(
            self.store.get_contract("ctr_plant2")["status"], "completed")

    def test_wellformed_plant_still_converges(self):
        result = self._run_to_gate("ctr_plant3")
        winner = self.store.get_candidate(result["winner"])
        snapshot = winner["payload_hash"]
        _plant_answer(self.store, "ctr_plant3",
                      f"git:{snapshot}", "approved")

        done = loop_mod.record_human_gate(
            self.store, "ctr_plant3", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["merge_source"], "already_completed")
        self.assertEqual(done["commit"], f"git:{snapshot}")
        self.assertIn("certificate", done)
        self.assertEqual(_final_answer_count(self.store, "ctr_plant3"), 1)
        self.assertEqual(len(_completions(self.store, "ctr_plant3")), 1)
        self.assertEqual(
            self.store.get_contract("ctr_plant3")["status"], "completed")


class TestPostAnswerDeathConverges(RealGitBase):
    """Spec K2 (round-7 security F1, gate-core F1): death after the
    final-answer commit, before the status write — the durable
    state is built by performing the completion's own writes in
    order and then stopping."""

    def test_post_answer_death_converges_on_presentation(self):
        result = self._run_to_gate("ctr_pad")
        winner = self.store.get_candidate(result["winner"])
        verdict = self.store.latest_verdict("ctr_pad")
        # The completion's durable writes, in order: the claim, then
        # the final answer with the winner's snapshot commit. Death
        # lands here — the status write never happens.
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))
        contract = self.store.get_contract("ctr_pad")
        self.store.create_final_answer(
            target_id=contract["target_id"], contract_id="ctr_pad",
            commit=f"git:{winner['payload_hash']}",
            human_gate="approved")
        self.assertEqual(
            self.store.get_contract("ctr_pad")["status"],
            "awaiting_human_gate")
        self.assertEqual(_final_answer_count(self.store, "ctr_pad"), 1)

        # The retry binding applies unchanged over this state.
        refused = loop_mod.record_human_gate(
            self.store, "ctr_pad", "rejected")
        self.assertEqual(refused["status"], "error", refused)
        self.assertIn("recorded decision is 'approved'",
                      refused["detail"])
        self.assertEqual(_gate_failures(self.store, "ctr_pad"), [])
        self.assertEqual(_final_answer_count(self.store, "ctr_pad"), 1)

        done = loop_mod.record_human_gate(
            self.store, "ctr_pad", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertIn("certificate", done)
        self.assertEqual(
            self.store.get_contract("ctr_pad")["status"], "completed")
        self.assertEqual(_final_answer_count(self.store, "ctr_pad"), 1)
        self.assertEqual(len(_completions(self.store, "ctr_pad")), 1)
        failures = _gate_failures(self.store, "ctr_pad")
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("after the final answer", failures[0]["reason"])
        # Terminal cleanup spent the winner's snapshot ref.
        mgr = WorktreeManager(self.repo)
        self.assertIsNone(
            mgr.snapshot_ref_commit("ctr_pad", winner["id"]))


class _FlipLosesStore:
    """Delegates to a real store; its conditional flip simulates a
    concurrent presenter winning the recovery race: ``on_flip``
    runs (it moves the real book), and the flip reports lost."""

    def __init__(self, real, on_flip):
        self._real = real
        self._on_flip = on_flip

    def __getattr__(self, name):
        return getattr(self._real, name)

    def set_human_gate_if(self, verdict_id, expected, value):
        self._on_flip()
        return False


class TestConcurrentPresenters(RealGitBase):
    """Spec K3 (round-7 gate-core F2, security F2): recovery under
    concurrency — one answer, one completion record, coherent
    results, and no fabricated failure from the presenter whose
    conditional flip loses."""

    def test_two_presenters_one_answer(self):
        self._run_to_gate("ctr_race")
        store_b = Store(self.data_dir / "data.db")
        self.addCleanup(store_b.close)
        entered = threading.Event()
        release = threading.Event()
        original = WorktreeManager.merge_commit_into_main
        calls = {"n": 0}

        def slowed(mgr, commit):
            calls["n"] += 1
            if calls["n"] == 1:
                entered.set()
                release.wait(timeout=120)
            return original(mgr, commit)

        results = {}

        def present(key, store):
            results[key] = loop_mod.record_human_gate(
                store, "ctr_race", "approved")

        with mock.patch.object(
                WorktreeManager, "merge_commit_into_main", slowed):
            thread_a = threading.Thread(
                target=present, args=("a", self.store))
            thread_a.start()
            self.assertTrue(entered.wait(timeout=120))
            # A is claimed and blocked inside its merge. B presents
            # the same decision: the durable state is indistinguishable
            # from a crash's, so B recovers — and completes.
            thread_b = threading.Thread(
                target=present, args=("b", store_b))
            thread_b.start()
            thread_b.join(timeout=180)
            release.set()
            thread_a.join(timeout=180)

        self.assertEqual(results["b"]["status"], "completed",
                         results.get("b"))
        # A wakes to find the answer already written: the store's
        # atomic insert refuses its duplicate and A converges on
        # B's row instead of writing a second answer.
        self.assertEqual(results["a"]["status"], "completed",
                         results.get("a"))
        self.assertEqual(_final_answer_count(self.store, "ctr_race"), 1)
        self.assertEqual(len(_completions(self.store, "ctr_race")), 1)
        # Exactly one interruption record: B's, from the recovery it
        # actually performed. (Detection cannot see liveness — the
        # record is the recoverer's, and dedupe plus the conditional
        # flip cap it at one; the state itself is never corrupted.)
        self.assertEqual(len(_gate_failures(self.store, "ctr_race")), 1)
        self.assertEqual(
            self.store.get_contract("ctr_race")["status"], "completed")
        self.assertEqual(
            (self.repo / "solution.txt").read_text(encoding="utf-8"),
            "solution by gen-b\n")

    def test_flip_loses_to_a_completed_gate_answers_completed(self):
        result = self._run_to_gate("ctr_flipwin")
        verdict = self.store.latest_verdict("ctr_flipwin")
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))

        def winner_finishes():
            # The concurrent presenter completes the gate on the
            # real store while this presentation's flip is in flight.
            loop_mod.record_human_gate(self.store, "ctr_flipwin",
                                       "approved")

        wrapped = _FlipLosesStore(self.store, winner_finishes)
        answered = loop_mod.record_human_gate(
            wrapped, "ctr_flipwin", "approved")
        self.assertEqual(answered["status"], "completed", answered)
        self.assertEqual(answered["merge_source"], "already_completed")
        self.assertIn("certificate", answered)
        # The only failure record is the winner's own interruption
        # record (the loser's dedupe saw it); the loser fabricated
        # nothing and wrote no second answer.
        self.assertEqual(len(_gate_failures(self.store, "ctr_flipwin")), 1)
        self.assertEqual(
            _final_answer_count(self.store, "ctr_flipwin"), 1)
        self.assertEqual(len(_completions(self.store, "ctr_flipwin")), 1)

    def test_flip_loses_to_a_recovery_in_flight_answers_refused(self):
        result = self._run_to_gate("ctr_fliplose")
        verdict = self.store.latest_verdict("ctr_fliplose")
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))

        def recoverer_flips():
            # Another recoverer landed the flip first: the verdict
            # now reads "failed" and its recovery owns the gate.
            self.store.set_verdict_human_gate(verdict["id"], "failed")

        wrapped = _FlipLosesStore(self.store, recoverer_flips)
        answered = loop_mod.record_human_gate(
            wrapped, "ctr_fliplose", "approved")
        self.assertEqual(answered["status"], "error", answered)
        self.assertEqual(answered["detail"],
                         "no pending human gate for this contract")
        self.assertEqual(
            _final_answer_count(self.store, "ctr_fliplose"), 0)


class TestGateRecordKeeping(RealGitBase):
    """Spec K6 (round-7 gate-core F3/F4/F5): the claim is the
    authority, grants are preserved, interruption records dedupe."""

    def test_waiver_grant_preserved_on_retry(self):
        result = self._run_to_gate("ctr_wv", measured=True)
        winner = self.store.get_candidate(result["winner"])
        verdict = self.store.latest_verdict("ctr_wv")
        # The durable state a death after the grant write leaves:
        # claim landed, grant on the verdict with the operator's
        # reason and the granting channel, nothing else.
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "waived"))
        grant = dict(verdict["waiver"])
        grant.update({
            "gate": "measured", "status": "granted", "by": "tool",
            "provenance": "tool", "candidate": winner["id"],
            "reason": "operator's original reason",
        })
        self.store.set_verdict_waiver(verdict["id"], grant)

        # The retry arrives with no reason, through another channel.
        done = loop_mod.record_human_gate(
            self.store, "ctr_wv", "waived", None, channel="cli")
        self.assertEqual(done["status"], "completed", done)
        verdict = self.store.latest_verdict("ctr_wv")
        self.assertEqual(verdict["waiver"]["status"], "granted")
        self.assertEqual(verdict["waiver"]["reason"],
                         "operator's original reason")
        self.assertEqual(verdict["waiver"]["by"], "tool")
        self.assertEqual(verdict["waiver"]["provenance"], "tool")
        self.assertEqual(done["waiver"]["reason"],
                         "operator's original reason")
        self.assertEqual(_final_answer_count(self.store, "ctr_wv"), 1)

    def test_verdict_claim_outranks_the_failure_log(self):
        self._run_to_gate("ctr_auth")
        verdict = self.store.latest_verdict("ctr_auth")
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))
        # A stale gate_failure entry naming a different decision:
        # store-level construction only — no product flow produces
        # the disagreement, but the authority order must still be
        # the claim's, never the log's.
        self.store.append_observation("ctr_auth", {
            "kind": "gate_failure", "decision": "rejected",
            "reason": "stale log entry (construction)",
            "channel": "cli",
        })

        refused = loop_mod.record_human_gate(
            self.store, "ctr_auth", "rejected")
        self.assertEqual(refused["status"], "error", refused)
        self.assertIn("recorded decision is 'approved'",
                      refused["detail"])
        self.assertIsNone(self.store.get_contract("ctr_auth_rej1"))

        done = loop_mod.record_human_gate(
            self.store, "ctr_auth", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(_final_answer_count(self.store, "ctr_auth"), 1)

    def test_interruption_recording_dedupes(self):
        self._run_to_gate("ctr_dup")
        verdict = self.store.latest_verdict("ctr_dup")
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))
        # A prior detector recorded the interruption and died before
        # its flip/status writes (round-7 gate-core F5's shape).
        self.store.append_observation("ctr_dup", {
            "kind": "gate_failure", "decision": "approved",
            "reason": loop_mod._INTERRUPTED_AFTER_CLAIM,
            "channel": "cli",
        })

        done = loop_mod.record_human_gate(
            self.store, "ctr_dup", "approved")
        self.assertEqual(done["status"], "completed", done)
        # One death, one record — the second presentation did not
        # append a duplicate before completing.
        self.assertEqual(len(_gate_failures(self.store, "ctr_dup")), 1)
        self.assertEqual(_final_answer_count(self.store, "ctr_dup"), 1)


if __name__ == "__main__":
    unittest.main()
