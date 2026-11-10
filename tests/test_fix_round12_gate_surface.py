"""Round-12 fix pass, Unit F — gate-record surface hardening.

Spec: FIX-SPEC-ROUND12-2026-11-09.md §Unit F. Findings closed:

* attack F1 — the Item 1 = A derivation reads elimination kinds
  from candidate-row evidence, and ``update_candidate`` accepted
  ``evidence=`` writes with no protection: one public call could
  CLEAR a ``gate_error`` kind (reviving a gate-killed seat to
  primary over the sole passer) or FORGE ``kind="hard"`` onto the
  sole passer (leaving the measured-failing demotee primary). The
  elimination record is now loop-only: ``update_candidate``
  refuses any evidence write that sets or clears the key, and the
  loop's marking / winner-clearing paths go through the store's
  dedicated channel, ``set_candidate_eliminated_kind`` (the
  ``set_candidate_payload_hash`` pattern). Fixtures elsewhere that
  set kinds through ``update_candidate`` were re-based to that
  production channel (round-6 matrix / decisions scoring helpers,
  the round-12 Unit T direct drive).
* attack F2 + store F1 (convergent) — the observation FIFO
  exemption keyed on the bare ``gate_completion`` kind alone:
  planted records of that kind grew the log without bound, and
  genuine ``gate_failure`` / ``gate_answer_refused`` records stayed
  evictable — evicting a gate_failure flipped
  ``_recorded_gate_decision`` approved -> None and silently
  defeated the retry refusal. The exemption now covers the whole
  gate-record family AND is capped at the same 50 as the log:
  beyond the cap the oldest gate record trims (genuine records are
  written once per gate by construction; a >50 gate-record log is
  a plant by definition, per the spec — so the 60-plant pin below
  asserts the CAP, not the survival of a record the plants are
  indistinguishable from).
* attack F3 — crash-window convergence deduped ``gate_completion``
  on kind alone, so one stray record (wrong commit) suppressed
  the genuine merge record and left a genuinely merged book
  permanently uncertifiable, silently. The dedupe is now on
  (kind, commit, decision): only a record of THIS merge
  suppresses the write.
"""

import unittest

from hybrid_critic_loop import chain
from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.state import Store

from tests.test_fix_round4_loop import RealGitBase
from tests.test_fix_round11_chain import _CrashShapeBase
from tests.test_fix_round11_loop import _gate_contract


def _fresh_store(test, cid="ctr_f"):
    store = Store(":memory:")
    test.addCleanup(store.close)
    tid = store.create_target("t", "code")
    store.create_contract(tid, "solve", "task", "code", [], [],
                          contract_id=cid)
    return store


def _candidate(store, cid, slot="a", expert="exp-a"):
    return store.create_candidate(
        cid, 1, "implement", expert, "fam-a", slot,
        f"wt://{expert}", "basehash")


def _kind(store, candidate_id):
    row = store.get_candidate(candidate_id) or {}
    evidence = row.get("evidence")
    return evidence.get("eliminated_kind") if isinstance(
        evidence, dict) else None


def _observations(store, cid="ctr_f"):
    return store.get_contract(cid)["observations"]


# ---------------------------------------------------------------------------
# F1 — the evidence write path
# ---------------------------------------------------------------------------

class TestEvidenceWritePathRefused(unittest.TestCase):
    """Both attack directions refused at the public API."""

    def setUp(self):
        self.store = _fresh_store(self)

    def test_forge_kind_on_clean_row_refused(self):
        cand = _candidate(self.store, "ctr_f")
        with self.assertRaises(ValueError):
            self.store.update_candidate(
                cand, evidence={"eliminated_kind": "hard"})
        self.assertIsNone(_kind(self.store, cand))

    def test_forge_kind_bundled_with_other_evidence_refused(self):
        cand = _candidate(self.store, "ctr_f")
        with self.assertRaises(ValueError):
            self.store.update_candidate(
                cand, evidence={"log_hash": "x",
                                "eliminated_kind": "gate_error"})
        row = self.store.get_candidate(cand)
        self.assertEqual(row["evidence"], {})

    def test_clear_gate_error_kind_refused(self):
        cand = _candidate(self.store, "ctr_f")
        self.store.set_candidate_eliminated_kind(cand, "gate_error")
        for clearing in ({}, {"log_hash": "x"}, None):
            with self.assertRaises(ValueError):
                self.store.update_candidate(cand, evidence=clearing)
            self.assertEqual(_kind(self.store, cand), "gate_error")

    def test_change_kind_refused(self):
        cand = _candidate(self.store, "ctr_f")
        self.store.set_candidate_eliminated_kind(cand, "gate_error")
        with self.assertRaises(ValueError):
            self.store.update_candidate(
                cand, evidence={"eliminated_kind": "hard"})
        self.assertEqual(_kind(self.store, cand), "gate_error")

    def test_evidence_write_without_kind_on_clean_row_still_works(self):
        # The dispatch path's own write shape: no kind on either
        # side — unaffected by the guard.
        cand = _candidate(self.store, "ctr_f")
        self.store.update_candidate(
            cand, evidence={"log_hash": "abc", "detail": "ran"},
            status="completed")
        row = self.store.get_candidate(cand)
        self.assertEqual(row["evidence"],
                         {"log_hash": "abc", "detail": "ran"})
        self.assertEqual(row["status"], "completed")

    def test_loop_channel_sets_preserves_and_clears(self):
        cand = _candidate(self.store, "ctr_f")
        self.store.update_candidate(cand, evidence={"log_hash": "abc"})
        self.store.set_candidate_eliminated_kind(cand, "measured")
        row = self.store.get_candidate(cand)
        self.assertEqual(row["evidence"],
                         {"log_hash": "abc",
                          "eliminated_kind": "measured"})
        self.store.set_candidate_eliminated_kind(cand, None)
        row = self.store.get_candidate(cand)
        self.assertEqual(row["evidence"], {"log_hash": "abc"})

    def test_mark_eliminated_lands_through_the_channel(self):
        # loop._mark_eliminated is best-effort (it swallows store
        # errors): if it still wrote through update_candidate, the
        # refusal would be swallowed and NO kind would land. The
        # kind landing proves the loop routes through the channel.
        cand = _candidate(self.store, "ctr_f")
        run = object.__new__(loop_mod._Run)
        run.store = self.store
        run._mark_eliminated({"id": cand}, "gate_error")
        self.assertEqual(_kind(self.store, cand), "gate_error")


class TestEvidenceForgeRouteOutcome(RealGitBase):
    """The lane probe's exact shapes (probeA_forge A1/A2), driven
    against the real derivation in _write_lesson: setup through
    the loop's channel, the ATTACK through the public API."""

    def _build(self, cid):
        contract = _gate_contract(cid)
        self.store.upsert_route("code", "exp-p", "exp-x", ["exp-z"],
                                [], "seed")
        cids = {}
        for slot, eid in (("a", "exp-p"), ("b", "exp-x"),
                          ("c", "exp-z")):
            cids[eid] = self.store.create_candidate(
                cid, 1, "implement", eid, f"fam-{eid[-1]}", slot,
                f"wt://{eid}", "basehash")
            self.store.update_candidate(cids[eid], status="completed")
        # exp-x is gate-killed, exactly as the loop would mark it.
        self.store.set_candidate_eliminated_kind(
            cids["exp-x"], "gate_error")
        self.store.create_verdict(
            cid, 1, cids["exp-z"], [], "pass", "fail", None, False,
            None, None, "pending", None)
        gate_details = {
            "candidate_experts": {cids[e]: e for e in cids},
            "hard_failures": [],
            "measurements": [
                {"candidate_id": cids["exp-p"], "metric": "ops",
                 "value": 99, "op": "<=", "threshold": 8,
                 "pass": False},
            ],
        }
        run = loop_mod._Run(self.store, contract, self.repo, {},
                            human_decider=None)
        return cids, run, gate_details

    def _primary(self):
        return self.store.get_route("code")["primary"]

    def test_control_gate_error_seat_not_promoted(self):
        _cids, run, gd = self._build("ctr_f_ctl")
        run._write_lesson(1, gd)
        self.assertEqual(self._primary(), "exp-z")

    def test_revival_attack_refused_and_seat_stays_dead(self):
        cids, run, gd = self._build("ctr_f_rev")
        with self.assertRaises(ValueError):
            self.store.update_candidate(cids["exp-x"], evidence={})
        run._write_lesson(1, gd)
        # NOT exp-x: the gate-killed seat is not revived over the
        # sole passer.
        self.assertEqual(self._primary(), "exp-z")

    def test_forge_attack_refused_and_passer_still_promoted(self):
        cids, run, gd = self._build("ctr_f_exc")
        with self.assertRaises(ValueError):
            self.store.update_candidate(
                cids["exp-z"], evidence={"eliminated_kind": "hard"})
        run._write_lesson(1, gd)
        # NOT exp-p: the measured-failing demotee does not keep
        # primary for want of a passer.
        self.assertEqual(self._primary(), "exp-z")


# ---------------------------------------------------------------------------
# F2 / store F1 — the FIFO exemption, bound on both sides
# ---------------------------------------------------------------------------

class TestGateRecordFamilyExemption(unittest.TestCase):

    def setUp(self):
        self.store = _fresh_store(self)

    def test_gate_failure_survives_fifty_noise_appends(self):
        # The lane's exact shape (p1_fifo #5): the recorded gate
        # decision survives the flood.
        self.store.append_observation("ctr_f", {
            "kind": "gate_failure", "decision": "approved",
            "reason": "boom", "channel": "cli"})
        verdict = {"id": "vrd_1", "human_gate": "failed"}
        self.assertEqual(
            loop_mod._recorded_gate_decision(
                self.store, "ctr_f", verdict), "approved")
        for i in range(50):
            self.store.append_observation(
                "ctr_f", {"kind": "noise", "n": i})
        self.assertEqual(
            loop_mod._recorded_gate_decision(
                self.store, "ctr_f", verdict), "approved")
        self.assertTrue(any(
            isinstance(o, dict) and o.get("kind") == "gate_failure"
            for o in _observations(self.store)))

    def test_gate_answer_refused_survives_fifty_noise_appends(self):
        # p1_fifo #6.
        loop_mod._append_answer_refusal_once(
            self.store, "ctr_f", "vrd_1", "approved", "bad row")
        self.assertTrue(loop_mod._answer_refusal_on_record(
            self.store, "ctr_f", "vrd_1", "approved"))
        for i in range(50):
            self.store.append_observation(
                "ctr_f", {"kind": "noise", "n": i})
        self.assertTrue(loop_mod._answer_refusal_on_record(
            self.store, "ctr_f", "vrd_1", "approved"))

    def test_gate_failure_dedupe_holds_after_the_flood(self):
        # p1_fifo #7: no eviction, so the deduped second write
        # still sees the first record — one record, not two.
        loop_mod._append_gate_failure_once(
            self.store, "ctr_f", "approved", "cli", "boom")
        for i in range(50):
            self.store.append_observation(
                "ctr_f", {"kind": "noise", "n": i})
        loop_mod._append_gate_failure_once(
            self.store, "ctr_f", "approved", "cli", "boom")
        failures = [o for o in _observations(self.store)
                    if isinstance(o, dict)
                    and o.get("kind") == "gate_failure"]
        self.assertEqual(len(failures), 1)

    def test_sixty_planted_gate_records_do_not_defeat_the_cap(self):
        # probeB_fifo (1): 60 plants, then 240 more — the log
        # stays at the cap either way.
        for i in range(60):
            self.store.append_observation("ctr_f", {
                "kind": "gate_completion", "commit": "junk", "n": i})
        self.assertEqual(len(_observations(self.store)), 50)
        for i in range(240):
            self.store.append_observation("ctr_f", {
                "kind": "gate_completion", "commit": "j" * 40,
                "n": i})
        self.assertEqual(len(_observations(self.store)), 50)

    def test_planted_flood_does_not_freeze_out_a_gate_failure(self):
        # probeB_fifo (2): with >=50 gate records occupying the
        # book, a genuine gate_failure append LANDS (the oldest
        # planted record trims) instead of silently self-evicting.
        for i in range(60):
            self.store.append_observation("ctr_f", {
                "kind": "gate_completion", "commit": "junk", "n": i})
        self.store.append_observation("ctr_f", {
            "kind": "gate_failure", "decision": "approved",
            "reason": "probe", "channel": "probe"})
        obs = _observations(self.store)
        self.assertEqual(len(obs), 50)
        self.assertTrue(any(
            isinstance(o, dict) and o.get("kind") == "gate_failure"
            for o in obs))

    def test_interleave_gate_record_kept_under_noise_pressure(self):
        # p1_fifo #1: 25 noise, 1 gate_completion, 30 noise.
        for i in range(25):
            self.store.append_observation(
                "ctr_f", {"kind": "noise", "n": i})
        self.store.append_observation("ctr_f", {
            "kind": "gate_completion", "decision": "approved",
            "commit": "a" * 40})
        for i in range(30):
            self.store.append_observation(
                "ctr_f", {"kind": "noise", "n": 100 + i})
        obs = _observations(self.store)
        self.assertEqual(len(obs), 50)
        self.assertTrue(any(
            isinstance(o, dict)
            and o.get("kind") == "gate_completion" for o in obs))
        self.assertEqual(
            sum(1 for o in obs
                if isinstance(o, dict) and o.get("kind") == "noise"),
            49)


# ---------------------------------------------------------------------------
# F3 — convergence dedupe on content
# ---------------------------------------------------------------------------

class TestConvergenceDedupeOnContent(_CrashShapeBase):

    def _gate_records(self, cid):
        return [o for o in self.store.get_contract(cid)["observations"]
                if isinstance(o, dict)
                and o.get("kind") == "gate_completion"]

    def test_stray_record_does_not_suppress_the_genuine_merge(self):
        # probeB_converge's exact shape: stray first (right kind,
        # right decision, WRONG commit), then convergence.
        _result, _winner, merged = self._crash_shape("ctr_f3")
        cid = "ctr_f3"
        self.store.append_observation(cid, {
            "kind": "gate_completion", "decision": "approved",
            "commit": "0" * 40, "channel": "stray"})
        done = loop_mod.record_human_gate(self.store, cid, "approved")
        self.assertEqual(done.get("status"), "completed", done)
        records = self._gate_records(cid)
        genuine = [o for o in records if o.get("commit") == merged]
        self.assertEqual(len(genuine), 1, records)
        self.assertEqual(genuine[0].get("merge_source"),
                         "already_completed")
        # The book completes, and its certificate issues + verifies.
        self.assertTrue(chain._contract_completed(self.store, cid))
        cert = chain.issue_certificate(self.store, cid)
        self.assertIsNotNone(cert)
        self.assertTrue(
            chain.verify_certificate(self.store, cert["cert"])[0])

    def test_same_content_record_still_dedupes(self):
        # Control: a prior record of THIS merge (same commit, same
        # decision) still suppresses a second write — a converged
        # book carries exactly one genuine record.
        _result, _winner, merged = self._crash_shape("ctr_f3b")
        cid = "ctr_f3b"
        self.store.append_observation(cid, {
            "kind": "gate_completion", "decision": "approved",
            "commit": merged, "channel": "pin"})
        done = loop_mod.record_human_gate(self.store, cid, "approved")
        self.assertEqual(done.get("status"), "completed", done)
        records = [o for o in self._gate_records(cid)
                   if o.get("commit") == merged]
        self.assertEqual(len(records), 1, records)

    def test_different_decision_record_does_not_suppress(self):
        # The decision half of the (kind, commit, decision) key:
        # same commit, different decision is not this merge.
        _result, _winner, merged = self._crash_shape("ctr_f3c")
        cid = "ctr_f3c"
        self.store.append_observation(cid, {
            "kind": "gate_completion", "decision": "waived",
            "commit": merged, "channel": "stray"})
        done = loop_mod.record_human_gate(self.store, cid, "approved")
        self.assertEqual(done.get("status"), "completed", done)
        genuine = [o for o in self._gate_records(cid)
                   if o.get("commit") == merged
                   and o.get("decision") == "approved"]
        self.assertEqual(len(genuine), 1, self._gate_records(cid))


if __name__ == "__main__":
    unittest.main()
