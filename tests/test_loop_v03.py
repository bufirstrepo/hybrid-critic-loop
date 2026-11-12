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
from hybrid_critic_loop.errors import ContractError, GateError
from hybrid_critic_loop.executors import Executor, GenResult
from hybrid_critic_loop.reports import ledger_report, verdict_report
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

    def is_commit(self, commit):
        return commit == "deadbeef"

    def merge_commit_into_main(self, commit):
        assert commit == "deadbeef"
        return "cafef00d"

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
        # Validity: every scored candidate earned exactly its recorded
        # matrix median — the winner's final-round 9.0 and each bracket
        # loser's median from the round that cut it (fix pass Wave 2.7,
        # review H3: early-round losers were given an invented 0.0
        # before). The judge seats, dispatched but never scored, stay
        # unreported. Cost is unreported throughout, so CPVP is too.
        summary = {r["expert_id"]: r for r in self.store.ledger_summary()}
        self.assertAlmostEqual(summary["gen-alpha"]["validity_points"], 9.0)
        for name, points in (("bravo", 8.0), ("charlie", 7.0), ("delta", 6.0), ("echo", 5.0)):
            self.assertAlmostEqual(summary[f"gen-{name}"]["validity_points"], points, msg=name)
        for name in ["alpha", "bravo", "charlie", "delta", "echo"]:
            self.assertIsNone(summary[f"judge-{name}"]["validity_points"], name)
        for row in summary.values():
            self.assertIsNone(row["cost_per_validity_point"])
        # Wins and losses are charged to seats that fielded a candidate
        # (fix pass Wave 2.8, review M3): one loss per losing generator,
        # none for the judge seats, which produced nothing.
        self.assertEqual((summary["gen-alpha"]["wins"], summary["gen-alpha"]["losses"]), (1, 0))
        for name in ["bravo", "charlie", "delta", "echo"]:
            self.assertEqual((summary[f"gen-{name}"]["wins"], summary[f"gen-{name}"]["losses"]), (0, 1), name)
        for name in ["alpha", "bravo", "charlie", "delta", "echo"]:
            self.assertEqual(self.store.get_expert(f"judge-{name}")["losses"], 0, name)
        # The judge seats were dispatched and declined to generate: the
        # ledger says `unavailable`, not `refused`, and the summary counts
        # exactly one dispatch for each seat.
        for name in ["alpha", "bravo", "charlie", "delta", "echo"]:
            row = summary[f"judge-{name}"]
            self.assertEqual((row["dispatches"], row["unavailable"], row["refused"]), (1, 1, 0), name)
        self.assertNotIn("queue", summary)  # the queue's notes are not dispatches
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


class FakeSeat(FakeJudge):
    """A real-panel seat: generates AND judges (the codex/grok/claude shape)."""

    def __init__(self, expert_id, family, marker, scores):
        super().__init__(expert_id, family, scores)
        self.marker = marker

    def generate(self, worktree, contract, context):
        return GenResult(status="completed", log=f"generated {self.marker}",
                         tokens_in=100, tokens_out=50, cost_usd=None,
                         request_ids=[], duration_ms=10,
                         detail=f"marker={self.marker}")


class TestSeatsJudgeEachOther(LoopV03Base):
    """Fix pass Wave 2.5 (review H1, 1D-03, M1): the peer matrix seats every
    judge-capable, available executor — contestants included, self-cells
    stripped by family — skips the rest by name, and never calls a no-pass
    escalation a retry cap."""

    SCORES = {"m-a": 9.0, "m-b": 8.0, "m-c": 7.0}

    def _seats(self, names):
        for n in names:
            self.markers[f"seat-{n}"] = f"m-{n}"
        return {f"seat-{n}": FakeSeat(f"seat-{n}", f"fam-{n}", f"m-{n}", self.SCORES) for n in names}

    def test_three_live_seats_that_generate_and_judge_complete_with_a_full_panel(self):
        executors = self._seats("abc")
        self.panel("seat-a", "seat-b", ["seat-c"], ["seat-a", "seat-b", "seat-c"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "completed", result)
        verdict = self.store.latest_verdict("ctr_fake")
        self.assertEqual(verdict["judged"]["panel_status"], "full")
        self.assertEqual(self.store.get_candidate(result["winner"])["expert_id"], "seat-a")
        rounds = [(r["field_in"], r["field_out"]) for r in verdict["judged"]["bracket"]["rounds"]]
        self.assertEqual(rounds, [(3, 2), (2, 1)])
        for cand in self.store.candidates_for("ctr_fake"):
            for sc in self.store.scores_for(cand["id"]):
                self.assertNotEqual(sc["judge_family"], cand["family"])  # no self-score, ever
        notes = [r["note"] or "" for r in self.store.ledger_rows()]
        self.assertTrue(any(n.startswith("matrix peers seated=['seat-a', 'seat-b', 'seat-c']") for n in notes), notes)

    def test_two_live_seats_reach_a_degraded_verdict_instead_of_escalating(self):
        executors = self._seats("ab")
        self.panel("seat-a", "seat-b", [], ["seat-a", "seat-b"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.store.latest_verdict("ctr_fake")["judged"]["panel_status"], "degraded")
        self.assertEqual(self.store.get_candidate(result["winner"])["expert_id"], "seat-a")

    def test_unavailable_and_judge_less_seats_are_skipped_and_named(self):
        class Unreachable(FakeJudge):
            def available(self):
                return (False, "binary not found on PATH")

        class DeclinesToJudge(FakeJudge):
            def can_judge(self):
                return False

        executors = {**self._seats("abc"),
                     "judge-x": Unreachable("judge-x", "fam-x", self.SCORES),
                     "judge-y": DeclinesToJudge("judge-y", "fam-y", self.SCORES)}
        self.panel("seat-a", "seat-b", ["seat-c", "judge-x", "judge-y"], ["seat-a", "seat-b", "seat-c"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(executors["judge-x"].calls, 0)
        self.assertEqual(executors["judge-y"].calls, 0)
        note = next(n for n in (r["note"] or "" for r in self.store.ledger_rows()) if n.startswith("matrix peers"))
        self.assertIn("('judge-x', 'unavailable: binary not found on PATH')", note)
        self.assertIn("('judge-y', 'cannot judge')", note)
        self.assertNotIn("'judge-x'", note.split("skipped=")[0])

    def test_no_pass_escalation_carries_no_retry_cap_event(self):
        low = {"m-a": 3.0, "m-b": 3.0, "m-c": 3.0}  # all below the contract min of 6
        for n in "abc":
            self.markers[f"seat-{n}"] = f"m-{n}"
        executors = {f"seat-{n}": FakeSeat(f"seat-{n}", f"fam-{n}", f"m-{n}", low) for n in "abc"}
        self.panel("seat-a", "seat-b", ["seat-c"], ["seat-a", "seat-b", "seat-c"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "escalated", result)
        self.assertIn("peer matrix passed no candidate", result["detail"])
        self.assertEqual([e["type"] for e in self.store.events_for("ctr_fake")], [])
        verdict = self.store.latest_verdict("ctr_fake")
        self.assertTrue(verdict["escalated"])
        # The round that ended in escalation says so (fix pass Wave 2.8,
        # C-08): the record shows the field, how many passed, and why the
        # ladder did not land on two.
        rounds = verdict["judged"]["bracket"]["rounds"]
        self.assertEqual([(r["field_in"], r["field_out"], r["passed"]) for r in rounds], [(3, 0, 0)])
        note = next(n for n in (r["note"] or "" for r in self.store.ledger_rows())
                    if n.startswith("bracket round 1"))
        self.assertIn("passed=0", note)
        self.assertIn("no candidate passed the matrix", note)
        # An escalated verdict names who was out (fix pass Wave 2.10, 1B-20):
        # every fielded candidate of the deciding attempt.
        fielded = sorted(c["id"] for c in self.store.candidates_for("ctr_fake") if c["status"] == "completed")
        self.assertEqual(sorted(verdict["eliminated"]), fielded)
        self.assertEqual(len(fielded), 3)


class TestValidityPointsComeOnlyFromTheMatrix(LoopV03Base):
    """Fix pass Wave 2.7 (review H3, packet 1A-02/1B-08/1C-06): a validity
    point is a recorded matrix median and nothing else. Before, an
    anchor-only winner was handed 10.0 and every unscored candidate 0.0,
    and the ledger printed both as if measured."""

    def test_anchor_only_winner_earns_no_validity_points(self):
        # The demo's Phase A shape: the measured anchor leaves one
        # survivor, the matrix never runs, nothing was ever scored.
        self.markers.update({"gen-a": "m-a", "gen-b": "m-b"})
        self.eliminated_at["m-b"] = "measured"
        executors = {
            "gen-a": FakeGen("gen-a", "fam-a", "m-a"),
            "gen-b": FakeGen("gen-b", "fam-b", "m-b"),
            "judge-z": FakeJudge("judge-z", "fam-z", {"m-a": 9.0, "m-b": 8.0}),
        }
        self.panel("gen-a", "gen-b", ["judge-z"], ["gen-a", "gen-b"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.store.latest_verdict("ctr_fake")["tie_break"], "single_passer")
        self.assertEqual(executors["judge-z"].calls, 0)  # the matrix never ran
        summary = {r["expert_id"]: r for r in self.store.ledger_summary()}
        for eid in ("gen-a", "gen-b", "judge-z"):
            self.assertIsNone(summary[eid]["validity_points"], eid)  # no 10.0, no 0.0
            self.assertIsNone(summary[eid]["cost_per_validity_point"], eid)
        self.assertNotIn("validity_points=", ledger_report(self.store))


def _record_measured_failures(test):
    """The shared fake evaluator records no measurement rows; the lesson
    derives its route change from those rows, so record a failing one for
    every candidate the test eliminates at the measured gate."""
    base_eval = gates_mod.evaluate_candidate
    markers, eliminated_at = test.markers, test.eliminated_at

    def eval_with_rows(store, contract, candidate, cwd):
        gr = base_eval(store, contract, candidate, cwd)
        if eliminated_at.get(markers.get(candidate["expert_id"])) == "measured":
            store.add_measurement(
                candidate["id"], "ops_bench", "ops_per_tick", 120.0, "<=", 8, False
            )
        return gr

    gates_mod.evaluate_candidate = eval_with_rows
    loop_mod.gates_mod.evaluate_candidate = eval_with_rows


class TestMeasuredFailureNeedsAnExplicitWaiver(LoopV03Base):
    """Fix pass Wave 2.7 (review H6, invariant 6) made the explicit
    waiver the only way past a measured failure: the run offered the
    measured-failed candidate to the human gate, a plain approval was
    refused, and only an explicit waive merged it.

    Owner decisions 2026-11-09 (Item 2(a) = B) supersede that for
    runs: a measured-only regression is NOT a valid contract — the
    run fails it like any other gate failure and never offers a
    waiver (pinned by the first test below). The gate machinery over
    a verdict that DOES carry an offered waiver — approval refused,
    explicit waive completing it, the grant recorded with its
    channel — is unchanged and stays pinned by the gate fixtures
    (round-4 certificate placement, round-5 waiver-write failure,
    round-7 grant preservation) and by
    tests/test_fix_decisions_scoring.py."""

    def setUp(self):
        super().setUp()
        _record_measured_failures(self)

    def _executors(self):
        self.markers.update({"gen-a": "m-a", "gen-b": "m-b"})
        return {
            "gen-a": FakeGen("gen-a", "fam-a", "m-a"),
            "gen-b": FakeGen("gen-b", "fam-b", "m-b"),
            "judge-z": FakeJudge("judge-z", "fam-z", {"m-a": 9.0, "m-b": 8.0}),
        }

    def test_measured_regression_fails_the_contract_without_a_waiver_offer(self):
        executors = self._executors()
        self.eliminated_at.update({"m-a": "measured", "m-b": "measured"})  # nobody survives
        self.panel("gen-a", "gen-b", ["judge-z"], ["gen-a", "gen-b"])
        offered = {}
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: (offered.update(info), "approved")[1],
        )
        # The failure machinery ran to its end: retries exhausted and
        # the contract escalated. The run never reached a human gate —
        # the decider was never consulted, no verdict carries a
        # winner, and no waiver was offered or recorded.
        self.assertEqual(result["status"], "escalated", result)
        self.assertEqual(offered, {})
        verdict = self.store.latest_verdict("ctr_fake")
        self.assertTrue(verdict["escalated"], verdict)
        self.assertIsNone(verdict["winner"], verdict)
        self.assertIsNone(verdict["waiver"], verdict)
        self.assertEqual(verdict["measured"], "fail")
        self.assertEqual(verdict["human_gate"], "not_reached")
        self.assertEqual(self.store.get_contract("ctr_fake")["status"], "escalated")

    def test_waiving_a_verdict_without_a_measured_failure_is_refused(self):
        executors = self._executors()
        self.eliminated_at["m-b"] = "measured"  # gen-a stands alone, clean
        self.panel("gen-a", "gen-b", ["judge-z"], ["gen-a", "gen-b"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "waived",
        )
        self.assertEqual(result["status"], "error", result)
        self.assertIn("measured failure", result["detail"])
        self.assertIsNone(self.store.latest_verdict("ctr_fake")["waiver"])
        done = loop_mod.record_human_gate(self.store, "ctr_fake", "approved")
        self.assertEqual(done["status"], "completed", done)
        self.assertIsNone(done["waiver"])


class TestGateEvidenceIsPerAttempt(LoopV03Base):
    """Fix pass Wave 2.7 (review H7, packet 1B-04): the lesson and the
    escalation flags describe the attempt that decided. Before, hard
    failures accumulated across attempts, so a retry that won was blamed
    for the first attempt's failure — the Lesson demoted the winner's own
    expert — and an escalation after a clean retry still said hard=fail."""

    def setUp(self):
        super().setUp()
        base_eval = gates_mod.evaluate_candidate

        def hard_fail_on_the_first_attempt(store, contract, candidate, cwd):
            if candidate["attempt"] == 1:
                return gates_mod.CandidateGateResult(
                    candidate_id=candidate["id"], provenance_ok=True, eliminated_at="hard",
                    hard=[gates_mod.HardResult(cmd="pytest -q", expected_exit=0,
                                               exit_code=1, passed=False,
                                               stderr_tail="1 failed")],
                )
            return base_eval(store, contract, candidate, cwd)

        gates_mod.evaluate_candidate = hard_fail_on_the_first_attempt
        loop_mod.gates_mod.evaluate_candidate = hard_fail_on_the_first_attempt
        self.markers.update({"gen-a": "m-a", "gen-b": "m-b"})

    def _executors(self, scores):
        return {
            "gen-a": FakeGen("gen-a", "fam-a", "m-a"),
            "gen-b": FakeGen("gen-b", "fam-b", "m-b"),
            "judge-y": FakeJudge("judge-y", "fam-y", scores),
            "judge-z": FakeJudge("judge-z", "fam-z", scores),
        }

    def test_a_retry_that_wins_is_not_blamed_for_the_first_attempts_hard_failure(self):
        executors = self._executors({"m-a": 9.0, "m-b": 7.0})
        self.panel("gen-a", "gen-b", ["judge-y", "judge-z"], ["gen-a", "gen-b"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "completed", result)
        verdict = self.store.latest_verdict("ctr_fake")
        self.assertEqual(verdict["attempt"], 2)
        self.assertEqual(verdict["hard"], "pass")
        self.assertEqual(self.store.get_candidate(result["winner"])["expert_id"], "gen-a")
        # Both attempts are still on record (generator candidates; the judge
        # seats are dispatched too and sit beside them as unavailable rows) ...
        attempts = sorted(c["attempt"] for c in self.store.candidates_for("ctr_fake")
                          if c["expert_id"].startswith("gen-"))
        self.assertEqual(attempts, [1, 1, 2, 2])
        # ... but the Lesson is derived from the deciding attempt's gates:
        # a clean win, no demotion of the winner's own expert.
        lesson = self.store.get_lesson(result["lesson_id"])
        self.assertNotIn("failed the hard gate", lesson["outcome"])
        self.assertIsNone(lesson["route_change"], lesson)
        self.assertEqual(self.store.get_route("code")["primary"], "gen-a")
        # The winner is not charged a loss for its own first attempt
        # (fix pass Wave 2.8, review M3).
        self.assertEqual((self.store.get_expert("gen-a")["wins"], self.store.get_expert("gen-a")["losses"]), (1, 0))
        self.assertEqual(self.store.get_expert("gen-b")["losses"], 1)

    def test_an_escalation_after_a_clean_retry_reports_the_final_attempts_gates(self):
        executors = self._executors({"m-a": 3.0, "m-b": 3.0})  # both below the min of 6
        self.panel("gen-a", "gen-b", ["judge-y", "judge-z"], ["gen-a", "gen-b"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "escalated", result)
        self.assertIn("peer matrix passed no candidate", result["detail"])
        verdict = self.store.latest_verdict("ctr_fake")
        self.assertEqual(verdict["attempt"], 2)
        self.assertEqual(verdict["hard"], "pass")      # the final attempt passed the hard gate
        self.assertEqual(verdict["measured"], "pass")
        lesson = self.store.get_lesson(result["lesson_id"])
        self.assertIn("escalated", lesson["outcome"])
        self.assertNotIn("failed the hard gate", lesson["outcome"])
        self.assertIsNone(lesson["route_change"], lesson)


class TestBracketRecordIsComplete(LoopV03Base):
    """Fix pass Wave 2.8 (review M5, C-08): the matrix-pass anchor is applied
    before the cut, every round records who was scored how, and a dissent
    re-run refreshes the evidence the Lesson is derived from."""

    def test_a_matrix_failer_does_not_displace_a_passer_at_the_cut(self):
        for n in "abcd":
            self.markers[f"gen-{n}"] = f"m-{n}"
        scores = {"m-a": 9.5, "m-b": 8.0, "m-c": 7.0, "m-d": 6.5}
        # gen-a: the highest median, but one rubric row far below the
        # floor — the matrix fails it. It must not consume a slot that a
        # passer below it would have advanced into.
        low_row = {("m-a", "evidence"): _cell(1.0)}
        executors = {f"gen-{n}": FakeGen(f"gen-{n}", f"fam-{n}", f"m-{n}") for n in "abcd"}
        executors["judge-y"] = FakeJudge("judge-y", "fam-y", scores, overrides=low_row, repeat_overrides=True)
        executors["judge-z"] = FakeJudge("judge-z", "fam-z", scores, overrides=low_row, repeat_overrides=True)
        self.panel("gen-a", "gen-b", ["gen-c", "gen-d", "judge-y", "judge-z"], ["gen-a", "gen-b", "gen-c", "gen-d"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.store.get_candidate(result["winner"])["expert_id"], "gen-b")
        verdict = self.store.latest_verdict("ctr_fake")
        by_expert = {c["expert_id"]: c["id"] for c in self.store.candidates_for("ctr_fake")}
        rounds = verdict["judged"]["bracket"]["rounds"]
        # Four in; three passed; the fixed cut of two applies to the three
        # passers (3 -> 2), the failer is out by score: 4 -> 2 -> 1.
        self.assertEqual([(r["field_in"], r["field_out"], r["passed"]) for r in rounds], [(4, 2, 3), (2, 1, 2)])
        self.assertEqual(set(rounds[0]["eliminated"]), {by_expert["gen-a"], by_expert["gen-d"]})
        self.assertEqual(set(rounds[1]["scores"]), {by_expert["gen-b"], by_expert["gen-c"]})  # c reached round 2
        self.assertFalse(rounds[0]["scores"][by_expert["gen-a"]]["pass"])
        self.assertAlmostEqual(rounds[0]["scores"][by_expert["gen-d"]]["median_mean"], 6.5)
        # The judged block covers every candidate the matrix scored, with
        # the round it was last scored in.
        judged = verdict["judged"]
        self.assertEqual({judged[by_expert[e]]["round"] for e in ("gen-a", "gen-d")}, {1})
        self.assertEqual({judged[by_expert[e]]["round"] for e in ("gen-b", "gen-c")}, {2})
        self.assertFalse(judged[by_expert["gen-a"]]["pass"])
        note = next(n for n in (r["note"] or "" for r in self.store.ledger_rows())
                    if n.startswith("bracket round 1"))
        self.assertIn("field 4 -> 2", note)
        self.assertIn("passed=3", note)

    def test_a_dissent_rerun_that_anchors_refreshes_the_lessons_evidence(self):
        self.markers.update({"gen-a": "m-a", "gen-b": "m-b", "gen-c": "m-c"})
        base_eval = gates_mod.evaluate_candidate
        markers, eval_counts = self.markers, self.eval_counts

        def second_look_fails_b(store, contract, candidate, cwd):
            gr = base_eval(store, contract, candidate, cwd)
            if markers.get(candidate["expert_id"]) == "m-b" and eval_counts.get("m-b", 0) >= 2:
                # The re-run reads the sensor again and the metric fails.
                store.add_measurement(candidate["id"], "ops_bench", "ops_per_tick", 120.0, "<=", 8, False)
                return gates_mod.CandidateGateResult(
                    candidate_id=candidate["id"], provenance_ok=True, eliminated_at="measured",
                    hard=[], measured=gr.measured,
                )
            return gr

        gates_mod.evaluate_candidate = second_look_fails_b
        loop_mod.gates_mod.evaluate_candidate = second_look_fails_b
        dissent_cell = {"score": 1.0, "confidence": 0.9,
                        "evidence": "ops_per_tick reading disputed",
                        "rationale": "the ops_per_tick sensor looks misread"}
        scores = {"m-a": 8.0, "m-b": 7.0, "m-c": 9.0}
        executors = {
            "gen-a": FakeGen("gen-a", "fam-a", "m-a"),
            "gen-b": FakeGen("gen-b", "fam-b", "m-b"),
            "gen-c": FakeGen("gen-c", "fam-c", "m-c"),
            "judge-a": FakeJudge("judge-a", "fam-a", scores),
            "judge-b": FakeJudge("judge-b", "fam-b", scores),
            "judge-c": FakeJudge("judge-c", "fam-c", scores,
                                 overrides={("m-b", "test_agreement"): dissent_cell},
                                 repeat_overrides=True),
        }
        self.panel("gen-a", "gen-b", ["gen-c", "judge-a", "judge-b", "judge-c"], ["gen-a", "gen-b", "gen-c"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.store.get_candidate(result["winner"])["expert_id"], "gen-c")
        self.assertEqual(self.eval_counts["m-b"], 2)  # the dissent re-ran b's gates once
        b_cand = next(c for c in self.store.candidates_for("ctr_fake") if c["expert_id"] == "gen-b")
        verdict = self.store.latest_verdict("ctr_fake")
        self.assertEqual(verdict["measured"], "fail")
        self.assertIn(b_cand["id"], verdict["eliminated"])
        # The Lesson is derived from the re-run's measurement row, not
        # from the first pass that the dissent disputed.
        lesson = self.store.get_lesson(result["lesson_id"])
        self.assertIn("failed the measured gate", lesson["outcome"])
        self.assertEqual(lesson["route_change"]["demote"], "gen-b")
        self.assertIn(f"measurement:{b_cand['id']}:ops_per_tick", lesson["evidence"])
        # The record counts the re-run as part of round 1, not as a round
        # of its own: 3 in, b anchored out, 2 advanced, then the final.
        rounds = verdict["judged"]["bracket"]["rounds"]
        self.assertEqual([r["round"] for r in rounds], [1, 2])
        self.assertEqual((rounds[0]["field_in"], rounds[0]["field_out"]), (3, 2))


class TestNobodyGenerates(LoopV03Base):
    """Fix pass Wave 2.8 (review M2, M3): there is no re-dispatch — every
    seat is already in the contest — so when no seat produces a candidate
    the run escalates with that reason, and the ledger says what each seat
    did: refused is refused, unavailable is unavailable."""

    def test_refused_and_unavailable_seats_are_recorded_as_such_and_the_run_escalates(self):
        class Refuses(FakeGen):
            def generate(self, worktree, contract, context):
                return GenResult(status="refused", log="I cannot help with that.",
                                 tokens_in=20, tokens_out=5, cost_usd=None, request_ids=[],
                                 duration_ms=5, detail="I cannot help with that.")

        class Unreachable(FakeGen):
            def available(self):
                return (False, "binary not found on PATH")

        self.markers.update({"gen-a": "m-a", "gen-b": "m-b"})
        executors = {"gen-a": Refuses("gen-a", "fam-a", "m-a"),
                     "gen-b": Unreachable("gen-b", "fam-b", "m-b")}
        self.panel("gen-a", "gen-b", [], ["gen-a", "gen-b"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "escalated", result)
        self.assertIn("no seat produced a candidate", result["detail"])
        outcomes = {r["expert_id"]: r["outcome"] for r in self.store.ledger_rows() if r["candidate_id"]}
        self.assertEqual(outcomes, {"gen-a": "refused", "gen-b": "unavailable"})
        summary = {r["expert_id"]: r for r in self.store.ledger_summary()}
        self.assertEqual((summary["gen-a"]["dispatches"], summary["gen-a"]["refused"], summary["gen-a"]["unavailable"]), (1, 1, 0))
        self.assertEqual((summary["gen-b"]["dispatches"], summary["gen-b"]["refused"], summary["gen-b"]["unavailable"]), (1, 0, 1))
        self.assertNotIn("queue", summary)
        queue_rows = [r for r in self.store.ledger_rows() if r["expert_id"] == "queue"]
        self.assertEqual([r["outcome"] for r in queue_rows], ["escalated"])
        report = ledger_report(self.store)
        self.assertIn("gen-b: dispatches=1 completed=0 refused=0 timeout=0 unavailable=1 error=0", report)
        self.assertIn("queue: escalated — ", report)
        self.assertNotIn("queue: dispatches=", report)
        # Nobody won, nobody lost: no candidate was fielded.
        for eid in ("gen-a", "gen-b"):
            self.assertEqual(self.store.get_expert(eid)["losses"], 0)


class TestContractValidation(unittest.TestCase):
    """Fix pass Wave 2.10 (review 1B-18): a judged item's `min` is validated
    at contract load — a number on the 0–10 scale — never after the wave has
    spent tokens."""

    def test_a_non_numeric_min_is_refused_at_load(self):
        with self.assertRaises(ContractError) as caught:
            _contract(acceptance=[
                {"class": "hard", "cmd": "true", "expect": {"exit": 0}},
                {"class": "judged", "rubric": "validity_matrix_v1", "min": "six", "judges": 3},
            ])
        self.assertIn("min", str(caught.exception))

    def test_a_min_above_the_scale_is_refused_at_load(self):
        with self.assertRaises(ContractError):
            _contract(acceptance=[
                {"class": "hard", "cmd": "true", "expect": {"exit": 0}},
                {"class": "judged", "rubric": "validity_matrix_v1", "min": 11, "judges": 3},
            ])

    def test_a_numeric_min_is_kept_as_a_float(self):
        contract = _contract(acceptance=[
            {"class": "hard", "cmd": "true", "expect": {"exit": 0}},
            {"class": "judged", "rubric": "validity_matrix_v1", "min": 6, "judges": 3},
        ])
        judged = next(a for a in contract["acceptance"] if a["class"] == "judged")
        self.assertEqual(judged["min"], 6.0)


class TestProvenanceComesFromTheAdapter(LoopV03Base):
    """Fix pass Wave 2.10 (review 1D-18): the candidate row's provenance is
    what the adapter reported, not a literal the store writes for every row
    — so the provenance gate checks a fact."""

    def test_the_rows_carry_the_adapters_provenance(self):
        class HandsInSomebodyElsesWork(FakeGen):
            def generate(self, worktree, contract, context):
                gen = super().generate(worktree, contract, context)
                gen.provenance = "human"
                return gen

        self.markers.update({"gen-a": "m-a", "gen-b": "m-b"})
        executors = {"gen-a": FakeGen("gen-a", "fam-a", "m-a"),
                     "gen-b": HandsInSomebodyElsesWork("gen-b", "fam-b", "m-b"),
                     "judge-z": FakeJudge("judge-z", "fam-z", {"m-a": 9.0, "m-b": 8.0})}
        self.panel("gen-a", "gen-b", ["judge-z"], ["gen-a", "gen-b"])
        self.assertEqual(GenResult(status="completed").provenance, "generated")
        loop_mod.run_contract(self.store, _contract(), "repo://fake", executors,
                              human_decider=lambda info: "approved")
        rows = {c["expert_id"]: c for c in self.store.candidates_for("ctr_fake")}
        self.assertEqual(rows["gen-a"]["provenance"], "generated")
        self.assertEqual(rows["gen-b"]["provenance"], "human")
        self.assertTrue(gates_mod.check_provenance(rows["gen-a"]))
        self.assertFalse(gates_mod.check_provenance(rows["gen-b"]))  # the real gate would eliminate it


class TestUnscoredCandidateEarnsNothing(LoopV03Base):
    """Phase 3 live-run finding (2026-11-07, the two-seat Phase A matrix):
    when a peer produces no valid cell for a candidate, that candidate is
    recorded as unscored — no validity points (not 0.0), no invented rows —
    and the bracket round names the peer that produced no cell."""

    def test_two_seats_where_one_judge_is_silent(self):
        class SilentJudge(FakeSeat):
            def judge(self, packet, rubric):
                self.calls += 1
                return None

        self.markers.update({"seat-a": "m-a", "seat-b": "m-b"})
        executors = {
            "seat-a": FakeSeat("seat-a", "fam-a", "m-a", {"m-a": 9.0, "m-b": 8.0}),
            "seat-b": SilentJudge("seat-b", "fam-b", "m-b", {}),
        }
        self.panel("seat-a", "seat-b", [], ["seat-a", "seat-b"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        # seat-a judged seat-b's candidate (8.0 -> passes); seat-b judged
        # nothing, so seat-a's own candidate has no cell from anyone.
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.store.get_candidate(result["winner"])["expert_id"], "seat-b")
        verdict = self.store.latest_verdict("ctr_fake")
        a_cand = next(c for c in self.store.candidates_for("ctr_fake") if c["expert_id"] == "seat-a")
        entry = verdict["judged"][a_cand["id"]]
        self.assertIsNone(entry["median_mean"])
        self.assertFalse(entry["pass"])
        self.assertFalse(entry["scored"])
        rounds = verdict["judged"]["bracket"]["rounds"]
        self.assertEqual(rounds[0]["peers_without_cells"], ["seat-b"])
        note = next(n for n in (r["note"] or "" for r in self.store.ledger_rows())
                    if n.startswith("bracket round 1"))
        self.assertIn("peers_without_cells=['seat-b']", note)
        summary = {r["expert_id"]: r for r in self.store.ledger_summary()}
        self.assertIsNone(summary["seat-a"]["validity_points"])        # nothing invented
        self.assertAlmostEqual(summary["seat-b"]["validity_points"], 8.0)  # the one recorded median
        self.assertNotIn("validity_points=0.0", ledger_report(self.store))


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


class TestGateErrorEliminatesOneCandidate(LoopV03Base):
    """Fix spec G7 tail (round-4 contest-core F3): a candidate whose
    gates raise GateError — a sensor that produced unparseable output —
    is eliminated with the reason recorded; the error never propagates
    out of run_contract and strands the contract at "open"."""

    def setUp(self):
        super().setUp()
        base_eval = gates_mod.evaluate_candidate
        self._base_eval = base_eval

        def eval_with_gate_error(store, contract, candidate, cwd):
            if candidate["expert_id"] in self._gate_error_experts:
                raise GateError("sensor ops_bench produced unparseable output")
            return base_eval(store, contract, candidate, cwd)

        self._gate_error_experts = {"gen-b"}
        gates_mod.evaluate_candidate = eval_with_gate_error
        loop_mod.gates_mod.evaluate_candidate = eval_with_gate_error
        self.markers.update({"gen-a": "m-a", "gen-b": "m-b"})

    def _executors(self):
        return {
            "gen-a": FakeGen("gen-a", "fam-a", "m-a"),
            "gen-b": FakeGen("gen-b", "fam-b", "m-b"),
            "judge-z": FakeJudge("judge-z", "fam-z", {"m-a": 9.0, "m-b": 8.0}),
        }

    def test_one_candidates_gate_error_eliminates_only_it(self):
        self.panel("gen-a", "gen-b", ["judge-z"], ["gen-a", "gen-b"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", self._executors(),
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "completed", result)
        verdict = self.store.latest_verdict("ctr_fake")
        losers = [self.store.get_candidate(cid) for cid in verdict["eliminated"]]
        self.assertIn("gen-b", [c["expert_id"] for c in losers])
        notes = [r["note"] or "" for r in self.store.ledger_rows()]
        self.assertTrue(
            any("gate error eliminated candidate" in n
                and "unparseable" in n for n in notes), notes)
        self.assertEqual(
            self.store.get_contract("ctr_fake")["status"], "completed")

    def test_every_candidate_gate_erroring_escalates_instead_of_raising(self):
        self._gate_error_experts = {"gen-a", "gen-b"}
        self.panel("gen-a", "gen-b", ["judge-z"], ["gen-a", "gen-b"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", self._executors(),
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "escalated", result)
        self.assertIn("retry cap", result["detail"])
        self.assertEqual(
            self.store.get_contract("ctr_fake")["status"], "escalated")


class TestSlotGuardFiresBeforeTheWave(LoopV03Base):
    """Fix spec G9 (round-3 contest-core F8): more contestants than
    slots escalates with the capacity reason BEFORE any dispatch —
    the old post-wave check could never fire, and the 27th contestant
    died on an IndexError mid-wave."""

    def test_27_contestants_escalate_with_no_dispatch(self):
        executors = {
            f"gen-{i:02d}": FakeGen(f"gen-{i:02d}", f"fam-{i:02d}", f"m-{i:02d}")
            for i in range(27)
        }
        ids = sorted(executors)
        self.panel(ids[0], ids[1], ids[2:], ids)
        contract = _contract(acceptance=[
            {"class": "hard", "cmd": "true", "expect": {"exit": 0}},
        ])
        result = loop_mod.run_contract(
            self.store, contract, "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "escalated", result)
        self.assertIn("slot capacity", result["detail"])
        self.assertEqual(self.store.candidates_for("ctr_fake"), [])


class TestEscalatedMatrixRunCreditsValidity(LoopV03Base):
    """Fix spec G8 (round-3 contest-core F7): an escalated matrix run
    credits the validity it measured — _escalate passes the scored
    candidate ids to _record_validity instead of an empty list."""

    def test_no_pass_escalation_still_records_medians(self):
        self.markers.update({"gen-a": "m-a", "gen-b": "m-b"})
        executors = {
            "gen-a": FakeGen("gen-a", "fam-a", "m-a"),
            "gen-b": FakeGen("gen-b", "fam-b", "m-b"),
            "judge-y": FakeJudge("judge-y", "fam-y", {"m-a": 3.0, "m-b": 3.0}),
            "judge-z": FakeJudge("judge-z", "fam-z", {"m-a": 3.0, "m-b": 3.0}),
        }
        self.panel("gen-a", "gen-b", ["judge-y", "judge-z"], ["gen-a", "gen-b"])
        result = loop_mod.run_contract(
            self.store, _contract(), "repo://fake", executors,
            human_decider=lambda info: "approved",
        )
        self.assertEqual(result["status"], "escalated", result)
        self.assertIn("peer matrix passed no candidate", result["detail"])
        self.assertEqual(self.store.get_expert("gen-a")["validity_points"], 3.0)
        self.assertEqual(self.store.get_expert("gen-b")["validity_points"], 3.0)


if __name__ == "__main__":
    unittest.main()
