"""Tests for executors, dev_solver, and worktrees."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from unittest.mock import patch

from hybrid_critic_loop.executors import (
    AGY_UNVERIFIED_DETAIL,
    BEAM_DETAIL,
    JUDGE_INSTRUCTION,
    JUDGE_INSTRUCTION_V2,
    AgyExecutor,
    BeamExecutor,
    ClaudeExecutor,
    CodexExecutor,
    GrokExecutor,
    KimiExecutor,
    LocalJudgeExecutor,
    LocalSolverExecutor,
    QwenExecutor,
    default_executors,
)
from hybrid_critic_loop.judges import build_packet
from hybrid_critic_loop.worktrees import WorktreeManager

STUB = '''class SpeedSampler:
    """Stub - to be implemented."""
    def __init__(self, capacity=120):
        pass
    def push(self, value):
        pass
    def average(self):
        return 0.0
'''


def load_sampler(module_path: Path):
    spec = importlib.util.spec_from_file_location("sampler_mod", str(module_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_sampler_semantics(testcase, cls):
    # empty -> 0.0
    s = cls()
    testcase.assertEqual(s.average(), 0.0)
    # basic average
    s.push(1.0)
    s.push(2.0)
    s.push(3.0)
    testcase.assertAlmostEqual(s.average(), 2.0)
    # capacity / wraparound: capacity 3, push 1..5 -> keeps 3,4,5 avg 4
    s2 = cls(capacity=3)
    for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
        s2.push(v)
    testcase.assertAlmostEqual(s2.average(), 4.0)
    # len / count if available
    try:
        testcase.assertEqual(len(s2), 3)
    except Exception:
        pass
    # exact capacity boundary
    s3 = cls(capacity=2)
    s3.push(10.0)
    s3.push(20.0)
    testcase.assertAlmostEqual(s3.average(), 15.0)
    s3.push(30.0)
    testcase.assertAlmostEqual(s3.average(), 25.0)


class TestDevSolver(unittest.TestCase):
    def _run_solver(self, strategy: str):
        solver = REPO_ROOT / "tools" / "dev_solver.py"
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            target = td / "speed_sampler.py"
            target.write_text(STUB, encoding="utf-8")
            payload = json.dumps({"contract": {}, "context": ""})
            proc = subprocess.run(
                [sys.executable, str(solver), "--strategy", strategy],
                cwd=str(td), input=payload, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 0, f"solver failed: {proc.stdout} {proc.stderr}")
            summary = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertEqual(summary["strategy"], strategy)
            self.assertEqual(summary["class"], "SpeedSampler")
            mod = load_sampler(target)
            check_sampler_semantics(self, mod.SpeedSampler)

    def test_naive(self):
        self._run_solver("naive")

    def test_ring(self):
        self._run_solver("ring")

    def test_balanced(self):
        self._run_solver("balanced")

    def test_balanced_is_a_distinct_shape(self):
        solver = REPO_ROOT / "tools" / "dev_solver.py"
        outputs = {}
        for strategy in ("naive", "ring", "balanced"):
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                (td / "speed_sampler.py").write_text(STUB, encoding="utf-8")
                proc = subprocess.run(
                    [sys.executable, str(solver), "--strategy", strategy],
                    cwd=str(td), input=json.dumps({"contract": {}, "context": ""}),
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                outputs[strategy] = (td / "speed_sampler.py").read_text(encoding="utf-8")
        self.assertNotEqual(outputs["balanced"], outputs["naive"])
        self.assertNotEqual(outputs["balanced"], outputs["ring"])
        self.assertIn("deque", outputs["balanced"])


class TestLocalSolverExecutor(unittest.TestCase):
    def _make_worktree_dir(self, td: Path) -> Path:
        target = td / "speed_sampler.py"
        target.write_text(STUB, encoding="utf-8")
        return td

    def test_lesson_conditioning_ring(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            self._make_worktree_dir(td)
            ex = LocalSolverExecutor(expert_id="local-naive", profile="naive")
            result = ex.generate(td, {}, context="lesson: ops_per_tick too high, reduce alloc")
            self.assertEqual(result.status, "completed", result.log + " " + result.detail)
            self.assertIn("lesson-conditioned", result.detail)
            # Ring implementation should have been written and pass semantics
            mod = load_sampler(td / "speed_sampler.py")
            check_sampler_semantics(self, mod.SpeedSampler)

    def test_profile_default_naive(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            self._make_worktree_dir(td)
            ex = LocalSolverExecutor(expert_id="local-naive", profile="naive")
            result = ex.generate(td, {}, context="")
            self.assertEqual(result.status, "completed", result.log + " " + result.detail)
            self.assertIn("profile default", result.detail)
            mod = load_sampler(td / "speed_sampler.py")
            check_sampler_semantics(self, mod.SpeedSampler)

    def test_profile_default_ring(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            self._make_worktree_dir(td)
            ex = LocalSolverExecutor(expert_id="local-ring", profile="ring")
            result = ex.generate(td, {}, context="")
            self.assertEqual(result.status, "completed", result.log + " " + result.detail)
            self.assertIn("profile default", result.detail)
            self.assertIn("ring", result.detail)

    def test_profile_default_balanced(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            self._make_worktree_dir(td)
            ex = LocalSolverExecutor(expert_id="local-balanced", profile="balanced")
            result = ex.generate(td, {}, context="")
            self.assertEqual(result.status, "completed", result.log + " " + result.detail)
            self.assertIn("profile default", result.detail)
            self.assertIn("balanced", result.detail)
            mod = load_sampler(td / "speed_sampler.py")
            check_sampler_semantics(self, mod.SpeedSampler)

    def test_lesson_conditioning_still_ring_for_balanced_profile(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            self._make_worktree_dir(td)
            ex = LocalSolverExecutor(expert_id="local-balanced", profile="balanced")
            result = ex.generate(td, {}, context="lesson: ops_per_tick too high, reduce alloc")
            self.assertEqual(result.status, "completed", result.log + " " + result.detail)
            self.assertIn("lesson-conditioned", result.detail)
            self.assertIn("strategy=ring", result.detail)


class TestWorktreeManager(unittest.TestCase):
    def test_create_diff_commit_merge(self):
        with tempfile.TemporaryDirectory() as td_str:
            repo = Path(td_str) / "repo"
            repo.mkdir()
            def git(*args):
                r = subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"git {args}: {r.stderr}")
                return r
            subprocess.run(["git", "init", str(repo)], capture_output=True, text=True, check=True)
            git("-c", "user.email=test@test", "-c", "user.name=Test", "config", "user.email", "test@test")
            # simpler: set local config
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
            git("add", "-A")
            git("commit", "-m", "init")

            mgr = WorktreeManager(repo)
            head = mgr.baseline_hash()
            self.assertTrue(len(head) >= 7)

            wt = mgr.create("contract1", "a")
            self.assertTrue(wt.exists())
            self.assertTrue((wt / "hello.txt").exists())
            (wt / "new.txt").write_text("new content\n", encoding="utf-8")
            (wt / "hello.txt").write_text("hello modified\n", encoding="utf-8")

            d = mgr.diff(wt)
            self.assertIn("new.txt", d)
            self.assertIn("new content", d)

            sha = mgr.commit_all(wt, "work in worktree")
            self.assertTrue(len(sha) >= 7)

            merged = mgr.merge_into_main(wt)
            self.assertTrue(len(merged) >= 7)
            self.assertTrue((repo / "new.txt").exists())
            self.assertEqual((repo / "new.txt").read_text(encoding="utf-8"), "new content\n")

            # Cleanup worktree
            mgr.remove(wt)

    def test_keep_as_ref(self):
        with tempfile.TemporaryDirectory() as td_str:
            repo = Path(td_str) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", str(repo)], capture_output=True, text=True, check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "a.txt").write_text("a\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True)
            mgr = WorktreeManager(repo)
            wt = mgr.create("c2", "loser")
            (wt / "b.txt").write_text("b\n", encoding="utf-8")
            mgr.commit_all(wt, "loser work")
            branch = mgr.keep_as_ref("c2", "loser")
            self.assertEqual(branch, "critic/c2/loser")
            self.assertFalse(wt.exists())
            r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", branch], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


class TestExternalExecutors(unittest.TestCase):
    def test_codex_available_returns_tuple(self):
        ex = CodexExecutor()
        result = ex.available()
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], bool)
        self.assertIsInstance(result[1], str)

    def test_grok_unavailable_when_missing(self):
        if shutil.which("grok"):
            self.skipTest("grok present on PATH")
        ex = GrokExecutor()
        with tempfile.TemporaryDirectory() as td:
            result = ex.generate(Path(td), {"task": "x"}, "")
            self.assertEqual(result.status, "unavailable")
            self.assertIn("grok binary not found", result.detail)


def _fake_which(present):
    def fake(name, *args, **kwargs):
        return f"/fake/bin/{name}" if name in present else None
    return fake


class TestJudgeInstructionV2(unittest.TestCase):
    def test_v2_mentions_confidence_and_rationale(self):
        self.assertIn("confidence", JUDGE_INSTRUCTION_V2)
        self.assertIn("rationale", JUDGE_INSTRUCTION_V2)
        self.assertIn("evidence", JUDGE_INSTRUCTION_V2)
        self.assertIn("score", JUDGE_INSTRUCTION_V2)

    def test_v2_supports_more_labels_than_a_b(self):
        self.assertIn("one key per candidate label", JUDGE_INSTRUCTION_V2)

    def test_old_name_is_alias(self):
        self.assertEqual(JUDGE_INSTRUCTION, JUDGE_INSTRUCTION_V2)


class TestNewCliAdapters(unittest.TestCase):
    ADAPTERS = [
        (AgyExecutor, "agy", "google"),
        (ClaudeExecutor, "claude", "anthropic"),
        (QwenExecutor, "qwen", "alibaba"),
        (KimiExecutor, "kimi", "moonshot"),
    ]

    def test_available_reflects_which(self):
        for cls, binary, family in self.ADAPTERS:
            ex = cls()
            self.assertEqual(ex.family, family)
            with patch("shutil.which", _fake_which({binary})):
                ok, detail = ex.available()
                self.assertTrue(ok, f"{binary} should be available when which() finds it")
                self.assertEqual(detail, f"/fake/bin/{binary}")
            with patch("shutil.which", _fake_which(set())):
                ok, detail = ex.available()
                self.assertFalse(ok, f"{binary} should be unavailable when which() misses it")
                self.assertIn("not found", detail)

    def test_generate_unavailable_when_binary_missing(self):
        for cls, binary, _family in self.ADAPTERS:
            if cls is AgyExecutor:
                continue  # agy has its own unverified-detail behaviour, tested below
            ex = cls()
            with patch("shutil.which", _fake_which(set())):
                with tempfile.TemporaryDirectory() as td:
                    result = ex.generate(Path(td), {"task": "x"}, "")
                self.assertEqual(result.status, "unavailable")
                self.assertIn(binary, result.detail)

    def test_judge_none_when_binary_missing(self):
        for cls, _binary, _family in self.ADAPTERS:
            ex = cls()
            with patch("shutil.which", _fake_which(set())):
                self.assertIsNone(ex.judge("packet", {"items": ["evidence"]}))

    def test_agy_generate_unverified_even_when_binary_present(self):
        ex = AgyExecutor()
        with patch("shutil.which", _fake_which({"agy"})):
            ok, _ = ex.available()
            self.assertTrue(ok)  # presence is honest...
            with tempfile.TemporaryDirectory() as td:
                result = ex.generate(Path(td), {"task": "x"}, "")
            # ...but no headless invocation was ever verified, so no flags are invented
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(result.detail, AGY_UNVERIFIED_DETAIL)
            self.assertIn("agy headless invocation not verified", result.detail)
            self.assertIsNone(ex.judge("packet", {"items": ["evidence"]}))


class TestBeamExecutor(unittest.TestCase):
    def test_always_unavailable_with_nous_detail(self):
        ex = BeamExecutor()
        self.assertEqual(ex.expert_id, "beam")
        self.assertEqual(ex.family, "reflection")
        with patch("shutil.which", _fake_which({"beam", "claude", "codex"})):
            ok, detail = ex.available()
            self.assertFalse(ok)
            self.assertEqual(detail, BEAM_DETAIL)
            self.assertIn("Nous", detail)
            self.assertIn("no local CLI", detail)
            with tempfile.TemporaryDirectory() as td:
                result = ex.generate(Path(td), {"task": "x"}, "")
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(result.detail, BEAM_DETAIL)
            self.assertIsNone(ex.judge("packet", {"items": ["evidence"]}))


class TestDefaultExecutors(unittest.TestCase):
    def test_eight_family_panel_ids_with_correct_families(self):
        # Owner decision (v0.3): the target panel is eight families.
        # qwen-local is the Ollama-backed alibaba seat; qwen-cli is the
        # same family's CLI adapter; deepseek is the API adapter.
        executors = default_executors()
        self.assertEqual(
            set(executors),
            {"codex", "grok", "agy", "claude", "qwen-local", "qwen-cli",
             "kimi", "deepseek", "beam"},
        )
        expected_families = {
            "codex": "openai",
            "grok": "xai",
            "agy": "google",
            "claude": "anthropic",
            "qwen-local": "alibaba",
            "qwen-cli": "alibaba",
            "kimi": "moonshot",
            "deepseek": "deepseek",
            "beam": "reflection",
        }
        for expert_id, family in expected_families.items():
            self.assertEqual(executors[expert_id].expert_id, expert_id)
            self.assertEqual(executors[expert_id].family, family)
        for adapter in executors.values():
            ok, detail = adapter.available()
            self.assertIsInstance(ok, bool)
            self.assertIsInstance(detail, str)


PANEL_RUBRIC = {
    "id": "panel_v2",
    "version": 2,
    "scale": 10,
    "items": ["test_agreement", "evidence", "question_fidelity", "completeness"],
}


def _sample_packet() -> str:
    summary = (
        "Candidate A: hard gates PASS; measurements: ops_per_tick=1.0 (<= 8: pass); measured gates PASS\n"
        "Candidate B: hard gates FAIL; measured gates FAIL"
    )
    diff_a = (
        "+class SpeedSampler:  # see speed_sampler.py:42 and steering_sampler.py:17\n"
        "+    def push(self, value):  # ring per ops_bench.py:9\n"
        + ("+    # padded line to give the diff some size\n" * 12)
    )
    diff_b = "+class SpeedSampler:\n+    pass\n"
    return build_packet({"A": diff_a, "B": diff_b}, summary, PANEL_RUBRIC)


class TestLocalJudgeExecutor(unittest.TestCase):
    def test_all_rows_for_every_label(self):
        packet = _sample_packet()
        for style in ("lenient", "strict", "evidence-first"):
            ex = LocalJudgeExecutor(f"judge-{style}", f"local-{style}", style)
            result = ex.judge(packet, PANEL_RUBRIC)
            self.assertIsNotNone(result)
            self.assertEqual(set(result), {"A", "B"})
            for label in ("A", "B"):
                items = result[label]["items"]
                self.assertEqual(set(items), set(PANEL_RUBRIC["items"]))
                for item in items.values():
                    self.assertGreaterEqual(item["score"], 0)
                    self.assertLessEqual(item["score"], 10)
                    self.assertGreaterEqual(item["confidence"], 0.6)
                    self.assertLessEqual(item["confidence"], 0.9)
                    self.assertTrue(item["evidence"].strip())
                    self.assertTrue(item["rationale"].strip())
                    self.assertNotIn("\n", item["rationale"])

    def test_test_agreement_drops_for_fail_label(self):
        ex = LocalJudgeExecutor("judge-ev", "local-evidence", "evidence-first")
        result = ex.judge(_sample_packet(), PANEL_RUBRIC)
        self.assertGreaterEqual(result["A"]["items"]["test_agreement"]["score"], 9)
        self.assertLessEqual(result["B"]["items"]["test_agreement"]["score"], 3)
        self.assertLess(
            result["B"]["items"]["test_agreement"]["score"],
            result["A"]["items"]["test_agreement"]["score"],
        )

    def test_evidence_row_rewards_citations(self):
        ex = LocalJudgeExecutor("judge-ev", "local-evidence", "evidence-first")
        result = ex.judge(_sample_packet(), PANEL_RUBRIC)
        self.assertGreater(
            result["A"]["items"]["evidence"]["score"],
            result["B"]["items"]["evidence"]["score"],
        )

    def test_deterministic_across_calls(self):
        packet = _sample_packet()
        ex = LocalJudgeExecutor("judge-strict", "local-strict", "strict")
        first = ex.judge(packet, PANEL_RUBRIC)
        second = ex.judge(packet, PANEL_RUBRIC)
        self.assertEqual(first, second)
        twin = LocalJudgeExecutor("judge-strict-2", "local-strict", "strict")
        self.assertEqual(first, twin.judge(packet, PANEL_RUBRIC))

    def test_style_offsets_and_validation(self):
        packet = _sample_packet()
        lenient = LocalJudgeExecutor("j1", "local-lenient", "lenient").judge(packet, PANEL_RUBRIC)
        strict = LocalJudgeExecutor("j2", "local-strict", "strict").judge(packet, PANEL_RUBRIC)
        self.assertGreater(
            lenient["A"]["items"]["completeness"]["score"],
            strict["A"]["items"]["completeness"]["score"],
        )
        self.assertNotEqual(
            lenient["A"]["items"]["completeness"]["confidence"],
            strict["A"]["items"]["completeness"]["confidence"],
        )
        with self.assertRaises(ValueError):
            LocalJudgeExecutor("j3", "local-x", "random")

    def test_generate_is_unavailable(self):
        ex = LocalJudgeExecutor("judge-lenient", "local-lenient", "lenient")
        ok, _ = ex.available()
        self.assertTrue(ok)
        with tempfile.TemporaryDirectory() as td:
            result = ex.generate(Path(td), {"task": "x"}, "")
        self.assertEqual(result.status, "unavailable")


class TestJudgeVerdictParsing(unittest.TestCase):
    """Judge output becomes a verdict only when it has the verdict shape;
    CLI envelopes are looked through (fix pass Wave 2.5, review H2)."""

    VERDICT = {"A": {"items": {"evidence": {"score": 8, "confidence": 0.9, "evidence": "p", "rationale": "r"}}},
               "B": {"items": {"evidence": {"score": 3, "confidence": 0.7, "evidence": "q", "rationale": "s"}}}}

    def test_codex_jsonl_stream_yields_the_agent_message_verdict(self):
        from hybrid_critic_loop.executors import _extract_judge_verdict

        stream = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "t1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message",
                                                           "text": "Here you go:\n" + json.dumps(self.VERDICT)}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}),
        ])
        self.assertEqual(_extract_judge_verdict(stream), self.VERDICT)

    def test_claude_envelope_yields_the_result_verdict(self):
        from hybrid_critic_loop.executors import _extract_judge_verdict

        envelope = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                               "result": json.dumps(self.VERDICT), "total_cost_usd": 0.01,
                               "usage": {"input_tokens": 900, "output_tokens": 40}})
        self.assertEqual(_extract_judge_verdict(envelope), self.VERDICT)

    def test_plain_text_and_prose_wrapped_verdicts(self):
        from hybrid_critic_loop.executors import _extract_judge_verdict

        self.assertEqual(_extract_judge_verdict(json.dumps(self.VERDICT)), self.VERDICT)
        self.assertEqual(_extract_judge_verdict("Sure. " + json.dumps(self.VERDICT) + " Done."), self.VERDICT)

    def test_wrapper_without_a_verdict_is_none_not_the_wrapper(self):
        from hybrid_critic_loop.executors import _extract_judge_verdict, _extract_first_json_object

        stream = json.dumps({"type": "thread.started", "thread_id": "t1"}) + "\n" + json.dumps({"type": "turn.completed"})
        self.assertIsNone(_extract_judge_verdict(stream))
        self.assertEqual(_extract_first_json_object(stream)["type"], "thread.started")  # the old behaviour, kept for usage parsing
        self.assertIsNone(_extract_judge_verdict(""))
        self.assertIsNone(_extract_judge_verdict("no json here"))

    def test_can_judge_declarations(self):
        from hybrid_critic_loop.executors import (
            AgyExecutor, BeamExecutor, ClaudeExecutor, CodexExecutor, GrokExecutor,
            LocalJudgeExecutor, LocalSolverExecutor,
        )

        self.assertTrue(CodexExecutor().can_judge())
        self.assertTrue(GrokExecutor().can_judge())
        self.assertTrue(ClaudeExecutor().can_judge())
        self.assertTrue(LocalJudgeExecutor("j", "fam", "strict").can_judge())
        self.assertFalse(AgyExecutor().can_judge())
        self.assertFalse(BeamExecutor().can_judge())
        self.assertFalse(LocalSolverExecutor("s", "naive").can_judge())


def _fake_run(result):
    """A stand-in for shellrun.run_command returning one scripted result."""
    def fake(cmd, cwd=None, timeout_s=900):
        fake.calls.append(cmd)
        return result
    fake.calls = []
    return fake


class TestCliResultClassification(unittest.TestCase):
    """Fix pass Wave 2.9 (review M8 / 1D-05, 1D-09): the exit code decides a
    CLI seat's outcome; a refusal marker explains a failure and never
    overrides a success; a run the OS refused to start names its cause."""

    ADAPTERS = [(CodexExecutor, "codex"), (GrokExecutor, "grok"), (ClaudeExecutor, "claude")]

    def _run(self, cls, binary, result):
        from hybrid_critic_loop.shellrun import RunResult  # noqa: F401 - shape under test

        fake = _fake_run(result)
        with patch("shutil.which", _fake_which({binary})), \
                patch("hybrid_critic_loop.executors.run_command", fake):
            with tempfile.TemporaryDirectory() as td:
                gen = cls().generate(Path(td), {"task": "x"}, "")
        self.assertEqual(len(fake.calls), 1, "the CLI must be invoked exactly once")
        return gen

    @staticmethod
    def _result(binary, exit_code, stdout="", stderr="", timed_out=False):
        from hybrid_critic_loop.shellrun import RunResult

        return RunResult(cmd=binary, exit_code=exit_code, stdout=stdout, stderr=stderr,
                         duration_ms=5, timed_out=timed_out)

    def test_a_successful_run_that_mentions_a_marker_is_completed(self):
        stream = ("Reconnecting to the session...\n"
                  + json.dumps({"type": "turn.completed",
                                "usage": {"input_tokens": 12, "output_tokens": 3}}) + "\n")
        for cls, binary in self.ADAPTERS:
            gen = self._run(cls, binary, self._result(binary, 0, stdout=stream))
            self.assertEqual(gen.status, "completed", (binary, gen.detail))
            self.assertIn("Reconnecting", gen.detail)  # noted, not decisive
            self.assertEqual((gen.tokens_in, gen.tokens_out), (12, 3), binary)

    def test_a_failed_run_with_a_marker_is_refused(self):
        for cls, binary in self.ADAPTERS:
            gen = self._run(cls, binary, self._result(binary, 1, stderr="Not logged in. Run login first.\n"))
            self.assertEqual(gen.status, "refused", (binary, gen.detail))
            self.assertIn("Not logged in", gen.detail)

    def test_a_failed_run_without_a_marker_is_an_error_naming_the_exit(self):
        gen = self._run(CodexExecutor, "codex", self._result("codex", 2, stderr="usage: codex exec ..."))
        self.assertEqual(gen.status, "error")
        self.assertIn("exited 2", gen.detail)

    def test_a_run_the_os_refused_to_start_names_the_cause(self):
        reason = ("E2BIG: a single argument of 200000 bytes exceeds the 131072-byte limit; "
                  "the command was not run")
        gen = self._run(CodexExecutor, "codex", self._result("codex", None, stderr=reason))
        self.assertEqual(gen.status, "error")
        self.assertIn("E2BIG", gen.detail)
        self.assertNotIn("exited None", gen.detail)

    def test_a_timeout_is_a_timeout_and_a_hung_refusal_stays_refused(self):
        gen = self._run(GrokExecutor, "grok", self._result("grok", None, stdout="working...", timed_out=True))
        self.assertEqual(gen.status, "timeout", gen.detail)
        gen = self._run(GrokExecutor, "grok", self._result("grok", None, stdout="Not logged in\n", timed_out=True))
        self.assertEqual(gen.status, "refused", gen.detail)
        self.assertIn("hung", gen.detail)


class TestUsageParsing(unittest.TestCase):
    """Fix pass Wave 2.9 (review M8 / 1D-06): token counts come from whole
    JSON — a JSONL event line or one JSON document — never from prose that
    happens to quote a usage-shaped object."""

    def test_prose_quoting_a_usage_object_counts_nothing(self):
        from hybrid_critic_loop.executors import _parse_jsonl_usage

        prose = ('I noticed the earlier run reported {"usage": {"input_tokens": 999, '
                 '"output_tokens": 888}} which looks high.\n')
        self.assertEqual(_parse_jsonl_usage(prose)[:2], (None, None))

    def test_jsonl_event_lines_and_a_whole_document_count(self):
        from hybrid_critic_loop.executors import _parse_jsonl_usage

        stream = (json.dumps({"type": "thread.started", "thread_id": "t1"}) + "\n"
                  + json.dumps({"type": "turn.completed",
                                "usage": {"input_tokens": 12, "output_tokens": 3}}) + "\n")
        self.assertEqual(_parse_jsonl_usage(stream)[:2], (12, 3))
        # One pretty-printed document (the claude --output-format json envelope).
        envelope = json.dumps({"result": "done",
                               "usage": {"input_tokens": 7, "output_tokens": 2}}, indent=2)
        self.assertEqual(_parse_jsonl_usage(envelope)[:2], (7, 2))

    def test_cost_is_read_only_when_the_provider_reports_it(self):
        # Fix pass Wave 2.10 (review 1D-16): claude's envelope carries
        # total_cost_usd; it is read as reported. Nothing else invents one.
        from hybrid_critic_loop.executors import _parse_jsonl_usage

        envelope = json.dumps({"result": "done", "total_cost_usd": 0.0123,
                               "usage": {"input_tokens": 900, "output_tokens": 40}})
        tokens_in, tokens_out, _rids, cost = _parse_jsonl_usage(envelope)
        self.assertEqual((tokens_in, tokens_out), (900, 40))
        self.assertAlmostEqual(cost, 0.0123)
        stream = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 3}})
        self.assertIsNone(_parse_jsonl_usage(stream)[3])
        prose = 'the bill said {"total_cost_usd": 9.99} but who knows\n'
        self.assertIsNone(_parse_jsonl_usage(prose)[3])


class TestRunCommandArgumentLimit(unittest.TestCase):
    """Fix pass Wave 2.9 (review M8 / 1D-09): an argument above the kernel's
    per-argument limit is refused before execve, with the reason, instead of
    surfacing as a bare OSError the adapters cannot explain."""

    def test_oversize_argument_is_refused_before_running(self):
        from hybrid_critic_loop.shellrun import MAX_ARG_STRLEN, run_command

        result = run_command("echo " + "x" * (MAX_ARG_STRLEN + 1))
        self.assertIsNone(result.exit_code)
        self.assertFalse(result.timed_out)
        self.assertIn("E2BIG", result.stderr)
        self.assertIn(str(MAX_ARG_STRLEN), result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_normal_argument_runs(self):
        from hybrid_critic_loop.shellrun import run_command

        result = run_command("echo hello")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("hello", result.stdout)


if __name__ == "__main__":
    unittest.main()
