"""Round-12 fix pass, Unit T — pins only, no source change
(FIX-SPEC-ROUND12-2026-11-09 §Unit T; round-12 testaudit F1–F4).

* F1 (HIGH) — the deciding-attempt scoping at
  ``loop.py`` ``_write_lesson`` (``deciding_rows`` read with
  ``attempt=deciding_attempt``) was load-bearing and unpinned:
  dropping the ``attempt=`` filter survived the round-11 targeted
  set (92 tests) while the mutant misbehaved live in two shapes —
  an attempt-1-only passer promoted, and a pass-then-fail seat
  promoted over the sole deciding passer (the Item 1 = A
  violation shape). Both shapes are pinned below TWICE: once
  end-to-end through ``run_contract`` (a legacy judged retry
  produces the two attempts; the deciding attempt's measured
  failures produce the demotion), and once by direct
  ``_write_lesson`` drive on hand-built rows (the lane probes'
  exact shape, where the attempt-1-only seat has no deciding
  row at all — a differing-candidate-set shape ``run_contract``'s
  fixed panel cannot mint). Every pin drives the REAL derivation
  (``lessons.derive_outcome_cause`` -> ``_write_lesson``'s
  ``gate_passed`` -> ``router.apply_lesson_route_change`` ->
  ``Store.apply_route_demotion``) and asserts the resulting
  route. Acceptance is the inverse mutation form: with the
  ``attempt=`` filter removed by hand, every discriminating pin
  below FAILS (the mutant promotes gen-q / exp-q); restored, all
  pass. A control pin (attempt-1 elimination does not leak into
  the deciding attempt) passes under both — it guards against an
  over-scoped repair, and is labelled a control, not a
  discriminating pin.

* F3 (LOW) — the round-11 direct-drive M1a pin's store-shape
  half was inert: its store-shape candidate was the demotee,
  whom the demotion excludes by construction, so only the unit
  predicate test discriminated the store shape (``pass: False``)
  that production actually feeds the derivation. The sibling
  below makes a NON-demotee seat fail only through a store-shape
  row, at route level.

* F4 (LOW) — the matrix presentation shuffle's cross-process
  reproducibility was pinned only by the source-text assertion
  ``test_no_site_seeds_from_the_builtin_hash``. The pins below
  exercise ``run_matrix`` BEHAVIORALLY: the persisted
  ``position_shown`` order for every peer must equal the order
  ``judges.seeded_shuffle_rng(seed, judge_id)`` produces, in
  process and across two subprocesses under different
  PYTHONHASHSEEDs. A matrix-site revert to the builtin ``hash()``
  (the round-12 lane's ``D-shuffle-matrix-site`` mutant, helper
  untouched) changes the persisted order and fails these pins
  with no source text involved.

* F2 (LOW) — three ``TestLeg1Discipline`` plants are
  multi-confounded. Two already have single-confound siblings in
  ``tests/test_fix_round11_chain.py``, pointed to here rather
  than duplicated: the rejected pair's decision half by
  ``test_rejected_decision_on_completed_book_is_not_completed``,
  and the banana+non-sha plant's halves by
  ``test_arbitrary_decision_on_completed_book_is_not_completed``
  (banana decision, genuine sha) and
  ``test_non_sha_commit_with_answer_decision_is_not_completed``
  (non-sha commit, answer decision). The null pair had NO
  single-confound sibling — its book is unsettled, so the status
  conjunct also refuses it — and the rejected pair's status half
  was pinned only with an unsettled ("open") book, never with
  the plant's own settled "rejected" status. Both missing
  siblings are added below.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from hybrid_critic_loop import chain, gates as gates_mod, judges
from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.contracts import validate_contract
from hybrid_critic_loop.executors import GenResult
from hybrid_critic_loop.matrix import run_matrix

from tests.test_fix_round11_chain import _pending_verdict, _plant_pair, SHA
from tests.test_loop_v03 import FakeGen, FakeJudge, LoopV03Base, _contract
from tests.test_matrix import cell_entry, uniform
from tests.test_tightening_chain import TighteningTestCase

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# F1 — end-to-end through run_contract (legacy judged retry = two attempts)
# ---------------------------------------------------------------------------

def _legacy_contract():
    return validate_contract({
        "id": "ctr_fake", "stage": "implement",
        "task": "Implement the fake thing.", "domain": "code.game",
        "tags": ["fake"],
        "acceptance": [
            {"class": "hard", "cmd": "true", "expect": {"exit": 0}},
            {"class": "measured",
             "metrics": [{"name": "ops_per_tick", "op": "<=", "value": 8}],
             "sensor": "ops_bench", "sensor_cmd": "true"},
            {"class": "judged", "rubric": "code_quality_v1",
             "min": 7, "judges": 3},
        ],
    })


class _TwoPhaseJudge(FakeJudge):
    """Attempt 1 scores every candidate below the judged minimum
    (the run retries); attempt 2 scores only gen-z's marker high."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.phase = 0

    def judge(self, packet, rubric):
        self.phase += 1
        if self.phase == 1:
            self.scores = {"m-p": 3.0, "m-q": 3.0, "m-z": 3.0}
        else:
            self.scores = {"m-p": 3.0, "m-q": 3.0, "m-z": 9.0}
        return super().judge(packet, rubric)


class _SecondCallUnavailableGen(FakeGen):
    """Generates on attempt 1; on the retry its generation reports
    unavailable, so its deciding-attempt row is not completed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gen_calls = 0

    def generate(self, worktree, contract, context):
        self.gen_calls += 1
        if self.gen_calls > 1:
            return GenResult(status="unavailable", log="", duration_ms=0,
                             detail="gone on retry")
        return super().generate(worktree, contract, context)


class TestAttemptScopingEndToEnd(LoopV03Base):
    """Both testaudit F1 shapes through the real run: attempt 1
    passes hard/measured for every seat but fails the legacy
    judged gate, so the run retries; on the deciding attempt
    gen-p fails the measured gate (the lesson's demote target)
    and gen-z alone passes everything and wins. The demotion of
    gen-p must promote gen-z. Drop the ``attempt=`` filter and
    gen-q's clean attempt-1 candidate id admits it to
    ``gate_passed`` — the mutant promotes gen-q in both shapes.
    """

    def _run_shape(self, shape):
        self.markers.update({"gen-p": "m-p", "gen-q": "m-q",
                             "gen-z": "m-z"})
        base_eval = gates_mod.evaluate_candidate
        store = self.store
        counts = {}
        fail_on_retry = {"gen-p"} if shape == 1 else {"gen-p", "gen-q"}

        def second_look(store_, contract, candidate, cwd):
            eid = candidate["expert_id"]
            counts[eid] = counts.get(eid, 0) + 1
            gr = base_eval(store_, contract, candidate, cwd)
            if eid in fail_on_retry and counts[eid] >= 2:
                store.add_measurement(
                    candidate["id"], "ops_bench", "ops_per_tick",
                    120.0, "<=", 8, False)
                return gates_mod.CandidateGateResult(
                    candidate_id=candidate["id"], provenance_ok=True,
                    eliminated_at="measured", hard=[],
                    measured=gr.measured)
            return gr

        gates_mod.evaluate_candidate = second_look
        loop_mod.gates_mod.evaluate_candidate = second_look
        low = {"m-p": 3.0, "m-q": 3.0, "m-z": 3.0}
        gen_q = (_SecondCallUnavailableGen("gen-q", "fam-q", "m-q")
                 if shape == 1 else FakeGen("gen-q", "fam-q", "m-q"))
        executors = {
            "gen-p": FakeGen("gen-p", "fam-p", "m-p"),
            "gen-q": gen_q,
            "gen-z": FakeGen("gen-z", "fam-z", "m-z"),
            "judge-x": _TwoPhaseJudge("judge-x", "fam-jx", dict(low)),
            "judge-y": _TwoPhaseJudge("judge-y", "fam-jy", dict(low)),
            "judge-z": _TwoPhaseJudge("judge-z", "fam-jz", dict(low)),
        }
        self.panel("gen-p", "gen-q", ["gen-z"],
                   ["gen-p", "gen-q", "gen-z",
                    "judge-x", "judge-y", "judge-z"])
        self.store.upsert_route(
            "code", "gen-p", "gen-q", ["gen-z"],
            ["judge-x", "judge-y", "judge-z"], "peer")
        result = loop_mod.run_contract(
            self.store, _legacy_contract(), "repo://fake", executors,
            human_decider=None)
        self.assertEqual(result["status"], "awaiting_human_gate", result)
        self.assertEqual(
            self.store.get_candidate(result["winner"])["expert_id"],
            "gen-z")
        verdict = self.store.latest_verdict("ctr_fake")
        self.assertEqual(verdict["attempt"], 2, verdict)
        lesson = self.store.get_lesson(result["lesson_id"])
        self.assertEqual(lesson["route_change"]["demote"], "gen-p",
                         lesson)
        route = self.store.get_route("code")
        self.assertEqual(
            (route["primary"], route["compete_with"], route["fallback"]),
            ("gen-z", "gen-p", ["gen-q"]), route)
        return counts

    def test_attempt1_only_passer_not_promoted(self):
        # Shape 1: gen-q's only completed row is attempt 1's; its
        # deciding-attempt row is unavailable.
        counts = self._run_shape(1)
        self.assertEqual(counts.get("gen-q"), 1, counts)
        q_rows = [c for c in self.store.candidates_for("ctr_fake")
                  if c["expert_id"] == "gen-q" and c["attempt"] == 2]
        self.assertEqual([c["status"] for c in q_rows], ["unavailable"])

    def test_pass_then_fail_seat_not_promoted(self):
        # Shape 2: gen-q passes attempt 1 and fails the deciding
        # attempt's measured gate.
        counts = self._run_shape(2)
        self.assertEqual(counts.get("gen-q"), 2, counts)
        q2 = [c for c in self.store.candidates_for("ctr_fake")
              if c["expert_id"] == "gen-q" and c["attempt"] == 2]
        self.assertEqual(
            [(c["status"],
              (c.get("evidence") or {}).get("eliminated_kind"))
             for c in q2],
            [("completed", "measured")])


# ---------------------------------------------------------------------------
# F1 — direct _write_lesson drive (the lane probes' exact shapes)
# ---------------------------------------------------------------------------

class TestAttemptScopingDirectDrive(LoopV03Base):
    """Hand-built rows against the real derivation, following the
    round-11 M1a pin's method. The demote target (exp-y) fails the
    deciding gate through a deciding-attempt measurement row;
    exp-z is the sole deciding passer."""

    def _drive(self, specs, measurements_for, route=("exp-y", "exp-q",
                                                     ["exp-z"])):
        store = self.store
        cids = {}
        for attempt, slot, eid, kind in specs:
            cid = store.create_candidate(
                "ctr_fake", attempt, "implement", eid, f"fam-{eid}",
                slot, f"wt://{eid}", "basehash")
            store.update_candidate(cid, status="completed")
            if kind:
                row = store.get_candidate(cid)
                ev = dict(row.get("evidence") or {})
                ev["eliminated_kind"] = kind
                store.update_candidate(cid, evidence=ev)
            cids[(attempt, eid)] = cid
        store.upsert_route("code", route[0], route[1], route[2], [],
                           "seed")
        winner = cids[(2, "exp-z")]
        store.create_verdict(
            "ctr_fake", 2, winner, [], "pass", "fail", None, False,
            None, None, "pending", None)
        gate_details = {
            "candidate_experts": {cid: eid
                                  for (_a, eid), cid in cids.items()},
            "hard_failures": [],
            "measurements": [
                {"candidate_id": cids[key], "metric": "ops_per_tick",
                 "value": 99, "op": "<=", "threshold": 8, "pass": False}
                for key in measurements_for
            ],
        }
        run = loop_mod._Run(store, _contract(), "repo://fake", {},
                            human_decider=None)
        lesson_id = run._write_lesson(2, gate_details)
        self.assertIsNotNone(lesson_id)
        lesson = store.get_lesson(lesson_id)
        self.assertEqual(lesson["route_change"]["demote"], "exp-y",
                         lesson)
        return store.get_route("code")

    def test_attempt1_only_passer_not_promoted_direct(self):
        # exp-q has a completed attempt-1 row and NO deciding row.
        route = self._drive(
            specs=[
                (1, "a", "exp-y", None), (1, "b", "exp-q", None),
                (1, "c", "exp-z", None),
                (2, "a", "exp-y", None), (2, "c", "exp-z", None),
            ],
            measurements_for=[(2, "exp-y")])
        self.assertEqual(
            (route["primary"], route["compete_with"], route["fallback"]),
            ("exp-z", "exp-y", ["exp-q"]), route)

    def test_pass_then_fail_seat_not_promoted_direct(self):
        # exp-q passes attempt 1 (clean row) and fails the deciding
        # attempt (its attempt-2 candidate is in failed_cids; its
        # clean attempt-1 candidate id is what the mutant admits).
        route = self._drive(
            specs=[
                (1, "a", "exp-y", None), (1, "b", "exp-q", None),
                (1, "c", "exp-z", None),
                (2, "a", "exp-y", None), (2, "b", "exp-q", None),
                (2, "c", "exp-z", None),
            ],
            measurements_for=[(2, "exp-y"), (2, "exp-q")])
        self.assertEqual(
            (route["primary"], route["compete_with"], route["fallback"]),
            ("exp-z", "exp-y", ["exp-q"]), route)

    def test_attempt1_elimination_does_not_leak_control(self):
        # CONTROL (passes with and without the attempt filter —
        # not a discriminating pin): exp-x was gate_error-killed
        # on attempt 1 but passes the deciding attempt, so it IS
        # a deciding passer and the demotion promotes it.
        route = self._drive(
            specs=[
                (1, "a", "exp-y", None), (1, "b", "exp-x", "gate_error"),
                (1, "c", "exp-z", None),
                (2, "a", "exp-y", None), (2, "b", "exp-x", None),
                (2, "c", "exp-z", None),
            ],
            measurements_for=[(2, "exp-y")],
            route=("exp-y", "exp-x", ["exp-z"]))
        self.assertEqual(
            (route["primary"], route["compete_with"], route["fallback"]),
            ("exp-x", "exp-z", ["exp-y"]), route)


# ---------------------------------------------------------------------------
# F3 — the M1a pin's store-shape half, at route level
# ---------------------------------------------------------------------------

class TestStoreShapeMeasurementIsLoadBearing(LoopV03Base):
    """Single deciding attempt, no elimination kind on any row.
    exp-p fails through a store-shape row (``pass: False``) and
    is the demote target; exp-x fails ONLY through a store-shape
    row; exp-z is the sole passer. Sever ``_measurement_failed``
    to the raw shape only (the round-12 ``A-P-store`` mutant) and
    exp-x's failure becomes invisible to the derivation — it is
    promoted over exp-z and this pin fails. (The round-11 pin's
    store-shape candidate was the demotee itself, so this route
    outcome could not move there.)"""

    def test_store_shape_failure_excludes_non_demotee(self):
        cid = "ctr_fake"
        self.store.upsert_route("code", "exp-p", "exp-x", ["exp-z"],
                                [], "seed")
        cids = {}
        for slot, eid in (("a", "exp-p"), ("b", "exp-x"),
                          ("c", "exp-z")):
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
                {"candidate_id": cids["exp-p"], "metric": "ops_per_tick",
                 "value": 99, "op": "<=", "threshold": 8, "pass": False},
                {"candidate_id": cids["exp-x"], "metric": "ops_per_tick",
                 "value": 99, "op": "<=", "threshold": 8, "pass": False},
            ],
        }
        run = loop_mod._Run(self.store, _contract(), "repo://fake", {},
                            human_decider=None)
        lesson_id = run._write_lesson(1, gate_details)
        self.assertIsNotNone(lesson_id)
        lesson = self.store.get_lesson(lesson_id)
        self.assertEqual(lesson["route_change"]["demote"], "exp-p",
                         lesson)
        route = self.store.get_route("code")
        self.assertEqual(
            (route["primary"], route["compete_with"], route["fallback"]),
            ("exp-z", "exp-p", ["exp-x"]), route)


# ---------------------------------------------------------------------------
# F4 — the matrix shuffle site, pinned by behavior
# ---------------------------------------------------------------------------

_MATRIX_SNIPPET = r'''
import json, sys
sys.path.insert(0, "__ROOT__")
from hybrid_critic_loop.matrix import run_matrix


class _Store:
    def __init__(self):
        self.scores = []

    def add_score(self, candidate_id, gate, rubric, judge_id,
                  judge_family, blind, position_shown, items, mean,
                  confidence=None, details=None):
        self.scores.append({"candidate_id": candidate_id,
                            "judge_id": judge_id,
                            "position_shown": position_shown})


class _Peer:
    def __init__(self, family):
        self.family = family

    def judge(self, packet, rubric):
        entry = {"items": {row: {"score": 8, "confidence": 0.9,
                                 "evidence": "src/impl.py:42",
                                 "rationale": "addresses the item"}
                           for row in ("question_fidelity",
                                       "completeness", "evidence",
                                       "test_agreement")}}
        return {label: entry for label in "ABCDEF"}


letters = "abcdef"
cands = [{"id": f"cand-{x}", "family": f"fam-{x}",
          "expert_id": f"expert-{x}", "contract_id": "ctr-1",
          "provenance": "generated", "evidence": {}}
         for x in letters]
peers = {f"expert-{x}": _Peer(f"fam-{x}") for x in letters}
contract = {"id": "ctr-1", "task": "Implement the sampler",
            "domain": "code", "acceptance": []}
store = _Store()
run_matrix(store, contract, cands, {},
           {c["id"]: "diff text" for c in cands}, peers,
           min_score=7, seed=7)
out = {}
for row in store.scores:
    out.setdefault(row["judge_id"], []).append(
        (row["position_shown"], row["candidate_id"]))
print(json.dumps({j: [cid for _p, cid in sorted(rows)]
                  for j, rows in out.items()}))
'''


def _matrix_fixtures():
    letters = "abcdef"
    cands = [{"id": f"cand-{x}", "family": f"fam-{x}",
              "expert_id": f"expert-{x}", "contract_id": "ctr-1",
              "provenance": "generated", "evidence": {}}
             for x in letters]
    peers = {}
    for c in cands:
        peers[c["expert_id"]] = _MatrixPeer(c["family"])
    contract = {"id": "ctr-1", "task": "Implement the sampler",
                "domain": "code", "acceptance": []}
    diffs = {c["id"]: "diff text" for c in cands}
    return cands, peers, contract, diffs


class _MatrixStore:
    def __init__(self):
        self.scores = []

    def add_score(self, candidate_id, gate, rubric, judge_id,
                  judge_family, blind, position_shown, items, mean,
                  confidence=None, details=None):
        self.scores.append({"candidate_id": candidate_id,
                            "judge_id": judge_id,
                            "judge_family": judge_family,
                            "position_shown": position_shown})


class _MatrixPeer:
    def __init__(self, family):
        self.family = family

    def judge(self, packet, rubric):
        entry = cell_entry(uniform(8))
        return {label: entry for label in "ABCDEF"}


def _positions_by_judge(scores):
    out = {}
    for row in scores:
        out.setdefault(row["judge_id"], []).append(
            (row["position_shown"], row["candidate_id"]))
    return {j: [cid for _p, cid in sorted(rows)]
            for j, rows in out.items()}


def _expected_positions(cands, seed):
    expected = {}
    for c in cands:
        judge_id = c["expert_id"]
        eligible = [o for o in cands if o["family"] != c["family"]]
        order = list(eligible)
        judges.seeded_shuffle_rng(seed, judge_id).shuffle(order)
        expected[judge_id] = [o["id"] for o in order]
    return expected


class TestMatrixShuffleSiteBehavioral(unittest.TestCase):
    """``run_matrix`` persists each peer's cells with
    ``position_shown`` in presentation order; that order must be
    the shared helper's order for the seed the caller passed —
    at the matrix site itself, not only inside the helper (the
    round-11 pins) and not only as source text."""

    def test_matrix_positions_match_helper_order(self):
        for seed in (7, 13):
            cands, peers, contract, diffs = _matrix_fixtures()
            store = _MatrixStore()
            run_matrix(store, contract, cands, {}, diffs, peers,
                       min_score=7, seed=seed)
            self.assertEqual(len(store.scores), 6 * 5)
            self.assertEqual(_positions_by_judge(store.scores),
                             _expected_positions(cands, seed))

    def test_matrix_positions_reproduce_across_processes(self):
        cands, _peers, _contract, _diffs = _matrix_fixtures()
        expected = _expected_positions(cands, 7)
        snippet = _MATRIX_SNIPPET.replace("__ROOT__", str(ROOT))
        outputs = []
        for hash_seed in ("0", "42"):
            env = dict(os.environ, PYTHONHASHSEED=hash_seed)
            proc = subprocess.run(
                [sys.executable, "-c", snippet], capture_output=True,
                text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outputs.append(json.loads(proc.stdout.strip()))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], expected)


# ---------------------------------------------------------------------------
# F2 — leg-1 single-confound siblings (see module docstring for the
# siblings that already exist in test_fix_round11_chain.py)
# ---------------------------------------------------------------------------

class TestLeg1SingleConfoundSiblings(TighteningTestCase):
    def _book(self, cid="ctr_0001"):
        self._contract(cid)
        self._dispatch(cid)
        _pending_verdict(self.store, cid)
        return cid

    def test_null_decision_on_completed_book_is_not_completed(self):
        # The null pair's missing sibling: ONLY the decision-set
        # conjunct can refuse — the pair agrees (both record no
        # decision), the sha is full-length hex, and the status
        # IS settled-completed. Sever the decision-set conjunct
        # and leg 1 completes this book.
        cid = self._book()
        _plant_pair(self, cid, None, f"git:{SHA}", SHA,
                    obs_decision=None, status="completed")
        self.assertFalse(chain._contract_completed(self.store, cid))
        self.assertIsNone(chain.issue_certificate(self.store, cid))

    def test_approved_pair_on_rejected_status_is_not_completed(self):
        # The rejected pair's status half with the plant's own
        # status value: ONLY the settled-status conjunct can
        # refuse — the decision is answer-producing, the pair
        # agrees, and the sha is full-length hex, but the book
        # settled as "rejected", not "completed". Sever the
        # status conjunct and leg 1 completes this book. (The
        # round-11 status pin uses an unsettled "open" book.)
        cid = self._book()
        _plant_pair(self, cid, "approved", f"git:{SHA}", SHA,
                    status="rejected")
        self.assertFalse(chain._contract_completed(self.store, cid))
        self.assertIsNone(chain.issue_certificate(self.store, cid))


if __name__ == "__main__":
    unittest.main()
