"""Verification suite for the Millionaire Matrix per-pool / per-seat usage feed.

Covers Wave 3 executable verification protocols (P0-P7 synthetic known-answer
pack), polarity tripwires, staleness fail-closed rules, pool deduplication,
cache failure asymmetry, contract ``min_usage_fraction`` pre-flight, storefront
live seat-prompting activity (``active_seat_ids``, ``being_prompted``,
``agent_cursor``), read-only ``plugin_api`` ``/usage`` + ``/status`` invariants,
and CLI ``usage`` + ``claude-statusline-tee`` subcommands.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from dashboard import plugin_api
from hybrid_critic_loop import cli as cli_mod
from hybrid_critic_loop import contracts as contracts_mod
from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop import plugin as plugin_mod
from hybrid_critic_loop import progress as progress_mod
from hybrid_critic_loop import usage_feed as uf
from hybrid_critic_loop.errors import ContractError
from hybrid_critic_loop.executors import Executor, GenResult
from hybrid_critic_loop.state import Store


REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_NOW = 1_794_360_000.0  # Fixed reference epoch (2026-11-10)


class TestStaticSafetyAndPaths(unittest.TestCase):
    """P0 Credential & Billing Static Safety Guards (R1-R8)."""

    def test_codex_reset_credit_consume_never_appears_in_production_code(self) -> None:
        forbidden_v2 = "account/" + "rateLimitResetCredit/consume"
        forbidden_v1 = "account/" + "rateLimits/resetCredit/consume"
        scan_roots = [
            REPO_ROOT / "hybrid_critic_loop",
            REPO_ROOT / "dashboard",
            REPO_ROOT / "desktop",
        ]
        hits: list[str] = []
        for root in scan_roots:
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix not in (".py", ".js"):
                    continue
                text = path.read_text(encoding="utf-8")
                if forbidden_v2 in text or forbidden_v1 in text:
                    hits.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(hits, [], f"Forbidden Codex consume RPC string found in: {hits}")

    def test_catalog_rule11_never_references_third_party_login_files(self) -> None:
        forbidden_tokens = (
            "auth.json",
            ".credentials.json",
            "kimi-code.json",
            "decode_jwt_claims_unverified",
        )
        scan_roots = [
            REPO_ROOT / "hybrid_critic_loop",
            REPO_ROOT / "dashboard",
            REPO_ROOT / "desktop",
        ]
        hits: list[str] = []
        for root in scan_roots:
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix not in (".py", ".js"):
                    continue
                text = path.read_text(encoding="utf-8")
                for tok in forbidden_tokens:
                    if tok in text:
                        hits.append(f"{path.relative_to(REPO_ROOT)}:{tok}")
        self.assertEqual(hits, [], f"Catalog Rule 11 forbidden credential-file reference found: {hits}")

    def test_pseudonymize_account_id_never_leaks_raw_secret(self) -> None:
        raw_email = "owner@example.com"
        raw_uuid = "9a12b34c-5678-49de-a012-bcdef0123456"
        h1 = uf.pseudonymize_account_id(raw_email, "codex")
        h2 = uf.pseudonymize_account_id(raw_uuid, "grok")
        self.assertRegex(h1, r"^acct-[0-9a-f]{8}$")
        self.assertRegex(h2, r"^acct-[0-9a-f]{8}$")
        self.assertNotIn("owner", h1)
        self.assertNotIn("9a12b34c", h2)
        self.assertEqual(uf.pseudonymize_account_id("", "codex"), "unknown")
        self.assertEqual(uf.pseudonymize_account_id(h1, "codex"), h1)

    def test_cross_platform_home_resolution_posix_and_windows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            appdata = root / "AppData" / "Roaming"
            (appdata / "antigravity-cli").mkdir(parents=True)
            env_win = {
                "USERPROFILE": str(root / "WinUser"),
                "APPDATA": str(appdata),
            }
            # When %USERPROFILE%\.gemini\antigravity-cli does not exist, Windows falls back to %APPDATA%\antigravity-cli
            resolved = uf.agy_dir(env=env_win, is_windows=True)
            self.assertEqual(resolved, appdata / "antigravity-cli")

            # Explicit env overrides always win on any OS
            env_overrides = {
                "CODEX_HOME": "/tmp/custom-codex",
                "GROK_HOME": "/tmp/custom-grok",
                "CLAUDE_CONFIG_DIR": "/tmp/custom-claude",
                "AGY_DIR": "/tmp/custom-agy",
                "KIMI_CODE_HOME": "/tmp/custom-kimi",
            }
            self.assertEqual(uf.codex_home(env_overrides), Path("/tmp/custom-codex"))
            self.assertEqual(uf.grok_home(env_overrides), Path("/tmp/custom-grok"))
            self.assertEqual(uf.claude_config_dir(env_overrides), Path("/tmp/custom-claude"))
            self.assertEqual(uf.agy_dir(env_overrides), Path("/tmp/custom-agy"))
            self.assertEqual(uf.kimi_code_home(env_overrides), Path("/tmp/custom-kimi"))


class TestPolarityTripwiresAndParsers(unittest.TestCase):
    """P0 Synthetic Known-Answer Pack across all seats (positive, negative, variant, polarity)."""

    def test_polarity_tripwire_used_vs_left_never_flipped(self) -> None:
        reset_future = int(BASE_NOW + 3600)

        # 1. Codex standard usedPercent=25 -> 0.75 left (convention='used')
        c_std = uf.parse_codex_rate_limits(
            {
                "rateLimits": {
                    "limitId": "codex",
                    "planType": "plus",
                    "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": reset_future},
                }
            },
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertAlmostEqual(c_std["fraction_left"], 0.75)
        self.assertEqual(c_std["used_fraction_reported"]["convention"], "used")

        # 2. Codex enterprise individualLimit.remainingPercent=25 -> 0.25 left (convention='left', NEVER 0.75!)
        c_ent = uf.parse_codex_rate_limits(
            {
                "rateLimits": {
                    "limitId": "codex",
                    "planType": "enterprise",
                    "individualLimit": {
                        "remainingPercent": 25,
                        "ordinaryUsageAllowed": True,
                        "resetsAt": reset_future,
                    },
                }
            },
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertAlmostEqual(c_ent["fraction_left"], 0.25)
        self.assertEqual(c_ent["used_fraction_reported"]["convention"], "left")

        # 3. Grok creditUsagePercent=25 -> 0.75 left (convention='used')
        g_res = uf.parse_grok_billing_config(
            {
                "subscription_tier": "SuperGrok Plus",
                "config": {
                    "creditUsagePercent": 25.0,
                    "currentPeriod": {
                        "start": uf.epoch_to_iso(BASE_NOW - 3600),
                        "end": uf.epoch_to_iso(reset_future),
                    },
                },
            },
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertAlmostEqual(g_res["fraction_left"], 0.75)
        self.assertEqual(g_res["used_fraction_reported"]["convention"], "used")

        # 4. Claude statusLine used_percentage=25 (0..100) vs stream rate_limit_event utilization=0.25 (0..1)
        cl_sl = uf.parse_claude_statusline(
            {"rate_limits": {"five_hour": {"used_percentage": 25, "resets_at": reset_future}}},
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertAlmostEqual(cl_sl["fraction_left"], 0.75)
        cl_ev = uf.parse_claude_rate_limit_event(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "unifiedWindows": {"5h": {"utilization": 0.25, "reset": reset_future}}
                },
            },
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertAlmostEqual(cl_ev["fraction_left"], 0.75)
        # Passing 0..100 value into 0..1 rate_limit_event must fail closed to unknown, never negative!
        cl_bad_scale = uf.parse_claude_rate_limit_event(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"unifiedWindows": {"5h": {"utilization": 25, "reset": reset_future}}},
            },
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertEqual(cl_bad_scale["status"], "unknown")
        self.assertIsNone(cl_bad_scale["fraction_left"])

        # 5. Antigravity remaining_fraction=0.25 -> 0.25 left (convention='left', NEVER 0.75!)
        agy_res = uf.parse_agy_usage_json(
            {
                "status": "SUCCESS",
                "num_turns": 0,
                "quota": [
                    {
                        "group_name": "Gemini Models",
                        "buckets": [
                            {
                                "id": "gemini-5h",
                                "remaining_fraction": 0.25,
                                "reset_time": uf.epoch_to_iso(reset_future),
                            }
                        ],
                    }
                ],
            },
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertAlmostEqual(agy_res["fraction_left"], 0.25)
        self.assertEqual(agy_res["used_fraction_reported"]["convention"], "left")

        # 6. Kimi used_ratio=0.25 -> 0.75 left (convention='used')
        kimi_res = uf.parse_kimi_usages(
            {"usages": {"limit_5h": {"used_ratio": 0.25, "reset_at": reset_future}}},
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertAlmostEqual(kimi_res["fraction_left"], 0.75)
        self.assertEqual(kimi_res["used_fraction_reported"]["convention"], "used")

    def test_codex_variants_multi_bucket_pro_and_expired_controls(self) -> None:
        reset_future = int(BASE_NOW + 7200)
        reset_past = int(BASE_NOW - 3600)

        # Multi-bucket separation: Spark ('codex_bengalfox') exhausted must NOT zero out main 'codex'
        multi = uf.parse_codex_rate_limits(
            {
                "result": {
                    "rateLimitsByLimitId": {
                        "codex": {
                            "limitId": "codex",
                            "planType": "pro",
                            "primary": {"usedPercent": 5, "windowDurationMins": 10080, "resetsAt": reset_future},
                            "secondary": None,
                            "rateLimitResetCredits": {"availableCount": 2, "expiresAt": reset_future + 86400},
                        },
                        "codex_bengalfox": {
                            "limitId": "codex_bengalfox",
                            "planType": "pro",
                            "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": reset_future},
                        },
                    }
                }
            },
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertEqual(multi["status"], "ok")
        self.assertAlmostEqual(multi["fraction_left"], 0.95)
        self.assertEqual(multi["plan"], "pro")
        # Pro's single primary window (10080m) must be named '7d', NOT hardcoded '5h'
        win_ids = {w["id"]: w for w in multi["windows"]}
        self.assertIn("7d", win_ids)
        self.assertIn("codex_bengalfox:5h", win_ids)
        self.assertEqual(multi["buckets"]["codex_bengalfox"]["status"], "exhausted")
        self.assertEqual(multi["reset_credits"]["available_count"], 2)
        self.assertEqual(multi["reset_credits"]["consume_policy"], "human_only")

        # Negative control: expired resetsAt 1h in the past -> status='unknown', fraction_left=None
        expired = uf.parse_codex_rate_limits(
            {
                "rateLimits": {
                    "limitId": "codex",
                    "planType": "plus",
                    "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": reset_past},
                }
            },
            fetched_at=BASE_NOW - 60,
            now=BASE_NOW,
        )
        self.assertEqual(expired["status"], "unknown")
        self.assertIsNone(expired["fraction_left"])
        self.assertEqual(expired["freshness"], "stale")

        # Negative control: empty & cross-seat wrong payload
        for bad in ({}, {"quota": []}):
            neg = uf.parse_codex_rate_limits(bad, fetched_at=BASE_NOW, now=BASE_NOW)
            self.assertEqual(neg["status"], "unknown")
            self.assertIsNone(neg["fraction_left"])

    def test_grok_wave3_absent_percent_three_way_rule_and_shrink_safe_log(self) -> None:
        p1_start = uf.epoch_to_iso(BASE_NOW - 604800)
        p1_end = uf.epoch_to_iso(BASE_NOW - 60)
        p2_start = uf.epoch_to_iso(BASE_NOW - 60)
        p2_end = uf.epoch_to_iso(BASE_NOW + 604740)

        # 1. SuperGrok Heavy omits creditUsagePercent permanently -> unknown
        heavy = uf.parse_grok_billing_config(
            {"subscription_tier": "SuperGrok Heavy", "config": {"currentPeriod": {"start": p2_start, "end": p2_end}}},
            fetched_at=BASE_NOW,
            now=BASE_NOW,
            prior_bounds=(p1_start, p1_end),
        )
        self.assertEqual(heavy["status"], "unknown")
        self.assertIsNone(heavy["fraction_left"])

        # 2. Free tier -> exhausted (0.0)
        free_t = uf.parse_grok_billing_config(
            {"subscription_tier": "Free", "config": {"currentPeriod": {"start": p2_start, "end": p2_end}}},
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertEqual(free_t["status"], "exhausted")
        self.assertEqual(free_t["fraction_left"], 0.0)

        # 3a. Paid rolling tier with absent % and NO prior_bounds (cold start) -> unknown (NEVER assume 100%!)
        cold = uf.parse_grok_billing_config(
            {"subscription_tier": "SuperGrok Plus", "config": {"currentPeriod": {"start": p2_start, "end": p2_end}}},
            fetched_at=BASE_NOW,
            now=BASE_NOW,
            prior_bounds=None,
        )
        self.assertEqual(cold["status"], "unknown")
        self.assertIsNone(cold["fraction_left"])

        # 3b. Paid rolling tier with absent % and SAME bounds -> unknown (mid-period suppression)
        same_b = uf.parse_grok_billing_config(
            {"subscription_tier": "SuperGrok Plus", "config": {"currentPeriod": {"start": p2_start, "end": p2_end}}},
            fetched_at=BASE_NOW,
            now=BASE_NOW,
            prior_bounds=(p2_start, p2_end),
        )
        self.assertEqual(same_b["status"], "unknown")
        self.assertIsNone(same_b["fraction_left"])

        # 3c. Paid rolling tier with absent % and ADVANCED bounds -> fresh rollover 1.0 left!
        rolled = uf.parse_grok_billing_config(
            {"subscription_tier": "SuperGrok Plus", "config": {"currentPeriod": {"start": p2_start, "end": p2_end}}},
            fetched_at=BASE_NOW,
            now=BASE_NOW,
            prior_bounds=(p1_start, p1_end),
        )
        self.assertEqual(rolled["status"], "ok")
        self.assertAlmostEqual(rolled["fraction_left"], 1.0)

        # 4. Legacy monthly schema (used / monthlyLimit)
        legacy = uf.parse_grok_billing_config(
            {"subscriptionTier": "SuperGrok", "config": {"used": 50, "monthlyLimit": 200, "currentPeriod": {"start": p2_start, "end": p2_end}}},
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertAlmostEqual(legacy["fraction_left"], 0.75)

        # 5. Shrink-safe unified.jsonl tail reader across truncation
        with tempfile.TemporaryDirectory() as td:
            log_p = Path(td) / "unified.jsonl"
            line1 = json.dumps({
                "msg": "latest_billing",
                "time": uf.epoch_to_iso(BASE_NOW - 10),
                "ctx": {"subscriptionTier": "SuperGrok Plus", "config": {"creditUsagePercent": 12.0}},
            })
            log_p.write_text(("noise\n" * 40) + line1 + "\n", encoding="utf-8")
            entry1, state1 = uf.read_grok_unified_log(log_p)
            self.assertEqual(entry1["ctx"]["config"]["creditUsagePercent"], 12.0)

            # Truncate file to a much smaller size with a new reading
            line2 = json.dumps({
                "msg": "latest_billing",
                "time": uf.epoch_to_iso(BASE_NOW),
                "ctx": {"subscriptionTier": "SuperGrok Plus", "config": {"creditUsagePercent": 3.0}},
            })
            log_p.write_text(line2 + "\n", encoding="utf-8")
            entry2, _state2 = uf.read_grok_unified_log(log_p, tail_state=state1)
            self.assertEqual(entry2["ctx"]["config"]["creditUsagePercent"], 3.0)

    def test_antigravity_version_gate_all_models_guard_and_proto3_zero(self) -> None:
        ok_old, ver_old, _ = uf.check_agy_version_gate("agy version 1.1.10")
        self.assertFalse(ok_old)
        self.assertEqual(ver_old, (1, 1, 10))

        ok_min, ver_min, _ = uf.check_agy_version_gate("1.1.11")
        self.assertTrue(ok_min)
        self.assertEqual(ver_min, (1, 1, 11))

        ok_cur, ver_cur, _ = uf.check_agy_version_gate("Antigravity CLI 1.5.5")
        self.assertTrue(ok_cur)
        self.assertEqual(ver_cur, (1, 5, 5))

        reset_future = uf.epoch_to_iso(BASE_NOW + 10800)

        # Fallback "All Models" group must be rejected -> unknown
        all_models = uf.parse_agy_usage_json(
            {"status": "SUCCESS", "num_turns": 0, "quota": [{"group_name": "All Models", "buckets": []}]},
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertEqual(all_models["status"], "unknown")
        self.assertIsNone(all_models["fraction_left"])

        # Non-zero num_turns must be rejected -> unknown
        billed_turn = uf.parse_agy_usage_json(
            {
                "status": "SUCCESS",
                "num_turns": 1,
                "quota": [{"group_name": "Gemini Models", "buckets": [{"id": "g", "remaining_fraction": 0.8, "reset_time": reset_future}]}],
            },
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertEqual(billed_turn["status"], "unknown")

        # Proto3 omitted remaining_fraction WITH future reset_time -> exact 0.0 exhausted
        proto_zero = uf.parse_agy_usage_json(
            {
                "status": "SUCCESS",
                "num_turns": 0,
                "quota": [{"group_name": "Gemini Models", "buckets": [{"id": "gemini-5h", "reset_time": reset_future}]}],
            },
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertEqual(proto_zero["status"], "exhausted")
        self.assertEqual(proto_zero["fraction_left"], 0.0)

        # Actual agy 1.5.5 `-p "/usage" --output-format json` wire shape (`command.data.groups` with `name`)
        agy_155 = uf.parse_agy_usage_json(
            {
                "status": "SUCCESS",
                "num_turns": 0,
                "command": {
                    "name": "usage",
                    "data": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "buckets": [
                                    {
                                        "id": "gemini-weekly",
                                        "name": "Weekly Limit Remaining",
                                        "remaining_fraction": 0.9156,
                                        "reset_time": reset_future,
                                    },
                                    {
                                        "id": "gemini-5h",
                                        "name": "Five Hour Limit Remaining",
                                        "remaining_fraction": 0.7703,
                                        "reset_time": reset_future,
                                    },
                                ],
                            },
                            {
                                "name": "Claude and GPT models",
                                "buckets": [
                                    {
                                        "id": "3p-5h",
                                        "name": "Five Hour Limit Remaining",
                                        "remaining_fraction": 1,
                                        "reset_time": reset_future,
                                    }
                                ],
                            },
                        ]
                    },
                },
            },
            fetched_at=BASE_NOW,
            now=BASE_NOW,
            group_filter="Gemini",
        )
        self.assertEqual(agy_155["status"], "ok")
        self.assertAlmostEqual(agy_155["fraction_left"], 0.7703)
        self.assertEqual(len(agy_155["windows"]), 3)
        self.assertEqual(agy_155["windows"][1]["label"], "Five Hour Limit Remaining")

    def test_pinned_cli_wire_regressions_codex_0166_grok_1077_and_stdio_open(self) -> None:
        reset_future = int(BASE_NOW + 3600)
        # 1. Codex 0.166.0 top-level `result.rateLimitResetCredits` & `result.ordinaryUsageAllowed`
        c_live = uf.parse_codex_rate_limits(
            {
                "id": 2,
                "result": {
                    "ordinaryUsageAllowed": True,
                    "rateLimits": {
                        "limitId": "codex",
                        "planType": "plus",
                        "primary": {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": reset_future},
                    },
                    "rateLimitResetCredits": {
                        "availableCount": 1,
                        "credits": [{"id": "rc_1", "expiresAt": reset_future + 86400}],
                    },
                    "accountId": "acct-9c7f6a89",
                },
            },
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertEqual(c_live["status"], "ok")
        self.assertAlmostEqual(c_live["fraction_left"], 0.99)
        self.assertEqual(c_live["reset_credits"]["available_count"], 1)
        self.assertEqual(c_live["reset_credits"]["consume_policy"], "human_only")

        # 2. Catalog Rule 11: Codex, Grok, Kimi, and API pool IDs derive purely from non-secret paths
        #    and NEVER open third-party login files or send Kimi bearer-token HTTP requests.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ch = root / "codex-home"
            gh = root / "grok-home"
            kh = root / "kimi-home"
            (ch).mkdir()
            (gh).mkdir()
            (kh / "credentials").mkdir(parents=True)

            # Plant canary login files; pool IDs must match before and after and never leak canary text
            c_before = uf._codex_pool_id_from_home(ch)
            g_before, g_exists = uf._grok_pool_id_from_home(gh)
            self.assertTrue(g_exists)
            self.assertRegex(c_before, r"^codex:acct-[0-9a-f]{8}$")
            self.assertRegex(g_before, r"^grok:acct-[0-9a-f]{8}$")

            (ch / "auth.json").write_text('{"account_id":"canary-codex-secret"}', encoding="utf-8")
            (gh / "auth.json").write_text(
                json.dumps({
                    "https://auth.x.ai::client-id": {
                        "user_id": "user-uuid-5678",
                        "principal_id": "p-1",
                        "key": "secret-token-value",
                    }
                }),
                encoding="utf-8",
            )
            (kh / "credentials" / "kimi-code.json").write_text(
                json.dumps({"access_token": "kimi-secret-bearer", "expires_at": int(BASE_NOW + 3600)}),
                encoding="utf-8",
            )
            self.assertEqual(uf._codex_pool_id_from_home(ch), c_before)
            self.assertEqual(uf._grok_pool_id_from_home(gh), (g_before, True))

            with mock.patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("Catalog Rule 11 violation: unexpected Kimi HTTP call"),
            ):
                k_reading, k_fkind = uf.probe_kimi_usage(env={"KIMI_CODE_HOME": str(kh)}, now=BASE_NOW)
                k_alias, _ = uf.probe_kimi_usages(env={"KIMI_CODE_HOME": str(kh)}, now=BASE_NOW)
            self.assertIsNone(k_fkind)
            self.assertEqual(k_reading["status"], "unknown")
            self.assertIsNone(k_reading["fraction_left"])
            self.assertRegex(k_reading["pool_id"], r"^kimi:acct-[0-9a-f]{8}$")
            self.assertEqual(k_alias["pool_id"], k_reading["pool_id"])

            # API seat pool IDs derive from non-secret home paths, not secret env values
            _, ds_p1 = uf.pool_kind_for_seat("deepseek", env={"HOME": str(root), "DEEPSEEK_API_KEY": "k1"})
            _, ds_p2 = uf.pool_kind_for_seat("deepseek", env={"HOME": str(root), "DEEPSEEK_API_KEY": "k2"})
            self.assertEqual(ds_p1, ds_p2)
            self.assertRegex(ds_p1, r"^api:deepseek:acct-[0-9a-f]{8}$")

        # 3. `_run_jsonrpc_stdio` keeps stdin open until `id=2` arrives (no premature EOF exit!)
        fake_server = (
            "import json, sys, time\n"
            "l1 = sys.stdin.readline()\n"
            "l2 = sys.stdin.readline()\n"
            "time.sleep(0.15)\n"
            "# Check stdin is still open (if closed early, read(0) works, so test a server that exits on EOF thread)\n"
            "sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':1,'result':{}}) + '\\n'); sys.stdout.flush()\n"
            "sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':2,'result':{'ok':True}}) + '\\n'); sys.stdout.flush()\n"
        )
        msg, err, timed_out = uf._run_jsonrpc_stdio(
            [os.sys.executable, "-c", fake_server],
            [{"jsonrpc": "2.0", "id": 1, "method": "initialize"}, {"jsonrpc": "2.0", "id": 2, "method": "read"}],
            target_id=2,
            timeout_s=3.0,
        )
        self.assertFalse(timed_out)
        self.assertEqual(err, "")
        self.assertEqual(msg["result"]["ok"], True)

    def test_kimi_dual_schemas_sticky_99_ignored_and_freeze_flag(self) -> None:
        reset_future = int(BASE_NOW + 7200)

        # Post-2026-09-15 usages object: parallel.limit=3 and sticky totalQuota.remaining="99" ignored
        modern = uf.parse_kimi_usages(
            {
                "usages": {
                    "limit_5h": {"used_ratio": 0.4, "reset_at": reset_future},
                    "limit_7d": {"used_ratio": 0.1, "reset_at": reset_future + 86400},
                    "parallel": {"limit": 3},
                    "totalQuota": {"used": 0, "remaining": "99"},
                }
            },
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertEqual(modern["status"], "ok")
        self.assertAlmostEqual(modern["fraction_left"], 0.6)
        self.assertEqual(len(modern["windows"]), 2)

        # Binary freeze flag: totalQuota.used > 0 forces exhausted (0.0)
        frozen = uf.parse_kimi_usages(
            {
                "usages": {
                    "limit_5h": {"used_ratio": 0.2, "reset_at": reset_future},
                    "totalQuota": {"used": 1, "remaining": "99"},
                }
            },
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertEqual(frozen["status"], "exhausted")
        self.assertEqual(frozen["fraction_left"], 0.0)

        # Pre-2026-09-15 legacy rows schema
        legacy = uf.parse_kimi_usages(
            {
                "user": {"membership": {"level": "LEVEL_INTERMEDIATE"}},
                "usage": {"limit": "100", "used": "20", "remaining": "80", "resetTime": uf.epoch_to_iso(reset_future + 86400)},
                "limits": [
                    {
                        "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                        "detail": {"limit": "100", "used": "30", "remaining": "70", "resetTime": uf.epoch_to_iso(reset_future)},
                    }
                ],
            },
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertEqual(legacy["status"], "ok")
        self.assertAlmostEqual(legacy["fraction_left"], 0.7)
        self.assertEqual(legacy["plan"], "LEVEL_INTERMEDIATE")

    def test_prepaid_api_and_ollama_parsers(self) -> None:
        ds = uf.parse_deepseek_balance(
            {
                "is_available": True,
                "balance_infos": [
                    {"currency": "CNY", "total_balance": "50.00"},
                    {"currency": "USD", "total_balance": "14.25"},
                ],
            },
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertEqual(ds["status"], "ok")
        self.assertIsNone(ds["fraction_left"])
        self.assertAlmostEqual(ds["money_left"], 14.25)
        self.assertEqual(ds["currency"], "USD")

        ms = uf.parse_moonshot_balance(
            {"code": 0, "data": {"available_balance": 0.0, "voucher_balance": 0.0, "cash_balance": 0.0}},
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertEqual(ms["status"], "exhausted")
        self.assertEqual(ms["money_left"], 0.0)

        # OpenRouter uncapped key alone -> unknown ceiling (never fake 100%)
        or_uncapped = uf.parse_openrouter_key(
            {"data": {"limit": None, "limit_remaining": None, "usage": 4.2, "is_free_tier": False}},
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertEqual(or_uncapped["status"], "unknown")
        self.assertIsNone(or_uncapped["fraction_left"])

        # OpenRouter capped key + account credits -> effective_budget picks tighter bound
        or_eff = uf.parse_openrouter_key(
            {"data": {"limit": 20.0, "limit_remaining": 15.0, "usage": 5.0}},
            credits_payload={"data": {"total_credits": 10.0, "total_usage": 6.0}},
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertEqual(or_eff["status"], "ok")
        self.assertAlmostEqual(or_eff["money_left"], 4.0)
        self.assertAlmostEqual(or_eff["fraction_left"], 0.4)

        # Ollama local compute -> unmetered_local, fraction_left=None
        ol = uf.parse_ollama_ps(
            {"models": [{"name": "qwen2.5-coder:14b", "size_vram": 9_000_000_000}]},
            fetched_at=BASE_NOW,
            now=BASE_NOW,
        )
        self.assertEqual(ol["meter_kind"], "unmetered_local")
        self.assertEqual(ol["status"], "ok")
        self.assertIsNone(ol["fraction_left"])
        self.assertEqual(ol["vram_used_bytes"], 9_000_000_000)


class TestCacheAsymmetryTeeAndIntegrations(unittest.TestCase):
    """Cache failure asymmetry, poll floor, pool dedup, preflight, storefront cursor, API & CLI."""

    def test_cache_failure_asymmetry_and_poll_floor(self) -> None:
        cache = uf.UsageCache(min_poll_interval_s=300.0)
        pid = "codex:acct-12345678"
        good = uf.parse_codex_rate_limits(
            {
                "rateLimits": {
                    "limitId": "codex",
                    "planType": "plus",
                    "primary": {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": int(BASE_NOW + 3600)},
                }
            },
            fetched_at=BASE_NOW,
            now=BASE_NOW,
            pool_id=pid,
        )
        cache.record_probe_result(pid, good, None, now=BASE_NOW, active_polled=True)
        self.assertFalse(cache.can_active_poll(pid, now=BASE_NOW + 120.0))
        self.assertTrue(cache.can_active_poll(pid, now=BASE_NOW + 301.0))

        # Transient transport error at +60s must preserve original fetched_at (NOT bump it!)
        err_reading = uf._unknown_pool_reading(
            pool_id=pid, provider="openai", source="codex_app_server",
            freshness="error", fetched_at=BASE_NOW + 60.0, now=BASE_NOW + 60.0, detail="HTTP 503",
        )
        preserved = cache.record_probe_result(pid, err_reading, "transport", now=BASE_NOW + 60.0)
        self.assertEqual(preserved["status"], "ok")
        self.assertAlmostEqual(preserved["fraction_left"], 0.80)
        self.assertEqual(preserved["fetched_at_epoch"], BASE_NOW)
        self.assertEqual(preserved["last_probe_error"], "HTTP 503")

        # Once original TTL (300s from BASE_NOW) elapses at +305s, preserved reading fails closed to unknown!
        aged = cache.get(pid, now=BASE_NOW + 305.0)
        self.assertEqual(aged["status"], "unknown")
        self.assertIsNone(aged["fraction_left"])
        self.assertEqual(aged["freshness"], "stale")

        # Re-populate fresh reading, then trigger 401 auth error -> immediate invalidation!
        cache.record_probe_result(pid, good, None, now=BASE_NOW + 10.0)
        auth_err = uf._unknown_pool_reading(
            pool_id=pid, provider="openai", source="codex_app_server",
            freshness="auth_locked", now=BASE_NOW + 20.0, detail="HTTP 401 Unauthorized",
        )
        inv = cache.record_probe_result(pid, auth_err, "auth", now=BASE_NOW + 20.0)
        self.assertEqual(inv["status"], "unknown")
        self.assertIsNone(inv["fraction_left"])
        self.assertEqual(inv["freshness"], "auth_locked")

    def test_claude_statusline_tee_and_stream_harvest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / uf.CLAUDE_STATUSLINE_FILENAME
            raw_stdin = json.dumps({
                "version": "2.1.310",
                "workspace": {"current_dir": "/secret/private/project"},
                "transcript_path": "/secret/transcript.jsonl",
                "model": {"id": "claude-opus-4-6"},
                "rate_limits": {
                    "five_hour": {"used_percentage": 35, "resets_at": int(BASE_NOW + 3600)},
                },
            })
            wrote, chained = uf.write_claude_statusline_tee(
                raw_stdin, dest, chain_cmd="python3 -c 'import sys; print(len(sys.stdin.read()))'", now=BASE_NOW
            )
            self.assertTrue(wrote)
            self.assertEqual(int(chained.strip()), len(raw_stdin))
            saved_text = dest.read_text(encoding="utf-8")
            self.assertNotIn("/secret/private/project", saved_text)
            self.assertNotIn("transcript.jsonl", saved_text)
            if os.name != "nt":
                mode = stat.S_IMODE(dest.stat().st_mode)
                self.assertEqual(mode, 0o600)

            reading, _ = uf.probe_claude_usage(
                data_dir=td, allow_auth_status=False, now=BASE_NOW + 30.0
            )
            self.assertEqual(reading["status"], "ok")
            self.assertAlmostEqual(reading["fraction_left"], 0.65)

            # Opportunistic stream harvest
            ndjson = (
                '{"type":"assistant","message":"hi"}\n'
                + json.dumps({
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "unifiedWindows": {"5h": {"utilization": 0.15, "reset": int(BASE_NOW + 3600)}}
                    },
                })
                + "\n"
            )
            harvested = uf.harvest_claude_output(ndjson, data_dir=td, now=BASE_NOW + 40.0)
            self.assertIsNotNone(harvested)
            self.assertAlmostEqual(harvested["fraction_left"], 0.85)
            self.assertTrue((Path(td) / uf.CLAUDE_HARVEST_FILENAME).is_file())

    def test_pool_deduplication_preflight_and_storefront_judge_activity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            (repo / "calc.py").write_text("def double(x):\n    return x * 2\n", encoding="utf-8")
            (repo / "test_calc.py").write_text(
                "import unittest\nfrom calc import double\n"
                "class T(unittest.TestCase):\n"
                "    def test_d(self): self.assertEqual(double(3), 6)\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

            # Contract validation check for min_usage_fraction
            with self.assertRaises(ContractError):
                contracts_mod.validate_contract({
                    "id": "bad_muf",
                    "task": "t",
                    "domain": "code",
                    "min_usage_fraction": 1.5,
                    "acceptance": [{"class": "hard", "cmd": "python3 -m unittest"}],
                })

            mid_judge_snapshots: list[dict] = []
            data_dir = root / "data"
            data_dir.mkdir()
            store = Store(data_dir / "data.db")
            try:
                class _TrackingSeat(Executor):
                    parallel_safe = False

                    def __init__(self, eid: str, fam: str) -> None:
                        self.expert_id = eid
                        self.family = fam
                        self.provider = "nous"

                    def available(self) -> tuple[bool, str]:
                        return True, "ready"

                    def generate(self, worktree: Path, contract: dict, context: str) -> GenResult:
                        p = Path(worktree) / "calc.py"
                        p.write_text(
                            f"# improved by {self.expert_id}\ndef double(x: int) -> int:\n    return x * 2\n",
                            encoding="utf-8",
                        )
                        return GenResult(status="completed", log="ok", duration_ms=5, provenance="generated")

                    def judge(self, packet: str, rubric: dict) -> dict | None:
                        # Capture run_status.json WHILE this judge is actively being prompted!
                        mid_judge_snapshots.append(progress_mod.read_run_status(data_dir))
                        out = {}
                        for line in packet.splitlines():
                            if line.startswith("=== CANDIDATE ") and line.endswith(" ==="):
                                lbl = line[len("=== CANDIDATE ") : -len(" ===")].strip()
                                out[lbl] = {
                                    "items": {
                                        r: {
                                            "score": 8.5,
                                            "confidence": 0.9,
                                            "evidence": "calc.py:1",
                                            "rationale": "clean typing improvement",
                                        }
                                        for r in rubric["items"]
                                    }
                                }
                        return out

                # 3 seats sharing 'hermes:nous' + 1 'codex' seat whose pool is below min_usage_fraction
                execs = {
                    "mm-gpt": _TrackingSeat("mm-gpt", "openai"),
                    "mm-claude": _TrackingSeat("mm-claude", "anthropic"),
                    "mm-grok": _TrackingSeat("mm-grok", "xai"),
                    "codex": _TrackingSeat("codex", "google"),
                }

                # Populate cache: codex pool has only 0.05 left (< min_usage_fraction=0.20);
                # hermes:nous is 'unknown' (must NOT be blocked!)
                _, codex_pid = uf.pool_kind_for_seat("codex", execs["codex"])
                uf.GLOBAL_USAGE_CACHE.put(
                    uf.parse_codex_rate_limits(
                        {
                            "rateLimits": {
                                "limitId": "codex",
                                "planType": "plus",
                                "primary": {"usedPercent": 95, "windowDurationMins": 300, "resetsAt": int(BASE_NOW + 3600)},
                            }
                        },
                        fetched_at=BASE_NOW,
                        now=BASE_NOW,
                        pool_id=codex_pid,
                    )
                )
                # Mark codex pool as recently polled so preflight uses the cached 0.05 reading deterministically
                uf.GLOBAL_USAGE_CACHE._last_active_poll[codex_pid] = BASE_NOW

                contract = contracts_mod.validate_contract({
                    "id": "uf_preflight_and_cursor",
                    "mode": "improve",
                    "task": "Add type annotations to double()",
                    "domain": "code",
                    "target_files": ["calc.py"],
                    "min_usage_fraction": 0.20,
                    "acceptance": [
                        {"class": "hard", "cmd": "python3 -m unittest"},
                        {"class": "judged", "rubric": "validity_matrix_v1", "min": 6.0, "judges": 4},
                    ],
                })

                with mock.patch("hybrid_critic_loop.usage_feed._now_ts", return_value=BASE_NOW + 10.0):
                    result = loop_mod.run_contract(store, contract, repo, execs)

                self.assertEqual(result["status"], "awaiting_human_gate")
                cands = {c["expert_id"]: c for c in store.candidates_for("uf_preflight_and_cursor")}
                # 'codex' was skipped at pre-flight without creating a worktree; 'mm-*' ran cleanly!
                self.assertEqual(cands["codex"]["status"], "unavailable")
                self.assertIn("below contract min_usage_fraction", cands["codex"]["evidence"]["detail"])
                self.assertFalse(Path(cands["codex"]["worktree"]).exists())

                # Verify storefront mid-judging activity was captured in run_status.json!
                self.assertTrue(len(mid_judge_snapshots) >= 3)
                saw_prompting = any(
                    bool(snap.get("active_seat_ids"))
                    and snap.get("agent_cursor", {}).get("action") == "judging"
                    for snap in mid_judge_snapshots
                )
                self.assertTrue(saw_prompting, f"Expected active_seat_ids during matrix judging, got {mid_judge_snapshots}")

                # Final run_status must have cleared active_seat_ids
                final_st = progress_mod.read_run_status(data_dir)
                self.assertEqual(final_st["active_seat_ids"], [])
            finally:
                store.close()

    def test_plugin_api_and_cli_usage_are_strictly_read_only_on_fresh_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            nonexistent_data_dir = Path(td) / "fresh_never_created"
            with mock.patch.dict(os.environ, {"HCL_DATA_DIR": str(nonexistent_data_dir)}):
                status_resp = asyncio.run(plugin_api.get_status(refresh_panel=False))
                self.assertTrue(status_resp["ok"])
                self.assertIn("usage", status_resp)
                self.assertFalse(nonexistent_data_dir.exists())

                usage_resp = asyncio.run(plugin_api.get_usage(refresh=False, include_cli_pools=True))
                self.assertTrue(usage_resp["ok"])
                self.assertIn("pools", usage_resp)
                self.assertIn("seats", usage_resp)
                self.assertIn("active_seat_ids", usage_resp)
                self.assertFalse(nonexistent_data_dir.exists())

                # CLI `usage --passive --json` also creates neither directory nor data.db
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cli_mod.main(["usage", "--passive", "--json", "--executors", "local"])
                self.assertEqual(rc, 0)
                parsed_cli = json.loads(buf.getvalue())
                self.assertIn("local:solver", parsed_cli["pools"])
                self.assertEqual(len(parsed_cli["seats"]), 2)
                # Both solver-naive and solver-ring deduplicate onto the single 'local:solver' pool!
                self.assertEqual(parsed_cli["seats"]["solver-naive"]["pool_id"], "local:solver")
                self.assertEqual(parsed_cli["seats"]["solver-ring"]["pool_id"], "local:solver")
                self.assertFalse(nonexistent_data_dir.exists())


if __name__ == "__main__":
    unittest.main()
