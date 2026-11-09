"""Round-9 fix pass, Unit A (loop) — spec FIX-SPEC-ROUND9-2026-11-09.

* M1 (HIGH) — ``gate_passed`` is scoped to the deciding attempt's
  evaluated candidates. The Item 1 = A wire in ``Loop._write_lesson``
  derived the passed set from ``gate_details["candidate_experts"]``,
  which spans every attempt and also registers seats that never
  generated on the deciding attempt. Two leaks resulted (round-9
  lane probes, reproduced by Muse personally): a seat that
  hard-failed the deciding attempt after generating in an earlier
  one was counted as gate-passed and promoted over the deciding
  passer, and an unavailable seat — which never faced the gate at
  all — was promoted to primary. The derivation now starts from the
  deciding attempt's candidate rows whose generation completed (the
  idiom the verdict builders use for the deciding attempt's scored
  candidates), maps them through the expert map, and subtracts the
  deciding attempt's hard/measurement failures. Judged-eliminated
  candidates stay in the set: they passed the deciding gates.

  The three route tests below are also the wire's regression pins:
  each asserts the deciding passer takes primary when the primary
  is demoted. Sever the wire (``gate_passed=None``) and the store
  falls back to the pre-ruling promotion default — the compete_with
  seat is promoted unchecked — so all three tests fail.

* M2 (HIGH test gap) — D2a pinned at the matrix-fallback site. The
  anchor-path pin lives in tests/test_fix_decisions_scoring.py;
  this one builds the peer-matrix shape: the matrix passes nobody
  while a measured-eliminated seat exists in the field's run — the
  exact input the pre-ruling fallback turned into a waiver offer.
  The run must escalate and the verdict must carry ``waiver=None``.
  (Tests only: building the shape confirmed no offer is reachable.)

All runs are real-git end-to-end runs in the RealGitBase style.
"""

import json
import re
import unittest
from pathlib import Path

from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.contracts import validate_contract
from hybrid_critic_loop.executors import Executor, GenResult

from tests.test_fix_round4_loop import RealGitBase


# ---------------------------------------------------------------------------
# Scripted seats
# ---------------------------------------------------------------------------

class ScriptGen(Executor):
    """Per-call script: 'pass' writes solution.txt, 'fail' writes
    nothing (the hard gate fails), 'error' returns a non-completed
    generation. The script clamps at its last entry."""

    def __init__(self, expert_id, family, script):
        self.expert_id = expert_id
        self.family = family
        self.script = script
        self.calls = 0

    def available(self):
        return (True, "scripted seat")

    def generate(self, worktree, contract, context):
        mode = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if mode == "pass":
            Path(worktree, "solution.txt").write_text(
                f"solution by {self.expert_id}\n", encoding="utf-8")
        return GenResult(
            status="completed" if mode != "error" else "error",
            log=f"gen {self.expert_id} call {self.calls}: {mode}",
            tokens_in=100, tokens_out=50, cost_usd=None, request_ids=[],
            duration_ms=5, detail=f"mode={mode}")


class Down(Executor):
    """A seat that reports unavailable: it never generates and never
    faces a gate."""

    def __init__(self, expert_id, family):
        self.expert_id = expert_id
        self.family = family

    def available(self):
        return (False, "seat down for maintenance")

    def generate(self, worktree, contract, context):  # never called
        raise AssertionError("unavailable executor was asked to generate")


class JudgeGen(Executor):
    """Generates solution.txt plus an ops.json the measured sensor
    reads back, and judges every candidate in the packet at a flat
    low score — the matrix passes nobody."""

    def __init__(self, expert_id, family, ops):
        self.expert_id = expert_id
        self.family = family
        self.ops = ops

    def available(self):
        return (True, "scripted seat")

    def generate(self, worktree, contract, context):
        Path(worktree, "solution.txt").write_text(
            f"solution by {self.expert_id}\n", encoding="utf-8")
        Path(worktree, "ops.json").write_text(
            json.dumps({"ops": self.ops}), encoding="utf-8")
        return GenResult(status="completed", log="gen", tokens_in=100,
                         tokens_out=50, cost_usd=None, request_ids=[],
                         duration_ms=5, detail="ok")

    def judge(self, packet, rubric):
        result = {}
        sections = re.split(r"=== CANDIDATE ([A-Za-z]+) ===", packet or "")
        for i in range(1, len(sections) - 1, 2):
            label = sections[i]
            result[label] = {"items": {
                row: {"score": 1.0, "confidence": 0.9,
                      "evidence": "packet section",
                      "rationale": "scripted low"}
                for row in rubric["items"]}}
        return result


def _hard_contract(cid):
    return validate_contract({
        "id": cid, "stage": "implement", "task": "Write solution.txt.",
        "domain": "code.game", "tags": ["fix-round9"],
        "acceptance": [
            {"class": "hard", "cmd": "test -f solution.txt",
             "expect": {"exit": 0}},
        ],
    })


def _route_order(route):
    return ([route["primary"], route["compete_with"]]
            + list(route["fallback"]))


def _route_changes(store, cid):
    rows = store._conn.execute(
        "SELECT route_change_json FROM lessons WHERE contract_id=?",
        (cid,)).fetchall()
    return [json.loads(r[0]) for r in rows if r[0]]


# ---------------------------------------------------------------------------
# M1 — gate_passed scoped to the deciding attempt
# ---------------------------------------------------------------------------

class TestGatePassedScopedToDecidingAttempt(RealGitBase):
    """Route: primary exp-y, compete exp-x, fallback [exp-z]. exp-y
    hard-fails (the lesson's demote target, first failure in route
    order); exp-z is the deciding passer. Under the pre-fix
    derivation exp-x leaked into the passed set and was promoted."""

    def _seed_xyz(self):
        for eid, fam in (("exp-x", "fam-x"), ("exp-y", "fam-y"),
                         ("exp-z", "fam-z")):
            self.store.upsert_expert(eid, fam, "scripted",
                                     cost_tier="free")
        self.store.upsert_route("code", "exp-y", "exp-x", ["exp-z"],
                                [], "seed")

    def test_two_attempt_failed_seat_not_promoted(self):
        # Attempt 1: the whole field hard-fails -> retry. Attempt 2:
        # exp-x hard-fails AGAIN, exp-y hard-fails, exp-z passes and
        # wins. exp-x generated on attempt 1, so the stale expert map
        # counted it passed; scoped to the deciding attempt it is a
        # failure and must not be promoted over exp-z.
        self._seed_xyz()
        executors = {
            "exp-y": ScriptGen("exp-y", "fam-y", ["fail", "fail"]),
            "exp-x": ScriptGen("exp-x", "fam-x", ["fail", "fail"]),
            "exp-z": ScriptGen("exp-z", "fam-z", ["fail", "pass"]),
        }
        result = loop_mod.run_contract(
            self.store, _hard_contract("ctr_r9_two_attempt"), self.repo,
            executors, human_decider=lambda *a, **k: "approved")
        self.assertEqual(result["status"], "completed", result)
        # The two-attempt shape really happened.
        attempts = {c["attempt"] for c in
                    self.store.candidates_for("ctr_r9_two_attempt")}
        self.assertEqual(attempts, {1, 2})
        changes = _route_changes(self.store, "ctr_r9_two_attempt")
        self.assertEqual([c["demote"] for c in changes], ["exp-y"])
        route = self.store.get_route("code")
        order = _route_order(route)
        self.assertEqual(route["primary"], "exp-z", route)
        self.assertNotEqual(route["primary"], "exp-x", route)
        self.assertLess(order.index("exp-z"), order.index("exp-x"))

    def test_unavailable_seat_not_promoted(self):
        # Single attempt. exp-b generates and hard-fails (demote
        # target); exp-c is unavailable — a candidate row is created
        # for it but it never generates and never faces the gate;
        # exp-a generates and passes. Only exp-a may be promoted.
        self.store.upsert_route("code", "exp-b", "exp-c", ["exp-a"],
                                [], "seed")
        executors = {
            "exp-b": ScriptGen("exp-b", "fam-b", ["fail"]),
            "exp-c": Down("exp-c", "fam-c"),
            "exp-a": ScriptGen("exp-a", "fam-a", ["pass"]),
        }
        result = loop_mod.run_contract(
            self.store, _hard_contract("ctr_r9_down"), self.repo,
            executors, human_decider=None)
        self.assertEqual(result["status"], "awaiting_human_gate", result)
        # Shape proof: exp-c's row exists but its generation never
        # completed, so it was never gate-evaluated.
        down_rows = [c for c in self.store.candidates_for("ctr_r9_down")
                     if c["expert_id"] == "exp-c"]
        self.assertTrue(down_rows)
        self.assertTrue(all(c["status"] != "completed"
                            for c in down_rows))
        changes = _route_changes(self.store, "ctr_r9_down")
        self.assertEqual([c["demote"] for c in changes], ["exp-b"])
        route = self.store.get_route("code")
        self.assertEqual(route["primary"], "exp-a", route)
        self.assertNotEqual(route["primary"], "exp-c", route)

    def test_single_attempt_control(self):
        # The pre-fix derivation was already correct for a single
        # attempt (the expert map holds only this attempt's ids);
        # the scoped derivation must not change that outcome.
        self._seed_xyz()
        executors = {
            "exp-y": ScriptGen("exp-y", "fam-y", ["fail"]),
            "exp-x": ScriptGen("exp-x", "fam-x", ["fail"]),
            "exp-z": ScriptGen("exp-z", "fam-z", ["pass"]),
        }
        result = loop_mod.run_contract(
            self.store, _hard_contract("ctr_r9_control"), self.repo,
            executors, human_decider=lambda *a, **k: "approved")
        self.assertEqual(result["status"], "completed", result)
        changes = _route_changes(self.store, "ctr_r9_control")
        self.assertEqual([c["demote"] for c in changes], ["exp-y"])
        route = self.store.get_route("code")
        self.assertEqual(route["primary"], "exp-z", route)
        self.assertEqual(_route_order(route),
                         ["exp-z", "exp-y", "exp-x"])


# ---------------------------------------------------------------------------
# M2 — D2a at the matrix-fallback site
# ---------------------------------------------------------------------------

class TestMatrixFallbackOffersNoWaiver(RealGitBase):
    """A validity-matrix contract where the matrix passes nobody,
    with a measured-eliminated seat in the run: mx-c passes the hard
    gate and fails ONLY the measured gate — the candidate the
    pre-ruling matrix fallback offered to the human gate as a
    waiver. Owner ruling 2026-11-09 (Item 2(a) = B): no offer; the
    run escalates and the verdict carries no waiver."""

    def test_matrix_passes_nobody_measured_seat_present_no_waiver(self):
        self.store.upsert_route("code", "mx-a", "mx-b", ["mx-c"], [],
                                "seed")
        contract = validate_contract({
            "id": "ctr_r9_matrix_nowinner", "stage": "implement",
            "task": "Write solution.txt.", "domain": "code.game",
            "tags": ["fix-round9"],
            "acceptance": [
                {"class": "hard", "cmd": "test -f solution.txt",
                 "expect": {"exit": 0}},
                {"class": "measured",
                 "metrics": [{"name": "ops", "op": "<=", "value": 8}],
                 "sensor": "ops_bench", "sensor_cmd": "cat ops.json"},
                {"class": "judged", "rubric": "validity_matrix_v1",
                 "min": 6, "judges": 3},
            ],
        })
        executors = {
            "mx-a": JudgeGen("mx-a", "fam-a", ops=3),
            "mx-b": JudgeGen("mx-b", "fam-b", ops=3),
            "mx-c": JudgeGen("mx-c", "fam-c", ops=99),
        }
        result = loop_mod.run_contract(
            self.store, contract, self.repo, executors,
            human_decider=None)
        self.assertEqual(result["status"], "escalated", result)
        self.assertEqual(result["detail"],
                         "peer matrix passed no candidate")
        # Shape proof: the measured-eliminated seat exists (the old
        # fallback's trigger), and both matrix field candidates were
        # eliminated by the matrix itself.
        kinds = {}
        for c in self.store.candidates_for("ctr_r9_matrix_nowinner"):
            kind = (c.get("evidence") or {}).get("eliminated_kind")
            if kind is not None:
                kinds.setdefault(kind, []).append(c["expert_id"])
        self.assertEqual(kinds.get("measured"), ["mx-c"], kinds)
        self.assertEqual(sorted(kinds.get("judged", [])),
                         ["mx-a", "mx-b"], kinds)
        verdict = self.store.latest_verdict("ctr_r9_matrix_nowinner")
        self.assertTrue(verdict["escalated"], verdict)
        self.assertIsNone(verdict["winner"], verdict)
        self.assertIsNone(verdict["waiver"], verdict)
        self.assertEqual(verdict["measured"], "fail")
        self.assertEqual(verdict["human_gate"], "not_reached")
        self.assertEqual(verdict["escalation_reason"],
                         "peer matrix passed no candidate")
        self.assertEqual(
            self.store.get_contract("ctr_r9_matrix_nowinner")["status"],
            "escalated")
        # And there is nothing at the gate to waive afterwards.
        waived = loop_mod.record_human_gate(
            self.store, "ctr_r9_matrix_nowinner", "waived",
            "accept the regression")
        self.assertEqual(waived["status"], "error", waived)
        self.assertIn("no pending human gate", waived["detail"])


if __name__ == "__main__":
    unittest.main()
