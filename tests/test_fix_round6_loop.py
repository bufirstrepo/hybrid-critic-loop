"""Round-6 fix pass, Unit 1 (gate recovery) regression tests — real git.

The properties this file pins (fix spec J1; round-6 gate-core F1/F3/F5,
security F1/F2; J5 tail):

* a process death between claim and completion leaves a durable
  state — verdict decided, contract awaiting, NO failure record —
  and presenting the recorded decision completes the gate from that
  state (the crash case is driven from the durable state itself:
  the claim is written directly through the store, no exception
  wrap ever runs);
* completion is idempotent: a failure landing AFTER the final
  answer is written never records "failed" — the retry converges
  and final_answers stays at exactly one row;
* a retry is bound to the recorded decision: a different decision
  is refused pre-claim, naming the recorded one (both directions);
* _already_run's answer for a gate_failed contract names the gate
  retry instead of "open a new contract id";
* the human_rejection observation carries the deciding channel.
"""

import sqlite3
import unittest
from unittest import mock

from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.state import Store

from tests.test_fix_round4_loop import RealGitBase
from tests.test_fix_round5_loop import (
    StrandingBase, _final_answer_count, _observations)


def _gate_failures(store, cid):
    return [o for o in _observations(store, cid)
            if o.get("kind") == "gate_failure"]


def _completions(store, cid):
    return [o for o in _observations(store, cid)
            if o.get("kind") == "gate_completion"]


class TestInterruptedCompletion(StrandingBase):
    """Spec J1 (round-6 gate-core F1): the durable crash state. The
    claim is committed through the store exactly as claim-first
    gating writes it, and the process simply stops — no exception,
    no failure record. Presenting the decision afterwards must
    complete the gate."""

    def test_claimed_gate_completes_when_decision_presented(self):
        self._run_code_contract("ctr_crash")
        verdict = self.store.latest_verdict("ctr_crash")
        # The durable aftermath of a mid-completion kill: claim
        # committed, nothing else written.
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))
        verdict = self.store.latest_verdict("ctr_crash")
        self.assertEqual(verdict["human_gate"], "approved", verdict)
        self.assertEqual(self.store.get_contract("ctr_crash")["status"],
                         "awaiting_human_gate")
        self.assertEqual(_final_answer_count(self.store, "ctr_crash"), 0)
        self.assertEqual(_gate_failures(self.store, "ctr_crash"), [])

        done = loop_mod.record_human_gate(self.store, "ctr_crash", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertIn("certificate", done)
        self.assertTrue(
            done["certificate"]["stamp"].startswith("MM CERTIFIED"), done)
        self.assertTrue(done["certificate"]["cert"], done)
        self.assertEqual(_final_answer_count(self.store, "ctr_crash"), 1)
        self.assertEqual(self.store.get_contract("ctr_crash")["status"],
                         "completed")
        verdict = self.store.latest_verdict("ctr_crash")
        self.assertEqual(verdict["human_gate"], "approved", verdict)
        self.assertIn(
            "change for ctr_crash",
            (self.repo / "code.txt").read_text(encoding="utf-8"))
        # The interruption is on the record, matching the exception
        # path's audit shape.
        failures = _gate_failures(self.store, "ctr_crash")
        self.assertEqual(len(failures), 1, failures)
        self.assertEqual(failures[0]["decision"], "approved")
        self.assertIn("interrupted completion", failures[0]["reason"])
        self.assertEqual(len(_completions(self.store, "ctr_crash")), 1)

    def test_interrupted_gate_refuses_a_different_decision(self):
        self._run_code_contract("ctr_crash2")
        verdict = self.store.latest_verdict("ctr_crash2")
        self.assertTrue(
            self.store.claim_human_gate(verdict["id"], "approved"))

        refused = loop_mod.record_human_gate(
            self.store, "ctr_crash2", "rejected")
        self.assertEqual(refused["status"], "error", refused)
        self.assertIn("recorded decision is 'approved'", refused["detail"])
        # The refusal consumed nothing and recorded nothing: the
        # gate is still in its interrupted state.
        verdict = self.store.latest_verdict("ctr_crash2")
        self.assertEqual(verdict["human_gate"], "approved", verdict)
        self.assertEqual(self.store.get_contract("ctr_crash2")["status"],
                         "awaiting_human_gate")
        self.assertEqual(_gate_failures(self.store, "ctr_crash2"), [])
        self.assertEqual(_final_answer_count(self.store, "ctr_crash2"), 0)

        # The recorded decision still completes it afterwards.
        done = loop_mod.record_human_gate(self.store, "ctr_crash2", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(_final_answer_count(self.store, "ctr_crash2"), 1)


class StatusBombStore(Store):
    """Raises once, only on the "completed" status write — the fault
    lands AFTER create_final_answer in the completion tail."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.armed = True

    def set_contract_status(self, contract_id, status):
        if self.armed and status == "completed":
            self.armed = False
            raise RuntimeError("status write exploded (test double)")
        return super().set_contract_status(contract_id, status)


class TestPostAnswerFailureConverges(RealGitBase):
    """Spec J1 (round-6 gate-core F3, security F1): a failure after
    the final-answer write must NOT flip the gate to "failed" — the
    completion's durable record exists, so the gate converges to
    completed and the retry never writes a second final answer."""

    def test_post_answer_failure_converges_without_second_answer(self):
        self._run_to_gate("ctr_tail")
        self.store.close()
        self.store = StatusBombStore(self.data_dir / "data.db")

        first = loop_mod.record_human_gate(self.store, "ctr_tail", "approved")
        self.assertEqual(first["status"], "completed", first)
        self.assertIn("certificate", first)
        self.assertEqual(first["merge_source"], "already_completed")
        verdict = self.store.latest_verdict("ctr_tail")
        self.assertEqual(verdict["human_gate"], "approved", verdict)
        self.assertEqual(self.store.get_contract("ctr_tail")["status"],
                         "completed")
        self.assertEqual(_gate_failures(self.store, "ctr_tail"), [])
        self.assertEqual(_final_answer_count(self.store, "ctr_tail"), 1)
        self.assertEqual(len(_completions(self.store, "ctr_tail")), 1)

        # A replay after success is refused and writes nothing more.
        replay = loop_mod.record_human_gate(self.store, "ctr_tail", "approved")
        self.assertEqual(replay["status"], "error", replay)
        self.assertEqual(_final_answer_count(self.store, "ctr_tail"), 1)


class TestRetryBindingDirections(StrandingBase):
    """Spec J1 (round-6 security F2): a retry presents the RECORDED
    decision or is refused naming it — in both directions."""

    def _fail_ctr_a(self):
        """Two contracts pending on one repo; approve B (main
        advances), then A's approval fails its merge post-claim —
        the round-5 stranding shape, now a recorded failure."""
        self._run_code_contract("ctr_a")
        self._run_code_contract("ctr_b")
        done_b = loop_mod.record_human_gate(self.store, "ctr_b", "approved")
        self.assertEqual(done_b["status"], "completed", done_b)
        failed = loop_mod.record_human_gate(self.store, "ctr_a", "approved")
        self.assertEqual(failed["status"], "error", failed)
        self.assertEqual(failed["human_gate"], "failed", failed)
        return failed

    def test_failed_approval_refuses_a_rejection_retry(self):
        self._fail_ctr_a()
        refused = loop_mod.record_human_gate(
            self.store, "ctr_a", "rejected")
        self.assertEqual(refused["status"], "error", refused)
        self.assertIn("recorded decision is 'approved'", refused["detail"])
        # Untouched: still failed, still gate_failed, no child
        # contract, no final answer.
        verdict = self.store.latest_verdict("ctr_a")
        self.assertEqual(verdict["human_gate"], "failed", verdict)
        self.assertEqual(self.store.get_contract("ctr_a")["status"],
                         "gate_failed")
        self.assertIsNone(self.store.get_contract("ctr_a_rej1"))
        self.assertEqual(_final_answer_count(self.store, "ctr_a"), 0)

    def test_failed_rejection_refuses_an_approval_retry(self):
        self._run_code_contract("ctr_r")
        # A rejection whose completion fails: the child-contract
        # creation is the rejection path's first durable write.
        original = Store.create_contract
        calls = {"n": 0}

        def flaky_create(self_, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("child create exploded (test double)")
            return original(self_, **kwargs)

        with mock.patch.object(Store, "create_contract", flaky_create):
            failed = loop_mod.record_human_gate(
                self.store, "ctr_r", "rejected")
        self.assertEqual(failed["status"], "error", failed)
        self.assertEqual(failed["human_gate"], "failed", failed)
        failures = _gate_failures(self.store, "ctr_r")
        self.assertEqual(len(failures), 1, failures)
        self.assertEqual(failures[0]["decision"], "rejected")

        refused = loop_mod.record_human_gate(self.store, "ctr_r", "approved")
        self.assertEqual(refused["status"], "error", refused)
        self.assertIn("recorded decision is 'rejected'", refused["detail"])
        verdict = self.store.latest_verdict("ctr_r")
        self.assertEqual(verdict["human_gate"], "failed", verdict)
        self.assertEqual(_final_answer_count(self.store, "ctr_r"), 0)

        # The recorded decision itself still retries cleanly.
        retried = loop_mod.record_human_gate(self.store, "ctr_r", "rejected")
        self.assertEqual(retried["status"], "rejected", retried)
        self.assertIsNotNone(self.store.get_contract("ctr_r_rej1"))


class TestAlreadyRunGateFailedHint(RealGitBase):
    """Spec J1 (round-6 gate-core F5): re-running a gate_failed
    contract answers with the gate-retry hint, not "open a new
    contract id"; a full re-run surfaces the same answer."""

    def test_already_run_names_the_gate_retry(self):
        run = loop_mod._Run(self.store, {"id": "ctr_hint"}, self.repo,
                            {}, None)
        result = run._already_run("gate_failed")
        self.assertEqual(result["status"], "already_run", result)
        self.assertIn("gate_failed", result["detail"])
        self.assertIn("present the recorded decision again",
                      result["detail"])
        self.assertIn("do not open a new contract id", result["detail"])

    def test_rerun_of_gate_failed_contract_answers_already_run(self):
        from tests.test_fix_round4_loop import _contract
        self._run_to_gate("ctr_hint2")
        self.store.set_contract_status("ctr_hint2", "gate_failed")
        again = loop_mod.run_contract(
            self.store, _contract("ctr_hint2"), self.repo, self._executors(),
            human_decider=None)
        self.assertEqual(again["status"], "already_run", again)
        self.assertIn("present the recorded decision again",
                      again["detail"])


class TestRejectionChannel(RealGitBase):
    """Spec J5 tail: the human_rejection observation carries the
    deciding channel, in parity with gate_completion."""

    def test_rejection_observation_records_the_channel(self):
        self._run_to_gate("ctr_chan")
        result = loop_mod.record_human_gate(
            self.store, "ctr_chan", "rejected", "not the direction",
            channel="tool")
        self.assertEqual(result["status"], "rejected", result)
        child = self.store.get_contract("ctr_chan_rej1")
        self.assertIsNotNone(child)
        rejections = [o for o in child.get("observations") or []
                      if o.get("kind") == "human_rejection"]
        self.assertEqual(len(rejections), 1, rejections)
        self.assertEqual(rejections[0]["channel"], "tool")
        self.assertEqual(rejections[0]["from_contract"], "ctr_chan")


if __name__ == "__main__":
    unittest.main()
