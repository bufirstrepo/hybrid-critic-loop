"""Worker D tests: plugin registration against a FakeCtx.

Registration must work with no loop.py import at module level and no
Hermes runtime — a recording fake ctx is the whole surface.  The data dir
is redirected to a temp dir via HCL_DATA_DIR so no real state is touched.
"""

import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hybrid_critic_loop import plugin


class FakeCtx:
    """Records every registration call plugin.register makes."""

    def __init__(self):
        self.tools = {}
        self.commands = {}
        self.cli_commands = {}
        self.hooks = {}

    def register_tool(self, name=None, toolset=None, schema=None, handler=None):
        self.tools[name] = {
            "toolset": toolset, "schema": schema, "handler": handler,
        }

    def register_command(self, name, handler, description):
        self.commands[name] = {"handler": handler, "description": description}

    def register_cli_command(self, name, help, setup_fn, handler_fn):
        self.cli_commands[name] = {
            "help": help, "setup_fn": setup_fn, "handler_fn": handler_fn,
        }

    def register_hook(self, name, callback):
        self.hooks[name] = callback


class FakeCtxNoCli:
    """Older ctx surface without register_cli_command."""

    def __init__(self):
        self.tools = {}
        self.commands = {}
        self.hooks = {}

    def register_tool(self, name=None, toolset=None, schema=None, handler=None):
        self.tools[name] = {"handler": handler, "schema": schema}

    def register_command(self, name, handler, description):
        self.commands[name] = {"handler": handler, "description": description}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


class TestRegistration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ, {"HCL_DATA_DIR": self._tmp.name}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        plugin.REGISTRATION_NOTES.clear()

    def test_register_tools_command_cli_hooks(self):
        ctx = FakeCtx()
        plugin.register(ctx)
        self.assertEqual(
            set(ctx.tools),
            {"critic_run", "critic_matrix", "critic_lessons", "critic_ledger", "critic_gate"},
        )
        for entry in ctx.tools.values():
            self.assertTrue(entry["schema"].get("description"))
            self.assertTrue(callable(entry["handler"]))
        self.assertIn("critic", ctx.commands)
        self.assertIn("critic", ctx.cli_commands)
        self.assertTrue(callable(ctx.cli_commands["critic"]["setup_fn"]))
        self.assertTrue(callable(ctx.cli_commands["critic"]["handler_fn"]))
        # Two hooks (fix pass Wave 2.10, review 1C-17): the two observers
        # that read fields Hermes never sends, against a contract no run
        # leaves open, are gone from the registration and the manifest.
        self.assertEqual(set(ctx.hooks), {"on_session_start", "pre_llm_call"})
        manifest = (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8")
        declared = re.findall(r"^\s*-\s*(on_session_start|pre_llm_call|post_tool_call|post_llm_call)\s*$",
                              manifest, re.M)
        self.assertEqual(sorted(declared), ["on_session_start", "pre_llm_call"])

    def test_register_without_cli_command_support(self):
        ctx = FakeCtxNoCli()
        before = len(plugin.REGISTRATION_NOTES)
        plugin.register(ctx)  # must not raise
        self.assertEqual(
            set(ctx.tools),
            {"critic_run", "critic_matrix", "critic_lessons", "critic_ledger", "critic_gate"},
        )
        self.assertGreater(len(plugin.REGISTRATION_NOTES), before)
        self.assertTrue(
            any("register_cli_command" in n for n in plugin.REGISTRATION_NOTES)
        )

    def test_critic_lessons_handler_returns_seeded_lesson(self):
        ctx = FakeCtx()
        store = plugin.register(ctx)
        self.assertIsNotNone(store)
        store.create_lesson(
            contract_id="seed-1", domain="code", tags=["x"],
            outcome="seeded outcome", cause="seeded cause",
            rule="seeded rule xyz", route_change=None,
            evidence=["verdict:seed-1"], written_to="plugin_store",
            provenance="generated",
        )
        handler = ctx.tools["critic_lessons"]["handler"]
        out = json.loads(handler({"domain": "code"}))
        rules = [lesson.get("rule") for lesson in out["lessons"]]
        self.assertIn("seeded rule xyz", rules)

    def test_critic_ledger_handler_shape(self):
        ctx = FakeCtx()
        plugin.register(ctx)
        handler = ctx.tools["critic_ledger"]["handler"]
        out = json.loads(handler({}))
        self.assertIn("summary", out)
        self.assertIn("report", out)
        self.assertIsInstance(out["summary"], list)

    def test_critic_ledger_handler_events_key_matches_store_surface(self):
        ctx = FakeCtx()
        store = plugin.register(ctx)
        handler = ctx.tools["critic_ledger"]["handler"]
        out = json.loads(handler({}))
        if hasattr(store, "events_for"):
            self.assertIn("events", out)
            self.assertIsInstance(out["events"], list)
        else:
            # Guarded surface: no events key promised until the store
            # actually records events.
            self.assertNotIn("events", out)

    def test_critic_matrix_handler_requires_contract_id(self):
        ctx = FakeCtx()
        plugin.register(ctx)
        handler = ctx.tools["critic_matrix"]["handler"]
        out = json.loads(handler({}))
        self.assertIn("error", out)

    def test_critic_matrix_handler_not_run_on_empty_store(self):
        ctx = FakeCtx()
        plugin.register(ctx)
        handler = ctx.tools["critic_matrix"]["handler"]
        out = json.loads(handler({"contract_id": "ctr-none"}))
        self.assertEqual(out["contract_id"], "ctr-none")
        self.assertEqual(out["rubric"], "validity_matrix_v1")
        self.assertEqual(out["note"], "not run")
        self.assertEqual(out["candidates"], {})
        self.assertEqual(len(out["rows"]), 4)
        self.assertIn("panel_status", out)

    def test_critic_matrix_handler_populated_shape(self):
        ctx = FakeCtx()
        store = plugin.register(ctx)
        cid = store.create_candidate(
            "ctr-matrix", 1, "implement", "expert-a", "fam-a",
            "a", "/wt/a", "basehash",
        )
        # Seed with plain positional add_score calls only.
        store.add_score(
            cid, "matrix", "validity_matrix_v1", "judge-1", "fam-j",
            True, 1, {"correctness": 9.0, "evidence": 8.0}, 8.5,
        )
        # A non-matrix score row for the same candidate must not leak in.
        store.add_score(
            cid, "judged", "code_quality_v1", "judge-9", "fam-x",
            True, 1, {"correctness": 1.0}, 1.0,
        )
        handler = ctx.tools["critic_matrix"]["handler"]
        out = json.loads(handler({"contract_id": "ctr-matrix"}))
        self.assertIsNone(out["note"])
        self.assertIn("correctness", out["rows"])
        self.assertIn("evidence", out["rows"])
        candidate = out["candidates"][cid]
        self.assertEqual(candidate["expert_id"], "expert-a")
        self.assertEqual(candidate["family"], "fam-a")
        self.assertEqual(set(candidate["cells"]), {"judge-1"})
        cell = candidate["cells"]["judge-1"]
        self.assertEqual(cell["judge_family"], "fam-j")
        self.assertEqual(cell["items"]["correctness"], 9.0)
        self.assertEqual(cell["items"]["evidence"], 8.0)
        self.assertEqual(cell["mean"], 8.5)
        self.assertIn("confidence", cell)
        self.assertIn("details", cell)
        self.assertEqual(candidate["row_results"], {})
        self.assertEqual(candidate["dissent"], [])

    def test_critic_run_requires_repo_path(self):
        ctx = FakeCtx()
        plugin.register(ctx)
        handler = ctx.tools["critic_run"]["handler"]
        out = json.loads(handler({"contract": {"id": "x"}}))
        self.assertIn("error", out)

    def test_command_handler_reports(self):
        ctx = FakeCtx()
        plugin.register(ctx)
        handler = ctx.commands["critic"]["handler"]
        self.assertIn("ledger: not run", handler("ledger"))
        self.assertIn("lessons: not run", handler("lessons"))
        help_text = handler("")
        self.assertIn("critic run", help_text)
        self.assertIn("unknown critic subcommand", handler("bogus"))

    def test_hooks_bound_and_functional(self):
        ctx = FakeCtx()
        store = plugin.register(ctx)
        store.create_lesson(
            contract_id="seed-1", domain="code", tags=[],
            outcome="o", cause="c", rule="hook rule abc",
            route_change=None, evidence=["verdict:seed-1"],
            written_to="plugin_store", provenance="generated",
        )
        result = ctx.hooks["pre_llm_call"]()
        self.assertIsNotNone(result)
        self.assertIn("hook rule abc", result["context"])
        # Hermes passes is_first_turn: later turns inject nothing (Wave 2.3).
        self.assertIsNone(ctx.hooks["pre_llm_call"](is_first_turn=False))
        self.assertIn("hook rule abc", ctx.hooks["pre_llm_call"](is_first_turn=True)["context"])
        # The host passes no domain; the default 'code' family must still
        # reach a lesson written under a dotted domain (H10).
        store.create_lesson(
            contract_id="seed-2", domain="code.game", tags=[],
            outcome="o", cause="c", rule="dotted domain rule",
            route_change=None, evidence=["verdict:seed-2"],
            written_to="plugin_store", provenance="generated",
        )
        self.assertIn("dotted domain rule", ctx.hooks["pre_llm_call"]()["context"])
        # Observers accept kwargs and stay quiet with no open contract.
        ctx.hooks["on_session_start"](domain="code", session_id="s1")
        session_file = Path(self._tmp.name) / "session.json"
        self.assertTrue(session_file.exists())
        self.assertNotIn("post_tool_call", ctx.hooks)
        self.assertNotIn("post_llm_call", ctx.hooks)

    def test_cli_setup_builds_parser(self):
        import argparse

        ctx = FakeCtx()
        plugin.register(ctx)
        setup_fn = ctx.cli_commands["critic"]["setup_fn"]
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="top")
        setup_fn(subparsers)
        args = parser.parse_args(["critic", "lessons", "--domain", "code"])
        self.assertEqual(args.critic_command, "lessons")
        self.assertEqual(args.domain, "code")
        args = parser.parse_args(
            ["critic", "run", "c.json", "--repo", ".", "--approve"]
        )
        self.assertEqual(args.critic_command, "run")
        self.assertEqual(args.gate, "approved")

    def test_data_dir_never_plugin_dir(self):
        data_dir = plugin.resolve_data_dir(FakeCtx())
        self.assertEqual(data_dir, Path(self._tmp.name))

    def test_root_init_exposes_register(self):
        root_init = Path(__file__).resolve().parent.parent / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            "hcl_root_plugin", root_init
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(callable(module.register))


class TestDataDirPrecedence(unittest.TestCase):
    """Where state lands, in order: ctx getter, HCL_DATA_DIR, Hermes's
    storage helper, the HERMES_HOME default — the explicit override must
    beat an importable Hermes package (fix pass Wave 2.0, F-W20-02).

    A fake ``plugins.plugin_storage`` stands in for Hermes's own helper, so
    the precedence is tested on every interpreter, not only the Hermes venv.
    """

    HELPER_DIR = "/nonexistent/hermes-home/plugin-data/hybrid-critic-loop"

    def setUp(self):
        import sys
        import types

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        fake = types.ModuleType("plugins.plugin_storage")
        fake.plugin_data_dir = lambda name=None: self.HELPER_DIR
        pkg = types.ModuleType("plugins")
        pkg.plugin_storage = fake
        patcher = mock.patch.dict(
            sys.modules, {"plugins": pkg, "plugins.plugin_storage": fake}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _env_without(self, *names):
        return {k: v for k, v in os.environ.items() if k not in names}

    def test_explicit_env_outranks_the_hermes_helper(self):
        with mock.patch.dict(os.environ, {"HCL_DATA_DIR": self._tmp.name}):
            self.assertEqual(plugin.resolve_data_dir(FakeCtx()), Path(self._tmp.name))

    def test_hermes_helper_used_when_nothing_overrides_it(self):
        with mock.patch.dict(os.environ, self._env_without("HCL_DATA_DIR"), clear=True):
            self.assertEqual(plugin.resolve_data_dir(FakeCtx()), Path(self.HELPER_DIR))

    def test_ctx_getter_outranks_everything(self):
        class Ctx(FakeCtx):
            def plugin_data_dir(self):
                return "/ctx/provided/dir"

        with mock.patch.dict(os.environ, {"HCL_DATA_DIR": self._tmp.name}):
            self.assertEqual(plugin.resolve_data_dir(Ctx()), Path("/ctx/provided/dir"))

    def test_hermes_home_default_without_helper_or_override(self):
        import sys

        env = self._env_without("HCL_DATA_DIR")
        env["HERMES_HOME"] = self._tmp.name
        # A ``None`` entry makes ``import plugins.plugin_storage`` fail.
        with mock.patch.dict(sys.modules, {"plugins": None, "plugins.plugin_storage": None}), \
                mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                plugin.resolve_data_dir(FakeCtx()),
                Path(self._tmp.name) / "plugin-data" / "hybrid-critic-loop",
            )

    def test_cli_resolves_the_same_directory_as_the_plugin(self):
        from hybrid_critic_loop import cli

        with mock.patch.dict(os.environ, {"HCL_DATA_DIR": self._tmp.name}):
            self.assertEqual(cli._resolve_data_dir(None), plugin.resolve_data_dir(None))
            self.assertEqual(cli._resolve_data_dir(None), Path(self._tmp.name))
        with mock.patch.dict(os.environ, self._env_without("HCL_DATA_DIR"), clear=True):
            self.assertEqual(cli._resolve_data_dir(None), Path(self.HELPER_DIR))
        self.assertEqual(cli._resolve_data_dir("~/x"), Path("~/x").expanduser())


class TestDocsMatchTheCode(unittest.TestCase):
    """Mechanical pins for statements the documents make about the code
    (fix pass Wave 2.2): the catalog disclosure equals the README's, the
    versions agree, the catalog mirrors the manifest, and the README's
    usage examples parse against the real parsers."""

    ROOT = Path(__file__).resolve().parent.parent

    def _read(self, name):
        return (self.ROOT / name).read_text(encoding="utf-8")

    def _section(self, text, heading):
        start = text.index(heading)
        rest = text[start + len(heading):]
        end = rest.find("\n## ")
        return rest if end < 0 else rest[:end]

    def test_submission_disclosure_equals_the_readme_and_names_the_api_seats(self):
        readme = self._section(self._read("README.md"), "## Disclosures (read first)")
        readme_bullets = [l for l in readme.splitlines() if l.startswith("- **")]
        submission = self._read("SUBMISSION.md")
        quoted = [l.strip()[2:] for l in submission.splitlines() if l.strip().startswith("> - **")]
        self.assertEqual(len(readme_bullets), 5)
        self.assertEqual(quoted, readme_bullets)
        joined = "\n".join(quoted)
        self.assertIn("ollama", joined)
        self.assertIn("DeepSeek", joined)
        self.assertNotIn("makes no network calls of its own", joined)

    def test_versions_agree_across_pyproject_manifest_and_catalog(self):
        import re

        py = re.search(r'^version\s*=\s*"([^"]+)"', self._read("pyproject.toml"), re.M).group(1)
        manifest = re.search(r'^version:\s*"([^"]+)"', self._read("plugin.yaml"), re.M).group(1)
        catalog = re.search(r'^version:\s*"([^"]+)"', self._read("the-millionaire-matrix.yaml"), re.M).group(1)
        self.assertEqual(py, manifest)
        self.assertEqual(manifest, catalog)
        self.assertIn(f"v{manifest.rsplit('.', 1)[0]}", self._read("SUBMISSION.md"))

    def test_catalog_capabilities_mirror_the_manifest(self):
        import re

        def items(text, key):
            block = re.search(rf"^\s*{key}:\n((?:\s+- .+\n)+)", text, re.M)
            self.assertIsNotNone(block, key)
            return [l.strip()[2:] for l in block.group(1).splitlines()]

        manifest = self._read("plugin.yaml")
        catalog = self._read("the-millionaire-matrix.yaml")
        self.assertEqual(items(catalog, "provides_tools"), items(manifest, "provides_tools"))
        self.assertEqual(items(catalog, "provides_hooks"), items(manifest, "provides_hooks"))
        self.assertEqual(
            items(manifest, "provides_tools"),
            ["critic_run", "critic_matrix", "critic_lessons", "critic_ledger", "critic_gate"],
        )

    def test_readme_hcl_example_parses_with_the_real_parser(self):
        import argparse
        import shlex

        from hybrid_critic_loop import cli

        line = next(l for l in self._read("README.md").splitlines() if l.startswith("hcl "))
        parser = argparse.ArgumentParser(prog="hcl")
        cli._add_subcommands(parser.add_subparsers(dest="command"))
        args = parser.parse_args(shlex.split(line)[1:])  # raises SystemExit if the example is wrong
        self.assertEqual(args.command, "run")
        self.assertEqual(args.contract, "contract.json")
        self.assertEqual(args.gate, "approved")
        self.assertEqual(args.data_dir, "./hcl-data")

    def test_readme_describes_the_injection_rule_as_implemented(self):
        readme = self._read("README.md")
        self.assertIn("the exact domain first, then sub-domains and ancestors", readme)
        self.assertIn("states how many lessons the cap dropped", readme)
        self.assertIn("on a session's first turn", readme)
        self.assertNotIn("injected automatically at session start for the session's domain.", readme)

    def test_readme_describes_peer_seating_as_implemented(self):
        readme = self._read("README.md")
        self.assertIn("contestants included, up to the contract's judge count", readme)
        self.assertIn("skipped and named in the ledger", readme)

    def test_readme_states_the_one_run_validity_and_waiver_rules(self):
        # Fix pass Wave 2.7 (review H3, H4, H6): the README's rules are the loop's.
        readme = self._read("README.md")
        self.assertIn("A contract id is one run", readme)
        self.assertIn("re-verifies its baseline", readme)
        self.assertIn("a plain approval is refused", readme)
        self.assertIn("has no validity points to report", readme)

    def test_readme_states_the_no_pass_round_and_the_ledger_counters(self):
        # Fix pass Wave 2.8 (C-08, M3, M6): the ladder's one exception, the
        # ledger's per-seat counts, and the hooks docstring's first-turn rule.
        from hybrid_critic_loop import hooks as hooks_mod

        readme = self._read("README.md")
        self.assertIn("matrix failers are out before the cut", readme)
        self.assertIn("unavailable", self._section(readme, "## Cost per validity point"))
        self.assertIn("first turn", hooks_mod.__doc__)

    def test_readme_states_the_upgrade_backup_and_contract_rules(self):
        # Fix spec G10/G7 (round 3 upgrade F2/F3, round 4 upgrade
        # F3/F4, security F1): pre-0.4 certificates stop verifying and
        # are re-issued from the store, possibly with tightened totals;
        # the book is four files that travel together; a contract's
        # gate commands execute on the operator's machine from the
        # baseline run.
        readme = self._read("README.md")
        self.assertIn("stop verifying under 0.4.0", readme)
        self.assertIn("re-issued from the store", readme)
        self.assertIn("bookkeeping rows no longer count", readme)
        self.assertIn("data.db-wal", readme)
        self.assertIn("data.db-shm", readme)
        self.assertIn("restored without its `cert_key`", readme)
        self.assertIn("starting with the baseline run", readme)
        self.assertIn("trusted to run code", readme)
        # Round-5 fix spec H6 (round-5 security F3): the disclosure is
        # honest about the env scrub's limit — gate commands run as
        # the operator and can read the parent process's environment.
        self.assertIn("can still read the parent process's environment",
                      readme)
        self.assertIn("hygiene, not a boundary", readme)

    def test_readme_states_the_adapter_rules(self):
        # Fix pass Wave 2.9 (review M8): the exit code decides a CLI seat's
        # outcome; an API seat needs a target_file and declines without one.
        readme = self._read("README.md")
        self.assertIn("the exit code decides", readme)
        self.assertIn("declines a contract without a `target_file`", readme)

    def test_hook_counts_cost_disclosure_and_package_version_are_true(self):
        # Fix pass Wave 2.10 (review 1C-17, 1D-16, 1A-06): two hooks, who
        # reports a cost, and the package's own version string.
        import hybrid_critic_loop

        readme = self._read("README.md")
        submission = self._read("SUBMISSION.md")
        self.assertIn("2 hooks", readme)
        self.assertNotIn("4 hooks", readme)
        self.assertIn("(5 tools, 2 hooks)", submission)
        self.assertIn("report tokens only", self._section(readme, "## Cost per validity point"))
        py = re.search(r'^version\s*=\s*"([^"]+)"', self._read("pyproject.toml"), re.M).group(1)
        self.assertEqual(hybrid_critic_loop.__version__, py)

    def test_dev_extra_does_not_cap_pytest_below_the_hermes_workspace(self):
        # Phase 3 finding: `hermes plugins validate --install-deps` resolves
        # the [dev] extra inside the Hermes uv workspace (pytest 9.x); a
        # `<9.0` cap made the resolution unsatisfiable. The runtime needs
        # nothing; the extra must not fight the host.
        pyproject = self._read("pyproject.toml")
        self.assertIn("dependencies = []", pyproject)
        dev = re.search(r'^dev\s*=\s*\[(.*)\]', pyproject, re.M).group(1)
        self.assertIn("pytest", dev)
        self.assertNotRegex(dev, r"pytest[^\"']*<")

    def test_readme_slash_usage_matches_the_handler_help(self):
        readme = self._read("README.md")
        self.assertIn("`/critic run <contract.json> [repo_path]`", readme)
        self.assertNotIn("/critic run <task>", readme)
        with mock.patch.dict(os.environ, {"HCL_DATA_DIR": tempfile.mkdtemp()}):
            ctx = FakeCtx()
            plugin.register(ctx)
            help_text = ctx.commands["critic"]["handler"]("")
        self.assertIn("critic run <contract.json> [repo_path]", help_text)


class TestDemoNarration(unittest.TestCase):
    """The demo's Phase A outcome is built from recorded rows, not from a
    canned expectation (fix pass Wave 2.2)."""

    def _demo(self):
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("run_demo_under_test", root / "demo" / "run_demo.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_phase_a_narration_reads_the_rows(self):
        demo = self._demo()
        summary = [
            {"expert_id": "codex", "dispatches": 1, "completed": 1, "refused": 0, "timeout": 0,
             "unavailable": 0, "error": 0},
            {"expert_id": "grok", "dispatches": 1, "completed": 0, "refused": 0, "timeout": 0,
             "unavailable": 1, "error": 0},
            {"expert_id": "queue", "dispatches": 1, "completed": 0, "refused": 0, "timeout": 0},
        ]
        lines = demo.phase_a_outcome_lines(
            {"status": "escalated", "detail": "peer matrix passed no candidate"}, summary
        )
        text = "\n".join(lines)
        self.assertIn("status=escalated — peer matrix passed no candidate", lines[0])
        self.assertIn("codex: dispatches=1 completed=1 refused=0 timeout=0 unavailable=0 error=0", text)
        # An unavailable seat is named as such (fix pass Wave 2.8, review M3).
        self.assertIn("grok: dispatches=1 completed=0 refused=0 timeout=0 unavailable=1 error=0", text)
        self.assertNotIn("queue:", text)
        self.assertNotIn("expected here", text)
        quiet = demo.phase_a_outcome_lines({"status": "completed"}, [])
        self.assertEqual(quiet[0], "Phase A outcome (recorded): status=completed")


class TestStoreOpening(unittest.TestCase):
    """Fix pass Wave 2.8 (review M7 / 1C-10): the in-memory fallback exists
    for a partial checkout without state.py — never for a database that
    cannot be opened, which must fail loudly instead of silently losing
    every run."""

    def test_an_unopenable_database_raises_instead_of_falling_back(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "data.db").mkdir()  # a directory where the database file should be
            with self.assertRaises(Exception) as caught:
                plugin._open_store(Path(td))
            self.assertIn("data.db", str(caught.exception))

    def test_the_fallback_store_serves_only_a_missing_state_module(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(sys.modules, {"hybrid_critic_loop.state": None}):
                store = plugin._open_store(Path(td))
            self.assertIsInstance(store, plugin._FallbackStore)
            real = plugin._open_store(Path(td))
            try:
                self.assertNotIsInstance(real, plugin._FallbackStore)
            finally:
                real.close()


class TestHostSurface(unittest.TestCase):
    """What Hermes actually does with our registrations (fix pass Wave 2.6):
    schemas carry a parameters block, a pending human gate can be completed
    through a tool, critic_run with a decision completes the gate instead
    of re-dispatching, and setup_fn accepts the parser Hermes hands it."""

    ROOT = Path(__file__).resolve().parent.parent

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HCL_DATA_DIR": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = FakeCtx()
        self.store = plugin.register(self.ctx)

    def test_tool_schemas_carry_a_parameters_block(self):
        for name, entry in self.ctx.tools.items():
            schema = entry["schema"]
            self.assertEqual(schema["name"], name)
            self.assertTrue(schema["description"])
            params = schema["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertIsInstance(params["properties"], dict)
            self.assertIsInstance(params["required"], list)
            self.assertNotIn("properties", schema)  # no stray top-level JSON-Schema keys
        self.assertEqual(self.ctx.tools["critic_run"]["schema"]["parameters"]["required"], ["repo_path"])
        self.assertIn("contract_id", self.ctx.tools["critic_gate"]["schema"]["parameters"]["properties"])
        self.assertEqual(
            self.ctx.tools["critic_gate"]["schema"]["parameters"]["properties"]["decision"]["enum"],
            ["approved", "rejected", "waived"],
        )

    def test_critic_gate_without_a_pending_gate_is_an_error_not_a_crash(self):
        handler = self.ctx.tools["critic_gate"]["handler"]
        self.assertIn("required", json.loads(handler({"contract_id": "ctr-x"}))["error"])
        out = json.loads(handler({"contract_id": "ctr-none", "decision": "approved"}))
        self.assertEqual(out["status"], "error")
        self.assertIn("no pending human gate", out["detail"])

    def test_critic_gate_routes_to_record_human_gate(self):
        with mock.patch("hybrid_critic_loop.loop.record_human_gate",
                        return_value={"contract_id": "ctr_x", "status": "completed"}) as rec:
            out = json.loads(self.ctx.tools["critic_gate"]["handler"](
                {"contract_id": "ctr_x", "decision": "waived", "reason": "measured only"}))
        self.assertEqual(out["status"], "completed")
        # The decision is recorded with its real channel (fix spec G1,
        # round 4 security F7): it arrived through the tool.
        rec.assert_called_once_with(
            self.store, "ctr_x", "waived", "measured only", channel="tool")

    def test_critic_run_cannot_decide_its_own_gate(self):
        # Fix spec G1 (round 4 security F7): the run tool's schema has
        # no human_gate parameter, and a stray human_gate argument is
        # ignored — the run dispatches the panel and ends at the gate;
        # record_human_gate is never reached from this handler.
        schema = self.ctx.tools["critic_run"]["schema"]
        self.assertNotIn("human_gate", schema["parameters"]["properties"])
        with mock.patch("hybrid_critic_loop.loop.run_contract",
                        return_value={"contract_id": "ctr_demo_1",
                                      "status": "awaiting_human_gate"}) as run, \
                mock.patch("hybrid_critic_loop.loop.record_human_gate") as rec:
            out = json.loads(self.ctx.tools["critic_run"]["handler"]({
                "contract_path": str(self.ROOT / "contracts" / "demo1.json"),
                "repo_path": "/nonexistent/repo", "human_gate": "approved",
                "executors": "local",
            }))
        self.assertEqual(out["status"], "awaiting_human_gate")
        run.assert_called_once()
        self.assertIsNone(run.call_args.kwargs.get("human_decider"))
        rec.assert_not_called()

    def test_critic_run_handler_returns_error_json_when_the_loop_raises(self):
        # Fix spec G10 (round 4 regression F2): a loop exception becomes
        # the handler's {"error": …} JSON, never a raise into the host —
        # the same containment the CLI's dispatch boundary gives.
        with mock.patch("hybrid_critic_loop.loop.run_contract",
                        side_effect=ValueError("usage blew up")):
            out = json.loads(self.ctx.tools["critic_run"]["handler"]({
                "contract_path": str(self.ROOT / "contracts" / "demo1.json"),
                "repo_path": "/nonexistent/repo", "executors": "local",
            }))
        self.assertIn("error", out)
        self.assertIn("usage blew up", out["error"])

    def test_critic_gate_handler_returns_error_json_when_the_loop_raises(self):
        # Fix spec G10 (round 4 regression F2): same boundary on the
        # gate handler's record_human_gate call.
        with mock.patch("hybrid_critic_loop.loop.record_human_gate",
                        side_effect=RuntimeError("merge exploded")):
            out = json.loads(self.ctx.tools["critic_gate"]["handler"](
                {"contract_id": "ctr_x", "decision": "approved"}))
        self.assertIn("error", out)
        self.assertIn("merge exploded", out["error"])

    def test_slash_gate_subcommand_maps_the_verbs(self):
        handler = self.ctx.commands["critic"]["handler"]
        self.assertIn("usage: critic gate", handler("gate ctr_x"))
        with mock.patch("hybrid_critic_loop.loop.record_human_gate",
                        return_value={"contract_id": "ctr_x", "status": "rejected"}) as rec:
            rendered = handler("gate ctr_x reject too slow for the hot loop")
        self.assertIn('"status": "rejected"', rendered)
        # The slash command routes through the gate tool handler, so the
        # recorded channel is "tool" (fix spec G1).
        rec.assert_called_once_with(
            self.store, "ctr_x", "rejected", "too slow for the hot loop",
            channel="tool")
        self.assertIn("critic gate <contract_id>", handler(""))

    def test_cli_setup_accepts_the_parser_hermes_hands_it(self):
        import argparse

        setup_fn = self.ctx.cli_commands["critic"]["setup_fn"]
        plugin_parser = argparse.ArgumentParser(prog="hermes critic")   # what hermes_cli/main.py passes
        setup_fn(plugin_parser)
        args = plugin_parser.parse_args(["gate", "ctr_x", "approved", "--reason", "fine"])
        self.assertEqual(args.critic_command, "gate")
        self.assertEqual((args.contract_id, args.decision, args.reason), ("ctr_x", "approved", "fine"))
        args = plugin_parser.parse_args(["run", "c.json", "--repo", ".", "--approve"])
        self.assertEqual(args.critic_command, "run")


class TestRound4CliSurfaces(unittest.TestCase):
    """Fix spec G10/G4/G2 (round 4 upgrade F3/F4, round 3's
    corrupt-store shape): the read-only open validates the store file
    instead of conjuring a schema into it, a locked store's error
    names the likely holder and the remedy, and verify-cert scopes
    its claim with the whole-chain state after a successful verify.
    (The class docstring's original "read-only open" wording is gone:
    round-5 fix spec H6 — these commands do not create a store, but
    their open may apply a pending one-time upgrade.)"""

    def _run(self, argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from hybrid_critic_loop import cli

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _empty_store_dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        from hybrid_critic_loop.state import Store

        store = Store(Path(tmp.name) / "data.db")
        store.close()
        return tmp.name

    def test_verify_cert_prints_the_whole_chain_state_on_success(self):
        data_dir = self._empty_store_dir()
        with mock.patch("hybrid_critic_loop.chain.verify_certificate",
                        return_value=(True, {"contract_id": "ctr_x"})), \
                mock.patch("hybrid_critic_loop.chain.verify_chain_cached",
                           return_value=(True, None)):
            code, out, _err = self._run(
                ["verify-cert", "mm1.a.b", "--data-dir", data_dir])
        self.assertEqual(code, 0)
        self.assertIn("certificate: VALID", out)
        self.assertIn("chain: the whole ledger chain verifies", out)

    def test_verify_cert_warns_when_the_chain_breaks_beyond_the_prefix(self):
        data_dir = self._empty_store_dir()
        with mock.patch("hybrid_critic_loop.chain.verify_certificate",
                        return_value=(True, {"contract_id": "ctr_x"})), \
                mock.patch("hybrid_critic_loop.chain.verify_chain_cached",
                           return_value=(False, 7)):
            code, out, _err = self._run(
                ["verify-cert", "mm1.a.b", "--data-dir", data_dir])
        self.assertEqual(code, 0)
        self.assertIn("certificate: VALID", out)
        self.assertIn("warning", out)
        self.assertIn("broken at row 7", out)

    def test_open_existing_rejects_a_non_database_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "data.db").write_bytes(b"this is not a database")
            code, _out, err = self._run(["standing", "--data-dir", tmp])
        self.assertEqual(code, 1)
        self.assertIn("store file exists but is not a database", err)

    def test_open_existing_rejects_a_zero_byte_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "data.db").write_bytes(b"")
            code, _out, err = self._run(
                ["verify-cert", "mm1.a.b", "--data-dir", tmp])
        self.assertEqual(code, 1)
        self.assertIn("store file exists but is not a database", err)

    def test_a_locked_store_error_names_the_holder_and_the_remedy(self):
        data_dir = self._empty_store_dir()
        with mock.patch(
                "hybrid_critic_loop.plugin._open_store",
                side_effect=RuntimeError(
                    "hybrid-critic-loop: cannot open its database at "
                    "/x/data.db: database is locked")):
            code, _out, err = self._run(
                ["verify-cert", "mm1.a.b", "--data-dir", data_dir])
        self.assertEqual(code, 1)
        self.assertIn("database is locked", err)
        self.assertIn("Hermes session", err)
        self.assertIn("close that process", err)


class TestRound5CliSurfaces(unittest.TestCase):
    """Round-5 fix spec H3/H6 (round-5 performance HIGH, upgrade F4):
    verify-cert's scoping print goes through the chain-verified head
    cache instead of a second full walk (wording unchanged), opening
    a store whose open sealed >= 1000 rows prints the one-time
    upgrade line to stderr exactly once, and the CLI stops claiming
    its inspecting commands are "read-only" — they do not create a
    store, but their open may apply the pending one-time upgrade.
    Plus H2: the fallback store has a close() (round-5 upgrade F2).
    """

    def _run(self, argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from hybrid_critic_loop import cli

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _empty_store_dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        from hybrid_critic_loop.state import Store

        store = Store(Path(tmp.name) / "data.db")
        store.close()
        return tmp.name

    def test_verify_cert_scoping_uses_the_cached_walk_not_the_full_one(self):
        # The full walk is rigged to raise: if the CLI still consulted
        # it for the scoping print, this command would fail outright.
        data_dir = self._empty_store_dir()
        with mock.patch("hybrid_critic_loop.chain.verify_certificate",
                        return_value=(True, {"contract_id": "ctr_x"})), \
                mock.patch("hybrid_critic_loop.chain.verify_chain_cached",
                           return_value=(True, None)) as cached, \
                mock.patch("hybrid_critic_loop.chain.verify_chain",
                           side_effect=AssertionError(
                               "full walk used for the scoping print")):
            code, out, _err = self._run(
                ["verify-cert", "mm1.a.b", "--data-dir", data_dir])
        self.assertEqual(code, 0)
        cached.assert_called_once()
        self.assertIn("chain: the whole ledger chain verifies", out)

    def test_open_upgrade_notice_prints_once_at_scale(self):
        class Stub:
            chain_backfilled = 400
            chain_repairs = 300
            chain_upgrades = 300

        with mock.patch("hybrid_critic_loop.plugin._open_store",
                        return_value=Stub()):
            _code, _out, err = self._run(
                ["standing", "--data-dir", self._empty_store_dir()])
        self.assertEqual(
            err.count("one-time ledger upgrade: sealing 1000 rows…"), 1,
            err)

    def test_open_upgrade_notice_prefers_the_recorded_count(self):
        import io
        from contextlib import redirect_stderr

        from hybrid_critic_loop import cli

        class Stub:
            last_seal_count = 2500
            chain_backfilled = 0
            chain_repairs = 0
            chain_upgrades = 0

        err = io.StringIO()
        with mock.patch("hybrid_critic_loop.plugin._open_store",
                        return_value=Stub()):
            with redirect_stderr(err):
                store = cli._open_store_cli(Path(self._empty_store_dir()))
        self.assertIsInstance(store, Stub)
        self.assertEqual(
            err.getvalue().count(
                "one-time ledger upgrade: sealing 2500 rows…"), 1,
            err.getvalue())

    def test_open_upgrade_notice_silent_below_the_threshold(self):
        class Stub:
            chain_backfilled = 3
            chain_repairs = 2
            chain_upgrades = 0

        with mock.patch("hybrid_critic_loop.plugin._open_store",
                        return_value=Stub()):
            _code, _out, err = self._run(
                ["standing", "--data-dir", self._empty_store_dir()])
        self.assertNotIn("one-time ledger upgrade", err)

    def test_open_existing_wording_is_the_true_claim(self):
        from hybrid_critic_loop import cli

        doc = (cli._open_existing.__doc__ or "") + (
            cli._NoStoreError.__doc__ or "")
        self.assertNotIn("read-only", doc)
        self.assertIn("never create one", doc)
        self.assertIn("one-time", doc)
        self.assertIn("may be applied", doc)

    def test_fallback_store_close_is_a_noop(self):
        from hybrid_critic_loop.plugin import _FallbackStore

        store = _FallbackStore()
        self.assertIsNone(store.close())
        store.close()  # closing twice is equally fine


class TestCliModule(unittest.TestCase):
    def test_main_no_args_is_error(self):
        from hybrid_critic_loop import cli

        self.assertEqual(cli.main([]), 1)

    def test_ledger_and_lessons_and_session_start(self):
        from hybrid_critic_loop import cli

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cli.main(["ledger", "--data-dir", tmp]), 0)
            self.assertEqual(
                cli.main(["lessons", "--domain", "code", "--data-dir", tmp]), 0
            )
            self.assertEqual(
                cli.main(["session-start", "--domain", "code",
                          "--data-dir", tmp]), 0
            )
            self.assertTrue((Path(tmp) / "session.json").exists())


if __name__ == "__main__":
    unittest.main()
