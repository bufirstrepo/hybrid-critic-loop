"""Regression checks for approval binding and vendor process environments."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hybrid_critic_loop import executors, plugin, usage_feed
from hybrid_critic_loop.shellrun import vendor_cli_env
from tests.test_plugin import FakeCtx


class ApprovalBoundary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.dict(os.environ, {"HCL_DATA_DIR": self.temp.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.ctx = FakeCtx()
        self.store = plugin.register(self.ctx)
        self.addCleanup(self.store.close)
        self.hook = self.ctx.hooks["pre_tool_call"]

    def approve(self, args):
        result = self.hook(tool_name="critic_run", args=args)
        self.assertEqual(result["action"], "approve", result)
        self.assertIn(str(Path(args["repo_path"]).resolve()), result["message"])
        return result

    def test_explicit_and_inferred_commands(self):
        root = Path(self.temp.name)
        for args, command in [
            ({"repo_path": str(root), "task": "fix", "test_cmd": "python3 -m unittest"}, "python3 -m unittest"),
            ({"repo_path": str(root), "task": "fix"}, "npm test"),
        ]:
            (root / "package.json").write_text('{}')
            result = self.approve(args)
            self.assertIn(json.dumps(command), result["message"])
            self.assertEqual(args["contract"]["acceptance"][0]["cmd"], command)
        self.assertIsNone(self.hook(tool_name="critic_matrix", args={}))

    def test_contract_commands_frozen_and_tampering_fails_closed(self):
        root = Path(self.temp.name)
        raw = {"id": "a", "task": "fix it", "domain": "code", "tags": [], "acceptance": [
            {"class": "hard", "cmd": "python3 test.py"},
            {"class": "measured", "sensor_cmd": "python3 sensor.py", "metrics": [{"name": "m", "op": "<=", "value": 3}]},
        ]}
        path = root / "contract.json"
        path.write_text(json.dumps(raw))
        args = {"repo_path": str(root), "contract_path": str(path), "executors": "local"}
        result = self.approve(args)
        self.assertIn('"python3 test.py"', result["message"])
        self.assertIn('"python3 sensor.py"', result["message"])
        path.write_text('{}')
        with mock.patch('hybrid_critic_loop.loop.run_contract', return_value={"status": "awaiting_human_gate"}) as run:
            out = json.loads(self.ctx.tools["critic_run"]["handler"](args))
            self.assertEqual(out["status"], "awaiting_human_gate")
            self.assertEqual(run.call_args.args[1]["acceptance"][0]["cmd"], "python3 test.py")
            args["contract"]["acceptance"][0]["cmd"] = "changed"
            self.assertIn("approval", json.loads(self.ctx.tools["critic_run"]["handler"](args))["error"])
            self.assertEqual(run.call_count, 1)
        self.assertIn("approval", json.loads(self.ctx.tools["critic_run"]["handler"]({"repo_path": str(root)}))["error"])

    def test_gate_reject_and_status_but_never_merge_decisions(self):
        handler = self.ctx.tools["critic_gate"]["handler"]
        with mock.patch('hybrid_critic_loop.loop.record_human_gate', return_value={"status": "rejected"}) as record:
            for decision in ("approved", "waived"):
                self.assertIn("error", json.loads(handler({"contract_id": "x", "decision": decision})))
            record.assert_not_called()
            self.assertEqual(json.loads(handler({"contract_id": "x", "decision": "rejected"}))["status"], "rejected")
            self.assertEqual(record.call_args.kwargs["channel"], "tool")
            self.assertIsNone(json.loads(handler({"contract_id": "x", "decision": "status"}))["verdict"])


class StorageBoundary(unittest.TestCase):
    def test_claude_harvest_never_uses_environment_override(self):
        with tempfile.TemporaryDirectory() as td:
            payload = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 15}}})
            with mock.patch.dict(os.environ, {"HCL_DATA_DIR": td}):
                self.assertIsNotNone(usage_feed.harvest_claude_output(payload))
                self.assertFalse((Path(td) / usage_feed.CLAUDE_HARVEST_FILENAME).exists())
                usage_feed.harvest_claude_output(payload, data_dir=td)
                self.assertTrue((Path(td) / usage_feed.CLAUDE_HARVEST_FILENAME).exists())


class VendorEnvironment(unittest.TestCase):
    def test_real_vendor_spawn_has_only_own_config(self):
        with tempfile.TemporaryDirectory() as td:
            cli = Path(td) / 'codex'
            cli.write_text('#!/usr/bin/env python3\nimport json, os\nprint(json.dumps(dict(os.environ)))\n')
            cli.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": td + os.pathsep + os.environ.get("PATH", ""),
                    "CODEX_HOME": "/config/codex", "CLAUDE_CONFIG_DIR": "/other/config", "OTHER_PROVIDER_API_KEY": "sentinel"}):
                result, stdout = executors._cli_generate('codex', 'codex exec', Path(td))
            child = json.loads(stdout)
            self.assertEqual(child['CODEX_HOME'], '/config/codex')
            self.assertNotIn('OTHER_PROVIDER_API_KEY', child)
            self.assertNotIn('CLAUDE_CONFIG_DIR', child)
            self.assertEqual(result.status, 'completed')

    def test_quota_spawn_uses_same_allowlist(self):
        with mock.patch('hybrid_critic_loop.usage_feed.subprocess.Popen', side_effect=OSError) as spawn:
            usage_feed._run_jsonrpc_stdio(['/usr/bin/codex'], [], env={"PATH": "/bin", "CODEX_HOME": "/own", "CLAUDE_CONFIG_DIR": "/other", "OTHER_PROVIDER_API_KEY": "sentinel"})
        self.assertEqual(spawn.call_args.kwargs['env'], {"PATH": "/bin", "CODEX_HOME": "/own"})
        self.assertEqual(vendor_cli_env('claude', {"CODEX_HOME": "x", "CLAUDE_CONFIG_DIR": "y"}), {"CLAUDE_CONFIG_DIR": "y"})
