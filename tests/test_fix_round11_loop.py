"""Round-11 fix pass, Unit A (loop) — spec FIX-SPEC-ROUND11-2026-11-09.

* HIGH-1 (gatecore F1) — the ``gate_passed`` derivation in
  ``Loop._write_lesson`` subtracted only hard-failure rows and
  failing measurement rows, so two elimination classes that produce
  neither — ``gate_error`` and provenance — counted as gate-PASSING
  and a seat killed by either was promoted to primary over the sole
  real passer. The derivation now also subtracts every deciding
  candidate whose own row carries a gate elimination kind
  (``_GATE_FAILED_KINDS``, derived from ``_ELIMINATED_KINDS``), which
  is also the record the dissent re-run refresh path writes, so a
  re-run elimination counts with no second code path. Judged and
  bracket-cut candidates stay in the set: they passed the gates.

* HIGH-2 (testaudit F2) — the measurements half of the subtraction
  was unpinned (mutation M1a survived round 10): deleting it left
  every test green. The direct-drive pin below fails when ONLY that
  half is removed, because the candidates it drives carry no
  elimination kind on their rows for the rows half to rescue.

* Raw-shape measurements (store F2 / Unit A item 2) — the old
  predicate ``not m.get("pass", True)`` never fired for raw gate
  rows, which carry ``passed`` and no ``pass`` key. The shared
  predicate ``_measurement_failed`` mirrors
  ``lessons.derive_outcome_cause``: a row fails when ``pass`` is
  False OR (``pass`` absent AND ``passed`` is False).
"""

import json
import unittest
from pathlib import Path

from hybrid_critic_loop import gates as gates_mod
from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.contracts import validate_contract
from hybrid_critic_loop.executors import Executor, GenResult

from tests.test_fix_round4_loop import RealGitBase
from tests.test_loop_v03 import FakeGen, FakeJudge, LoopV03Base, _contract


# ---------------------------------------------------------------------------
# Predicate pins
# ---------------------------------------------------------------------------

class TestMeasurementFailedPredicate(unittest.TestCase):
    def test_both_shapes_one_predicate(self):
        f = loop_mod._measurement_failed
        # Store shape: "pass" key.
        self.assertTrue(f({"pass": False}))
        self.assertFalse(f({"pass": True}))
        # Raw gate shape: "passed" only, no "pass" key.
        self.assertTrue(f({"passed": False}))
        self.assertFalse(f({"passed": True}))
        # "pass" present governs, exactly as in derive_outcome_cause.
        self.assertTrue(f({"pass": False, "passed": True}))
        self.assertFalse(f({"pass": True, "passed": False}))
        # Absent / malformed rows never fail.
        self.assertFalse(f({}))
        self.assertFalse(f({"value": 3}))
        self.assertFalse(f(None))
        self.assertFalse(f("pass"))

    def test_gate_failed_kinds_derive_from_eliminated_kinds(self):
        self.assertEqual(loop_mod._GATE_FAILED_KINDS,
                         frozenset(loop_mod._ELIMINATED_KINDS.values()))
        self.assertIn("gate_error", loop_mod._GATE_FAILED_KINDS)
        self.assertIn("hard", loop_mod._GATE_FAILED_KINDS)       # provenance lands here
        self.assertIn("measured", loop_mod._GATE_FAILED_KINDS)
        self.assertNotIn("judged", loop_mod._GATE_FAILED_KINDS)
        self.assertNotIn("bracket_cut", loop_mod._GATE_FAILED_KINDS)


# ---------------------------------------------------------------------------
# HIGH-1 — end-to-end: gate_error / provenance seats are never promoted
# ---------------------------------------------------------------------------

class ModeGen(Executor):
    """Real-git seat: 'measured_fail' breaches the ops metric,
    'gate_error' emits an unparseable sensor payload, 'provenance'
    generates with mock provenance, 'pass' is clean."""

    def __init__(self, expert_id, family, mode):
        self.expert_id = expert_id
        self.family = family
        self.mode = mode

    def available(self):
        return (True, "ok")

    def generate(self, worktree, contract, context):
        Path(worktree, "solution.txt").write_text(
            f"solution by {self.expert_id}\n", encoding="utf-8")
        provenance = "generated"
        if self.mode == "measured_fail":
            Path(worktree, "ops.json").write_text(
                json.dumps({"ops": 99}), encoding="utf-8")
        elif self.mode == "gate_error":
            Path(worktree, "ops.json").write_text(
                "NOT-JSON{{{", encoding="utf-8")
        elif self.mode == "provenance":
            Path(worktree, "ops.json").write_text(
                json.dumps({"ops": 3}), encoding="utf-8")
            provenance = "mock"
        else:
            Path(worktree, "ops.json").write_text(
                json.dumps({"ops": 3}), encoding="utf-8")
        return GenResult(status="completed", log="gen", tokens_in=100,
                         tokens_out=50, cost_usd=None, request_ids=[],
                         duration_ms=5, detail="ok",
                         provenance=provenance)


def _gate_contract(cid):
    return validate_contract({
        "id": cid, "stage": "implement", "task": "Write solution.txt.",
        "domain": "code.game", "tags": ["fix-round11"],
        "acceptance": [
            {"class": "hard", "cmd": "test -f solution.txt",
             "expect": {"exit": 0}},
            {"class": "measured",
             "metrics": [{"name": "ops", "op": "<=", "value": 8}],
             "sensor": "ops_bench", "sensor_cmd": "cat ops.json"},
        ],
    })


class TestGateKilledSeatNeverPromoted(RealGitBase):
    """Route: primary exp-y (measured-fails — the lesson's demote
    target), compete exp-x (gate-killed two ways), fallback [exp-z]
    (the sole passer and winner). The demotion of exp-y must promote
    exp-z, never the gate-killed exp-x."""

    def _run_case(self, mode, cid, expected_kind):
        self.store.upsert_route("code", "exp-y", "exp-x", ["exp-z"],
                                [], "seed")
        executors = {
            "exp-y": ModeGen("exp-y", "fam-y", "measured_fail"),
            "exp-x": ModeGen("exp-x", "fam-x", mode),
            "exp-z": ModeGen("exp-z", "fam-z", "pass"),
        }
        result = loop_mod.run_contract(
            self.store, _gate_contract(cid), self.repo, executors,
            human_decider=None)
        self.assertEqual(result["status"], "awaiting_human_gate", result)
        winner = self.store.get_candidate(result["winner"])
        self.assertEqual(winner["expert_id"], "exp-z", result)
        kinds = {c["expert_id"]: (c.get("evidence") or {}).get(
            "eliminated_kind")
            for c in self.store.candidates_for(cid)}
        self.assertEqual(kinds["exp-x"], expected_kind, kinds)
        self.assertEqual(kinds["exp-y"], "measured", kinds)
        changes = [json.loads(r[0]) for r in self.store._conn.execute(
            "SELECT route_change_json FROM lessons WHERE contract_id=?",
            (cid,)).fetchall() if r[0]]
        self.assertEqual([c["demote"] for c in changes], ["exp-y"])
        route = self.store.get_route("code")
        self.assertEqual(route["primary"], "exp-z", route)
        self.assertNotEqual(route["primary"], "exp-x", route)
        order = ([route["primary"], route["compete_with"]]
                 + list(route["fallback"]))
        self.assertLess(order.index("exp-z"), order.index("exp-x"))

    def test_gate_error_seat_not_promoted(self):
        self._run_case("gate_error", "ctr_r11_gate_error", "gate_error")

    def test_provenance_seat_not_promoted(self):
        # Provenance failures persist under the "hard" kind.
        self._run_case("provenance", "ctr_r11_provenance", "hard")


# ---------------------------------------------------------------------------
# HIGH-2 / raw shape — direct _write_lesson drive (the M1a mutation pin)
# ---------------------------------------------------------------------------

class TestMeasurementsHalfIsLoadBearing(RealGitBase):
    """Hand-built deciding attempt: three completed candidate rows
    with NO elimination kind on any row, a verdict, and gate_details
    whose only failure evidence is measurement rows — exp-p's in
    store shape (``pass: False``; it is the lesson's demote target,
    the first failing row) and exp-x's in raw shape (``passed:
    False``, no ``pass`` key). The complete derivation promotes the
    sole passer exp-z. Remove ONLY the measurements half of the
    subtraction and nothing else excludes exp-p or exp-x, so this
    pin fails — HIGH-2's exact mutant (M1a). Revert the predicate to
    ``not m.get("pass", True)`` and the raw row stops failing, so
    the pin fails then too."""

    def test_raw_shape_measurement_failure_excludes(self):
        cid = "ctr_r11_raw_shape"
        contract = _gate_contract(cid)
        self.store.upsert_route("code", "exp-p", "exp-x", ["exp-z"],
                                [], "seed")
        cids = {}
        for slot, eid in (("a", "exp-p"), ("b", "exp-x"), ("c", "exp-z")):
            cids[eid] = self.store.create_candidate(
                cid, 1, "implement", eid, f"fam-{eid[-1]}", slot,
                f"wt://{eid}", "basehash")
            self.store.update_candidate(cids[eid], status="completed")
        self.store.create_verdict(
            cid, 1, cids["exp-z"], [], "pass", "fail", None, False,
            None, None, "pending", None)
        gate_details = {
            "candidate_experts": {cids[e]: e for e in cids},
            "hard_failures": [],
            "measurements": [
                {"candidate_id": cids["exp-p"], "metric": "ops",
                 "value": 99, "op": "<=", "threshold": 8, "pass": False},
                {"candidate_id": cids["exp-x"], "metric": "ops",
                 "value": 99, "op": "<=", "threshold": 8,
                 "passed": False},
            ],
        }
        run = loop_mod._Run(self.store, contract, self.repo, {},
                            human_decider=None)
        lesson_id = run._write_lesson(1, gate_details)
        self.assertIsNotNone(lesson_id)
        lesson = self.store.get_lesson(lesson_id)
        self.assertEqual(lesson["route_change"]["demote"], "exp-p",
                         lesson)
        route = self.store.get_route("code")
        self.assertEqual(route["primary"], "exp-z", route)
        self.assertNotEqual(route["primary"], "exp-x", route)


# ---------------------------------------------------------------------------
# Unit A item 3 — dissent re-run elimination counts in the derivation
# ---------------------------------------------------------------------------

class TestDissentRerunGateErrorNotPromoted(LoopV03Base):
    """gen-p fails the measured gate on the first pass (the lesson's
    demote target). gen-x passes, the matrix scores it, a dissent
    cites the ops metric for gen-x, and the gate re-run eliminates
    gen-x at gate_error — no hard row, no measurement row. gen-z is
    the sole remaining passer and wins. The demotion of gen-p must
    promote gen-z: gen-x's row carries the gate_error kind the
    derivation reads, even though the re-run refresh added nothing
    to gate_details."""

    def test_rerun_gate_error_seat_not_promoted(self):
        self.markers.update({"gen-p": "m-p", "gen-x": "m-x",
                             "gen-z": "m-z"})
        base_eval = gates_mod.evaluate_candidate
        store = self.store
        counts = {}

        def second_look(store_, contract, candidate, cwd):
            eid = candidate["expert_id"]
            counts[eid] = counts.get(eid, 0) + 1
            gr = base_eval(store_, contract, candidate, cwd)
            if eid == "gen-p" and counts[eid] == 1:
                store.add_measurement(
                    candidate["id"], "ops_bench", "ops_per_tick",
                    120.0, "<=", 8, False)
                return gates_mod.CandidateGateResult(
                    candidate_id=candidate["id"], provenance_ok=True,
                    eliminated_at="measured", hard=[],
                    measured=gr.measured)
            if eid == "gen-x" and counts[eid] >= 2:
                return gates_mod.CandidateGateResult(
                    candidate_id=candidate["id"], provenance_ok=True,
                    eliminated_at="gate_error", hard=[], measured=[])
            return gr

        gates_mod.evaluate_candidate = second_look
        loop_mod.gates_mod.evaluate_candidate = second_look
        dissent_cell = {"score": 1.0, "confidence": 0.9,
                        "evidence": "ops_per_tick reading disputed",
                        "rationale": "the ops_per_tick sensor looks misread"}
        scores = {"m-p": 8.0, "m-x": 8.0, "m-z": 9.0}
        executors = {
            "gen-p": FakeGen("gen-p", "fam-p", "m-p"),
            "gen-x": FakeGen("gen-x", "fam-x", "m-x"),
            "gen-z": FakeGen("gen-z", "fam-z", "m-z"),
            "judge-a": FakeJudge("judge-a", "fam-ja", scores),
            "judge-b": FakeJudge("judge-b", "fam-jb", scores),
            "judge-c": FakeJudge(
                "judge-c", "fam-jc", scores,
                overrides={("m-x", "test_agreement"): dissent_cell},
                repeat_overrides=True),
        }
        self.panel("gen-p", "gen-x",
                   ["gen-z", "judge-a", "judge-b", "judge-c"],
                   ["gen-p", "gen-x", "gen-z"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(
            self.store.get_candidate(result["winner"])["expert_id"],
            "gen-z")
        # Shape proof: the dissent re-ran gen-x's gates exactly once
        # more, and the re-run's kind landed on gen-x's row.
        self.assertEqual(counts.get("gen-x"), 2, counts)
        kinds = {c["expert_id"]: (c.get("evidence") or {}).get(
            "eliminated_kind")
            for c in self.store.candidates_for("ctr_fake")}
        self.assertEqual(kinds["gen-p"], "measured", kinds)
        self.assertEqual(kinds["gen-x"], "gate_error", kinds)
        lesson = self.store.get_lesson(result["lesson_id"])
        self.assertEqual(lesson["route_change"]["demote"], "gen-p",
                         lesson)
        route = self.store.get_route("code")
        self.assertEqual(route["primary"], "gen-z", route)
        self.assertNotEqual(route["primary"], "gen-x", route)


if __name__ == "__main__":
    unittest.main()
