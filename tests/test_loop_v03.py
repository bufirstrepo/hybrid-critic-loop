"""End-to-end matrix + bracket tests (v0.3, The Millionaire Matrix).

Fully in-memory fakes — no subprocesses, no git — so the machinery
under test is exactly the loop's: panel composition, the peer
validity matrix over scripted judge cells, the step-down bracket
(multi-round), the dissent gate re-run trigger, low-confidence
escalation, the durable events table, and validity bookkeeping
(cost unreported -> CPVP "not reported"). The real-git end-to-end
path is covered by tests/test_loop.py; the real local-solver demo
panel is exercised by demo/run_demo.py.
"""

import unittest

from hybrid_critic_loop import gates as gates_mod
from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.contracts import validate_contract
from hybrid_critic_loop.executors import Executor, GenResult
from hybrid_critic_loop.state import Store

ROWS = ["question_fidelity", "completeness", "evidence", "test_agreement"]


def _cell(score, confidence=0.9, evidence="packet section", rationale="scripted"):
    return {"score": score, "confidence": confidence,
            "evidence": evidence, "rationale": rationale}


class FakeGen(Executor):
    """Completes instantly; 'writes' its marker into the worktree diff
    via the loop's diff hook (patched per test module)."""

    def __init__(self, expert_id, family, marker):
        self.expert_id = expert_id
        self.family = family
        self.marker = marker

    def available(self):
        return (True, "fake generator")

    def generate(self, worktree, contract, context):
        return GenResult(status="completed", log=f"generated {self.marker}",
                         tokens_in=100, tokens_out=50, cost_usd=None,
                         request_ids=[], duration_ms=10,
                         detail=f"marker={self.marker}")


class FakeJudge(Executor):
    """Scores by marker: parses `marker=<name>` out of each candidate
    section of the blinded packet and returns scripted per-marker
    scores. ``overrides`` maps (marker, row) -> cell dict, applied
    once per judge instance unless ``repeat_overrides`` is set."""

    def __init__(self, expert_id, family, scores, overrides=None,
                 repeat_overrides=False):
        self.expert_id = expert_id
        self.family = family
        self.scores = scores          # marker -> base score (all rows)
        self.overrides = overrides or {}
        self.repeat_overrides = repeat_overrides
        self.calls = 0
        self._fired = set()

    def available(self):
        return (True, "fake judge")

    def generate(self, worktree, contract, context):
        return GenResult(status="unavailable", log="", duration_ms=0,
                         detail="judge does not generate")

    def judge(self, packet, rubric):
        self.calls += 1
        result = {}
        # The packet's candidate sections carry `marker=<name>` lines
        # (injected by the patched diff hook below).
        import re
        sections = re.split(r"=== CANDIDATE ([A-Za-z]+) ===", packet or "")
            # sections: [pre, label, body, label, body, ...]
        for i in range(1, len(sections) - 1, 2):
            label, body = sections[i], sections[i + 1]
            m = re.search(r"marker=([\w-]+)", body)
            if not m:
                continue
            marker = m.group(1)
            base = self.scores.get(marker, 5.0)
            items = {}
            for row in rubric["items"]:
                # NOTE: marker names must not collide with any expert
                # id or family string — the matrix blinds packets by
                # replacing those terms, which would eat the marker.
                # The tests use markers like "m-alpha" for that reason.
                override = self.overrides.get((marker, row))
                if override is not None and (
                        self.repeat_overrides or (marker, row) not in self._fired):
                    self._fired.add((marker, row))
                    items[row] = dict(override)
                else:
                    items[row] = _cell(base)
            result[label] = {"items": items}
        return result or None

class FakeWorktrees:
    """In-memory stand-in for WorktreeManager (diffs carry markers)."""

    def __init__(self, markers):
        self.markers = markers  # expert_id -> marker
        self._by_path = {}

    def create(self, contract_id, name):
        path = f"wt://{contract_id}/{name}"
        return path

    def baseline_hash(self):
        return "basehash"

    def diff(self, wt):
        marker = self._by_path.get(str(wt), "")
        return f"candidate patch payload\nmarker={marker}\nsome code line\n"


    def commit_all(self, wt, msg):
        return "deadbeef"

    def merge_into_main(self, wt):
            return "cafef00d"

    def keep_as_ref(self, contract_id, slot):
        return None

    def remove(self, wt):
        return None


def _contract(**overrides):
    raw = {
        "id": "ctr_fake",
        "stage": "implement",
        "task": "Implement the fake thing.",
        "domain": "code.game",
        "tags": ["fake"],
        "acceptance": [
            {"class": "hard", "cmd": "true", "expect": {"exit": 0}},
            {"class": "measured",
             "metrics": [{"name": "ops_per_tick", "op": "<=", "value": 8}],
             "sensor": "ops_bench", "sensor_cmd": "true"},
            {"class": "judged", "rubric": "validity_matrix_v1", "min": 6,
             "judges": 6},
        ],
    }
    raw.update(overrides)
    return validate_contract(raw)


class LoopV03Base(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self._orig_wm = loop_mod.WorktreeManager
        self._orig_eval = gates_mod.evaluate_candidate
        self._orig_verify = loop_mod.verify_baseline
        self.markers = {}          # expert_id -> marker
        self.eliminated_at = {}    # marker -> eliminated_at value
        self.eval_counts = {}      # candidate marker -> evaluate calls

        markers, eliminated_at, eval_counts = (
            self.markers, self.eliminated_at, self.eval_counts)

        class WM(FakeWorktrees):
            _dispatch_order = []

            def __init__(self, repo_path):
                super().__init__(markers)
                self.repo_path = repo_path

            def create(self, contract_id, name):
                # Slot names are "<slot>" or "<slot><attempt>"; the
                # loop dispatches generators in panel order (route
                # primary, compete_with, then fallback) into slots
                # a, b, c, ... — the test sets _dispatch_order to the
                # generator expert ids in that same order. `name` may
                # be a plain string or a Path (the baseline check
                # passes a Path) — take its final component.
                path = super().create(contract_id, name)
                order = type(self)._dispatch_order
                slot = str(name).replace("\\", "/").rstrip("/").split("/")[-1]
                idx = "abcdefghijklmnopqrstuvwxyz".find(slot[0]) if slot else -1
                if 0 <= idx < len(order):
                    self._by_path[path] = markers.get(order[idx], "")
                else:
                    # Not a generator slot (baseline/judges): the Nth
                    # worktree overall still needs a stable mapping —
                    # fall back to creation order among unmapped paths.
                    pass
                return path

        self._wm_class = WM

        def fake_eval(store, contract, candidate, cwd):
            marker = markers.get(candidate["expert_id"], "?")
            eval_counts[marker] = eval_counts.get(marker, 0) + 1
            elim = eliminated_at.get(marker)
            # Carry the contract's measured metric name so matrix
            # dissent detection can recognise a citation of it.
            measured = [
                gates_mod.MeasureResult(
                    sensor="ops_bench",
                    metrics=[{"metric": "ops_per_tick", "value": 1.0,
                              "op": "<=", "threshold": 8, "passed": True}],
                    passed=True,
                )
            ]
            return gates_mod.CandidateGateResult(
                candidate_id=candidate["id"],
                hard=[], measured=measured,
                provenance_ok=True, eliminated_at=elim,
            )

        loop_mod.WorktreeManager = WM
        gates_mod.evaluate_candidate = fake_eval
        loop_mod.gates_mod.evaluate_candidate = fake_eval
        loop_mod.verify_baseline = lambda *a, **k: (True, {"fake": True})

    def tearDown(self):
        loop_mod.WorktreeManager = self._orig_wm
        gates_mod.evaluate_candidate = self._orig_eval
        loop_mod.gates_mod.evaluate_candidate = self._orig_eval
        loop_mod.verify_baseline = self._orig_verify
        self.store.close()

    def route(self, primary, compete, fallback):
        self.store.upsert_route(
            "code", primary, compete, fallback,
            [], "peer_matrix; no self-scoring; blind",
        )

    def panel(self, primary, compete, fallback, generators_in_order):
        """Register the route AND the generator dispatch order (the
        expert ids the loop will dispatch into slots a, b, c, ...)."""
        self.route(primary, compete, fallback)
        self._wm_class._dispatch_order = list(generators_in_order)


class TestPanelBracket(LoopV03Base):
    def test_five_family_panel_steps_down_to_one(self):
        gens = {}
        scores = {}
        for i, name in enumerate(["alpha", "bravo", "charlie", "delta", "echo"]):
            eid = f"gen-{name}"
            self.markers[eid] = f"m-{name}"
            gens[eid] = FakeGen(eid, f"fam-{name}", f"m-{name}")
            scores[f"m-{name}"] = 9.0 - i  # m-alpha 9 ... m-echo 5
        judges = {
            f"judge-{n}": FakeJudge(f"judge-{n}", f"fam-{n}", scores)
            for n in ["alpha", "bravo", "charlie", "delta", "echo"]
        }
        executors = {**gens, **judges}
        self.panel("gen-alpha", "gen-echo",
                   ["gen-bravo", "gen-charlie", "gen-delta",
                    "judge-alpha", "judge-bravo", "judge-charlie",
                    "judge-delta", "judge-echo"],
                   ["gen-alpha", "gen-echo", "gen-bravo",
                    "gen-charlie", "gen-delta"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "completed", result)
        verdict = self.store.latest_verdict("ctr_fake")
        winner = self.store.get_candidate(result["winner"])
        self.assertEqual(winner["expert_id"], "gen-alpha")
        judged = verdict["judged"]
        rounds = judged["bracket"]["rounds"]
        # fixed cut 2: 5 -> 3 -> 2 -> 1 (three scored rounds)
        self.assertEqual(
            [(r["field_in"], r["field_out"]) for r in rounds],
            [(5, 3), (3, 2), (2, 1)],
        )
        self.assertEqual(rounds[0]["cut_line"], 7.0)
        self.assertEqual(judged["panel_status"], "full")
        # The verdict's eliminated list covers every non-winner
        # candidate generated in the final attempt — the four bracket
        # losers (judge seats were also dispatched as panel members;
        # their unavailable rows are part of the panel record).
        final_attempt = verdict["attempt"]
        gen_ids = {
            c["id"] for c in self.store.candidates_for("ctr_fake")
            if c["attempt"] == final_attempt
            and c["expert_id"].startswith("gen-")
        }
        self.assertEqual(len(gen_ids), 5)
        self.assertEqual(
            set(verdict["eliminated"]) & gen_ids,
            gen_ids - {result["winner"]},
        )
        # Validity: winner earned its matrix score; cost unreported.
        summary = {r["expert_id"]: r for r in self.store.ledger_summary()}
        self.assertAlmostEqual(summary["gen-alpha"]["validity_points"], 9.0)
        for row in summary.values():
            self.assertIsNone(row["cost_per_validity_point"])
        # Events persisted (owner item 8).
        types = [e["type"] for e in self.store.events_for("ctr_fake")]
        self.assertIn("human_gate", types)
        # Bracket rounds in the ledger.
        notes = [r["note"] or "" for r in self.store.ledger_rows()]
        self.assertEqual(sum("bracket round" in n for n in notes), 3)


class TestAnchorAtAnyRound(LoopV03Base):
    def test_measured_failure_eliminates_despite_top_matrix_score(self):
        self.markers.update({"gen-a": "m-a", "gen-b": "m-b", "gen-c": "m-c"})
        self.eliminated_at["m-a"] = "measured"  # best score, dead anchor
        executors = {
            "gen-a": FakeGen("gen-a", "fam-a", "m-a"),
            "gen-b": FakeGen("gen-b", "fam-b", "m-b"),
            "gen-c": FakeGen("gen-c", "fam-c", "m-c"),
            "judge-a": FakeJudge("judge-a", "fam-a", {"m-a": 10.0, "m-b": 8.0, "m-c": 7.0}),
            "judge-b": FakeJudge("judge-b", "fam-b", {"m-a": 10.0, "m-b": 8.0, "m-c": 7.0}),
            "judge-c": FakeJudge("judge-c", "fam-c", {"m-a": 10.0, "m-b": 8.0, "m-c": 7.0}),
        }
        self.panel("gen-a", "gen-b",
                   ["gen-c", "judge-a", "judge-b", "judge-c"],
                   ["gen-a", "gen-b", "gen-c"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "completed", result)
        winner = self.store.get_candidate(result["winner"])
        self.assertEqual(winner["expert_id"], "gen-b")
        verdict = self.store.latest_verdict("ctr_fake")
        a_cand = next(c for c in self.store.candidates_for("ctr_fake")
                      if c["expert_id"] == "gen-a")
        self.assertIn(a_cand["id"], verdict["eliminated"])
        self.assertNotIn(a_cand["id"], verdict["judged"])


class TestDissentRerun(LoopV03Base):
    def test_dissent_citing_measured_metric_reruns_gate_once(self):
        self.markers.update({"gen-a": "m-a", "gen-b": "m-b", "gen-c": "m-c"})
        dissent_cell = {
            "score": 1.0, "confidence": 0.9,
            "evidence": "ops_per_tick reading disputed",
            "rationale": "the ops_per_tick sensor looks misread",
        }
        judges = {
            "judge-a": FakeJudge("judge-a", "fam-a", {"m-a": 8.0, "m-b": 7.0, "m-c": 9.0}),
            "judge-b": FakeJudge("judge-b", "fam-b", {"m-a": 8.0, "m-b": 7.0, "m-c": 9.0}),
            "judge-c": FakeJudge(
                "judge-c", "fam-c", {"m-a": 8.0, "m-b": 7.0, "m-c": 9.0},
                overrides={("m-b", "test_agreement"): dissent_cell},
                repeat_overrides=True),
        }
        executors = {
            "gen-a": FakeGen("gen-a", "fam-a", "m-a"),
            "gen-b": FakeGen("gen-b", "fam-b", "m-b"),
            "gen-c": FakeGen("gen-c", "fam-c", "m-c"),
            **judges,
        }
        self.panel("gen-a", "gen-b", ["gen-c", "judge-a", "judge-b", "judge-c"],
                   ["gen-a", "gen-b", "gen-c"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "completed", result)
        # Candidate b's gates were evaluated twice: the initial pass
        # plus the dissent-triggered re-run. (judge-c's low cell cites
        # the ops_per_tick metric; judge-a's high cell on the same row
        # is the majority side of that dissent — the re-run request
        # comes from the citing cell.)
        self.assertGreaterEqual(self.eval_counts.get("m-b", 0), 2)
        # And the matrix re-ran for that round (judge-c called again).
        self.assertGreaterEqual(judges["judge-c"].calls, 2)


class TestDegradedVerdictHoldsRouteChange(LoopV03Base):
    """Owner floor rule (README "Panel floor"): a degraded matrix never
    closes the lesson loop on its own. The Lesson is stored; a route
    change derived from the gates is HELD — visible on the verdict row,
    in the ledger and on the run result — never applied. A full panel
    applies it as before. (Fix pass Wave 2.1, packet F1.)"""

    def setUp(self):
        super().setUp()
        # The shared fake evaluator records no measurement rows; the
        # lesson derives its route change from those rows, so record a
        # failing one for every candidate the test eliminates at measured.
        base_eval = gates_mod.evaluate_candidate
        markers, eliminated_at = self.markers, self.eliminated_at

        def eval_with_rows(store, contract, candidate, cwd):
            gr = base_eval(store, contract, candidate, cwd)
            if eliminated_at.get(markers.get(candidate["expert_id"])) == "measured":
                store.add_measurement(
                    candidate["id"], "ops_bench", "ops_per_tick", 120.0, "<=", 8, False
                )
            return gr

        gates_mod.evaluate_candidate = eval_with_rows
        loop_mod.gates_mod.evaluate_candidate = eval_with_rows

    def _generators(self):
        self.markers.update({"gen-a": "m-a", "gen-b": "m-b", "gen-c": "m-c"})
        self.eliminated_at["m-a"] = "measured"  # the anchor kills gen-a
        return {
            "gen-a": FakeGen("gen-a", "fam-a", "m-a"),
            "gen-b": FakeGen("gen-b", "fam-b", "m-b"),
            "gen-c": FakeGen("gen-c", "fam-c", "m-c"),
        }

    def _assert_held(self, result, lesson_id):
        verdict = self.store.latest_verdict("ctr_fake")
        self.assertEqual(verdict["judged"]["panel_status"], "degraded")
        lesson = self.store.get_lesson(lesson_id)
        self.assertIsNotNone(lesson)                                      # the Lesson is stored
        self.assertEqual(lesson["route_change"]["demote"], "gen-a")        # with its derived change
        self.assertEqual(self.store.get_route("code")["primary"], "gen-a",
                         "a degraded verdict must not apply the demotion")
        hold = result.get("route_change_hold")
        self.assertIsNotNone(hold, result)
        self.assertEqual(hold["status"], "held")
        self.assertEqual(hold["route_change"]["demote"], "gen-a")
        self.assertEqual(verdict["route_hold"]["lesson_id"], lesson_id)    # on the verdict row
        notes = [r["note"] or "" for r in self.store.ledger_rows()]
        held = [n for n in notes if "route_change held" in n and "degraded" in n]
        self.assertEqual(len(held), 1, notes)                             # in the ledger
        self.assertFalse(any(n.startswith("route_change demote") for n in notes))

    def test_degraded_bracket_verdict_holds_the_demotion(self):
        executors = {**self._generators(),
                     "judge-z": FakeJudge("judge-z", "fam-z", {"m-a": 9.0, "m-b": 8.0, "m-c": 7.0})}
        self.panel("gen-a", "gen-b", ["gen-c", "judge-z"], ["gen-a", "gen-b", "gen-c"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.store.get_candidate(result["winner"])["expert_id"], "gen-b")
        self._assert_held(result, result["lesson_id"])

    def test_degraded_escalation_holds_the_demotion_too(self):
        class ShyJudge(FakeJudge):
            def judge(self, packet, rubric):
                out = super().judge(packet, rubric)
                if out:
                    for cell in out.values():
                        for item in cell["items"].values():
                            item["confidence"] = 0.3
                return out

        executors = {**self._generators(),
                     "judge-z": ShyJudge("judge-z", "fam-z", {"m-a": 8.0, "m-b": 8.0, "m-c": 8.0})}
        self.panel("gen-a", "gen-b", ["gen-c", "judge-z"], ["gen-a", "gen-b", "gen-c"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "escalated", result)
        self.assertIn("confidence", result["detail"])
        self._assert_held(result, result["lesson_id"])

    def test_full_panel_still_applies_the_demotion(self):
        scores = {"m-a": 9.0, "m-b": 8.0, "m-c": 7.0}
        executors = {**self._generators(),
                     "judge-y": FakeJudge("judge-y", "fam-y", scores),
                     "judge-z": FakeJudge("judge-z", "fam-z", scores)}
        self.panel("gen-a", "gen-b", ["gen-c", "judge-y", "judge-z"], ["gen-a", "gen-b", "gen-c"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "completed", result)
        verdict = self.store.latest_verdict("ctr_fake")
        self.assertEqual(verdict["judged"]["panel_status"], "full")
        self.assertIsNone(verdict["route_hold"])
        self.assertNotIn("route_change_hold", result)
        self.assertEqual(self.store.get_route("code")["primary"], "gen-b")
        notes = [r["note"] or "" for r in self.store.ledger_rows()]
        self.assertTrue(any(n.startswith("route_change demote gen-a") for n in notes), notes)


class TestInMemoryStoreLeavesTheCwdAlone(LoopV03Base):
    """A run on Store(':memory:') must not write lessons.md into the current
    directory (fix pass Wave 2.4, packet F3: that is how the suite used to
    dirty the plugin tree)."""

    def test_no_lessons_md_appears_in_the_cwd(self):
        import contextlib
        import os
        import tempfile

        self.markers.update({"gen-a": "m-a", "gen-b": "m-b", "gen-c": "m-c"})
        scores = {"m-a": 9.0, "m-b": 8.0, "m-c": 7.0}
        executors = {
            "gen-a": FakeGen("gen-a", "fam-a", "m-a"),
            "gen-b": FakeGen("gen-b", "fam-b", "m-b"),
            "gen-c": FakeGen("gen-c", "fam-c", "m-c"),
            "judge-y": FakeJudge("judge-y", "fam-y", scores),
            "judge-z": FakeJudge("judge-z", "fam-z", scores),
        }
        self.panel("gen-a", "gen-b", ["gen-c", "judge-y", "judge-z"], ["gen-a", "gen-b", "gen-c"])
        with tempfile.TemporaryDirectory() as td, contextlib.chdir(td):
            result = loop_mod.run_contract(
                self.store, _contract(), "repo://fake", executors,
                human_decider=lambda info: "approved",
            )
            self.assertEqual(result["status"], "completed", result)
            self.assertIsNotNone(result["lesson_id"])          # the row is written ...
            self.assertEqual(os.listdir(td), [])                # ... the cwd stays clean
        self.assertIsNone(loop_mod._lesson_data_dir(self.store))


class TestLowConfidence(LoopV03Base):
    def test_uniform_low_confidence_escalates(self):
        self.markers.update({"gen-a": "m-a", "gen-b": "m-b", "gen-c": "m-c"})

        class ShyJudge(FakeJudge):
            def judge(self, packet, rubric):
                result = super().judge(packet, rubric)
                if result:
                    for cell in result.values():
                        for item in cell["items"].values():
                            item["confidence"] = 0.3
                return result

        executors = {
            "gen-a": FakeGen("gen-a", "fam-a", "m-a"),
            "gen-b": FakeGen("gen-b", "fam-b", "m-b"),
            "gen-c": FakeGen("gen-c", "fam-c", "m-c"),
            "judge-a": ShyJudge("judge-a", "fam-a", {"m-a": 8.0, "m-b": 8.0, "m-c": 8.0}),
            "judge-b": ShyJudge("judge-b", "fam-b", {"m-a": 8.0, "m-b": 8.0, "m-c": 8.0}),
            "judge-c": ShyJudge("judge-c", "fam-c", {"m-a": 8.0, "m-b": 8.0, "m-c": 8.0}),
        }
        self.panel("gen-a", "gen-b", ["gen-c", "judge-a", "judge-b", "judge-c"],
                   ["gen-a", "gen-b", "gen-c"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "escalated", result)
        self.assertIn("confidence", result["detail"])


if __name__ == "__main__":
    unittest.main()
