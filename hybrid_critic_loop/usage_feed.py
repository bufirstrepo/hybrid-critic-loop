"""Normalized per-pool and per-seat plan-usage feed for The Millionaire Matrix.

Implements the verified multi-CLI / multi-API usage data layer (Waves 1-3):
- **Pools, not seats:** Multiple seats sharing one account or daemon map to a
  single ``pool_id`` so quota is never double-counted or double-polled.
- **Strict polarity normalization:** Codex (``usedPercent``), Grok
  (``creditUsagePercent``), Claude (``used_percentage`` 0-100 / ``utilization``
  0-1), Kimi (``used_ratio`` 0-1), and Hermes account cache report **USED**
  (``fraction_left = 1 - used``). **Only** Codex enterprise
  ``individualLimit.remainingPercent`` (0-100) and Antigravity
  ``remaining_fraction`` (0-1) report **LEFT** (never inverted). Every window
  and pool preserves ``used_fraction_reported: {value, unit, convention}``.
- **Fail closed on staleness & ambiguity:** A window reading is valid iff
  ``now < min(resets_at, fetched_at + ttl_s)``. Past ``resets_at``, past TTL,
  missing, or ambiguous readings return ``status = "unknown"`` and
  ``fraction_left = None`` — never a fabricated ``0.0`` or ``1.0``.
- **Credential safety (R1-R8):** Pure read-only inspection. Never refreshes any
  CLI's OAuth token, never mutates credential files, never logs raw tokens /
  emails / account IDs (pseudonymized to ``acct-<8hex>``), never invokes Codex
  reset-credit consume RPCs (surfaces read-only count/expiry
  with ``consume_policy = "human_only"``), and enforces ``agy >= 1.1.11``
  before invoking ``agy -p "/usage" --output-format json``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MIN_POLL_INTERVAL_S = 300.0

CODEX_TTL_S = 300.0
GROK_TTL_S = 300.0
AGY_TTL_S = 300.0
CLAUDE_TTL_S = 900.0
KIMI_TTL_S = 300.0
API_TTL_S = 300.0
OLLAMA_TTL_S = 120.0
HERMES_TTL_S = 900.0

AGY_MIN_SAFE_VERSION = (1, 1, 11)

CLAUDE_STATUSLINE_FILENAME = "claude_statusline.json"
CLAUDE_HARVEST_FILENAME = "claude_harvest.json"
USAGE_CACHE_FILENAME = "usage_feed_cache.json"

_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_GROK_TAIL_MAX_BYTES = 524_288


# ---------------------------------------------------------------------------
# Time, formatting, and credential-hygiene helpers
# ---------------------------------------------------------------------------

def _now_ts(now: float | None = None) -> float:
    return float(now) if now is not None else time.time()


def epoch_to_iso(ts: float | int | None) -> str | None:
    if ts is None or isinstance(ts, bool):
        return None
    try:
        val = float(ts)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val <= 0:
        return None
    try:
        return datetime.fromtimestamp(val, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OSError, OverflowError, ValueError):
        return None


def parse_epoch(value: Any) -> float | None:
    """Parse Unix epoch seconds (or ms if > 1e12) or ISO-8601 timestamp to seconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        val = float(value)
        if not math.isfinite(val) or val <= 0:
            return None
        if val > 1_000_000_000_000.0:
            val /= 1000.0
        return val
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            num = float(text)
            if math.isfinite(num) and num > 0:
                return num / 1000.0 if num > 1_000_000_000_000.0 else num
        except ValueError:
            pass
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _clamp(val: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, float(val)))


def is_window_fresh(
    fetched_at: float | None,
    resets_at: float | None,
    ttl_s: float,
    now: float,
) -> bool:
    """Valid iff ``now < min(resets_at, fetched_at + ttl_s)``."""
    if fetched_at is None or not math.isfinite(fetched_at):
        return False
    if now >= (fetched_at + float(ttl_s)):
        return False
    if resets_at is not None and now >= resets_at:
        return False
    return True


def pseudonymize_account_id(raw_value: str | None, namespace: str = "") -> str:
    """Hash an account/key identifier to ``acct-<8hex>``; never persist raw IDs."""
    text = str(raw_value or "").strip()
    if not text:
        return "unknown"
    if re.fullmatch(r"acct-[0-9a-f]{8}", text):
        return text
    material = f"{namespace}:{text}".encode("utf-8")
    return "acct-" + hashlib.sha256(material).hexdigest()[:8]


def decode_jwt_claims_unverified(token: str | None) -> dict[str, Any]:
    """Decode a JWT payload segment in memory without signature verification or logging."""
    if not token or not isinstance(token, str):
        return {}
    parts = token.strip().split(".")
    if len(parts) < 2:
        return {}
    seg = parts[1]
    pad = "=" * ((4 - len(seg) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode((seg + pad).encode("ascii"))
        obj = json.loads(raw.decode("utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Cross-platform CLI home resolution (POSIX + Windows)
# ---------------------------------------------------------------------------

def _user_home(env: dict[str, str] | None = None) -> Path:
    e = env if env is not None else os.environ
    if os.name == "nt":
        up = e.get("USERPROFILE")
        if up:
            return Path(up)
    home = e.get("HOME") or e.get("USERPROFILE")
    return Path(home) if home else Path.home()


def codex_home(env: dict[str, str] | None = None) -> Path:
    e = env if env is not None else os.environ
    override = e.get("CODEX_HOME")
    return Path(override).expanduser() if override else (_user_home(e) / ".codex")


def grok_home(env: dict[str, str] | None = None) -> Path:
    e = env if env is not None else os.environ
    override = e.get("GROK_HOME")
    return Path(override).expanduser() if override else (_user_home(e) / ".grok")


def claude_config_dir(env: dict[str, str] | None = None) -> Path:
    e = env if env is not None else os.environ
    override = e.get("CLAUDE_CONFIG_DIR")
    return Path(override).expanduser() if override else (_user_home(e) / ".claude")


def agy_dir(env: dict[str, str] | None = None, is_windows: bool | None = None) -> Path:
    e = env if env is not None else os.environ
    override = e.get("AGY_DIR")
    if override:
        return Path(override).expanduser()
    win = (os.name == "nt") if is_windows is None else bool(is_windows)
    base = _user_home(e) / ".gemini" / "antigravity-cli"
    if win and not base.exists():
        appdata = e.get("APPDATA")
        if appdata:
            alt = Path(appdata) / "antigravity-cli"
            if alt.exists():
                return alt
    return base


def kimi_code_home(env: dict[str, str] | None = None) -> Path:
    e = env if env is not None else os.environ
    override = e.get("KIMI_CODE_HOME")
    if override:
        return Path(override).expanduser()
    primary = _user_home(e) / ".kimi-code"
    if primary.exists():
        return primary
    legacy = _user_home(e) / ".kimi"
    if (legacy / "credentials" / "kimi-code.json").is_file():
        return legacy
    return primary


def _unknown_pool_reading(
    *,
    pool_id: str,
    provider: str,
    source: str,
    meter_kind: str = "subscription_window",
    plan: str | None = None,
    freshness: str = "missing",
    fetched_at: float | None = None,
    ttl_s: float = 300.0,
    now: float | None = None,
    detail: str | None = None,
    windows: list[dict[str, Any]] | None = None,
    reset_credits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now_val = _now_ts(now)
    age_s = round(max(0.0, now_val - fetched_at), 1) if fetched_at is not None else None
    return {
        "pool_id": pool_id,
        "provider": provider,
        "meter_kind": meter_kind,
        "status": "unknown",
        "fraction_left": None,
        "resets_at": None,
        "plan": plan,
        "source": source,
        "freshness": freshness,
        "is_fresh": False,
        "fetched_at": epoch_to_iso(fetched_at),
        "fetched_at_epoch": fetched_at,
        "age_s": age_s,
        "ttl_s": ttl_s,
        "used_fraction_reported": None,
        "windows": list(windows or []),
        "money_left": None,
        "currency": None,
        "reset_credits": reset_credits,
        "detail": detail,
    }


def recheck_reading_freshness(reading: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Re-evaluate ``now < min(resets_at, fetched_at + ttl_s)`` on a cached reading."""
    out = dict(reading)
    now_val = _now_ts(now)
    fetched_epoch = parse_epoch(out.get("fetched_at_epoch") or out.get("fetched_at"))
    ttl_s = float(out.get("ttl_s") or 300.0)
    if fetched_epoch is not None:
        out["age_s"] = round(max(0.0, now_val - fetched_epoch), 1)

    if out.get("status") == "unknown":
        out["fraction_left"] = None
        out["is_fresh"] = False
        return out

    meter_kind = out.get("meter_kind")
    if meter_kind == "unmetered_local":
        if fetched_epoch is not None and now_val >= fetched_epoch + ttl_s:
            out["status"] = "unknown"
            out["fraction_left"] = None
            out["freshness"] = "stale"
            out["is_fresh"] = False
        return out

    resets_epoch = parse_epoch(out.get("resets_at"))
    if not is_window_fresh(fetched_epoch, resets_epoch, ttl_s, now_val):
        out["status"] = "unknown"
        out["fraction_left"] = None
        out["freshness"] = "stale"
        out["is_fresh"] = False
        return out

    # Also check individual windows if present
    raw_windows = out.get("windows") or []
    if raw_windows and meter_kind == "subscription_window":
        updated_windows = []
        fresh_governing: list[dict[str, Any]] = []
        for w in raw_windows:
            w_copy = dict(w)
            w_reset = parse_epoch(w_copy.get("resets_at"))
            w_fresh = is_window_fresh(fetched_epoch, w_reset, ttl_s, now_val)
            w_copy["is_fresh"] = w_fresh
            if not w_fresh:
                w_copy["fraction_left"] = None
            elif w_copy.get("governs_pool", True) and w_copy.get("fraction_left") is not None:
                fresh_governing.append(w_copy)
            updated_windows.append(w_copy)
        out["windows"] = updated_windows
        if not fresh_governing and out.get("fraction_left") is not None:
            # If the reading had windows and every governing window expired: fail closed
            if any(w.get("governs_pool", True) for w in raw_windows):
                out["status"] = "unknown"
                out["fraction_left"] = None
                out["freshness"] = "stale"
                out["is_fresh"] = False
    return out


# ---------------------------------------------------------------------------
# 1. Codex CLI (0.166.0 verified: app-server account/rateLimits/read)
# ---------------------------------------------------------------------------

def _codex_duration_label(mins: Any, fallback_slot: str) -> str:
    if isinstance(mins, int) and not isinstance(mins, bool) and mins > 0:
        if mins == 300:
            return "5h"
        if mins == 10080:
            return "7d"
        if mins == 1440:
            return "24h"
        if mins == 60:
            return "1h"
        if mins % 1440 == 0:
            return f"{mins // 1440}d"
        if mins % 60 == 0:
            return f"{mins // 60}h"
        return f"{mins}m"
    return fallback_slot


def _parse_codex_reset_credits(rc: Any) -> dict[str, Any] | None:
    """Read-only extraction of Codex reset-credit count & earliest expiry.

    Hard rule: reset-credit consumption stays strictly human-triggered.
    """
    if not isinstance(rc, dict):
        return None
    raw_count = rc.get("availableCount")
    if isinstance(raw_count, bool) or not isinstance(raw_count, int):
        return None
    expiries: list[float] = []
    top_exp = parse_epoch(rc.get("expiresAt"))
    if top_exp is not None:
        expiries.append(top_exp)
    credits_list = rc.get("credits")
    if isinstance(credits_list, list):
        for item in credits_list:
            if isinstance(item, dict):
                ep = parse_epoch(item.get("expiresAt"))
                if ep is not None:
                    expiries.append(ep)
    earliest = min(expiries) if expiries else None
    return {
        "available_count": max(0, raw_count),
        "earliest_expires_at": epoch_to_iso(earliest),
        "consume_policy": "human_only",
    }


def parse_codex_rate_limits(
    payload: Any,
    *,
    fetched_at: float | None = None,
    now: float | None = None,
    ttl_s: float = CODEX_TTL_S,
    pool_id: str = "codex:unknown",
    target_limit_id: str = "codex",
) -> dict[str, Any]:
    """Normalize Codex ``GetAccountRateLimitsResponse`` into a PoolUsage reading."""
    now_val = _now_ts(now)
    fetched_val = _now_ts(fetched_at) if fetched_at is not None else now_val

    if not isinstance(payload, dict):
        return _unknown_pool_reading(
            pool_id=pool_id, provider="openai", source="codex_app_server",
            fetched_at=fetched_val, ttl_s=ttl_s, now=now_val, detail="non-object payload",
        )

    body = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    if not isinstance(body, dict) or (
        "rateLimits" not in body and "rateLimitsByLimitId" not in body
    ):
        return _unknown_pool_reading(
            pool_id=pool_id, provider="openai", source="codex_app_server",
            fetched_at=fetched_val, ttl_s=ttl_s, now=now_val,
            detail="missing rateLimits / rateLimitsByLimitId",
        )

    raw_by_id = body.get("rateLimitsByLimitId")
    raw_single = body.get("rateLimits")
    bucket_snaps: dict[str, dict[str, Any]] = {}
    if isinstance(raw_by_id, dict):
        for lid, snap in raw_by_id.items():
            if isinstance(snap, dict):
                bucket_snaps[str(lid)] = snap
    if isinstance(raw_single, dict):
        lid = str(raw_single.get("limitId") or "codex")
        bucket_snaps.setdefault(lid, raw_single)

    if not bucket_snaps:
        return _unknown_pool_reading(
            pool_id=pool_id, provider="openai", source="codex_app_server",
            fetched_at=fetched_val, ttl_s=ttl_s, now=now_val, detail="empty rateLimit buckets",
        )

    selected_bucket = (
        target_limit_id
        if target_limit_id in bucket_snaps
        else ("codex" if "codex" in bucket_snaps else next(iter(bucket_snaps)))
    )

    plan_type: str | None = None
    reset_credits: dict[str, Any] | None = (
        _parse_codex_reset_credits(body.get("rateLimitResetCredits"))
        if "rateLimitResetCredits" in body
        else None
    )
    all_windows: list[dict[str, Any]] = []
    buckets_out: dict[str, dict[str, Any]] = {}
    any_seen_window = False
    ordinary_blocked_on_selected = (body.get("ordinaryUsageAllowed") is False)

    for lid, snap in bucket_snaps.items():
        if plan_type is None and isinstance(snap.get("planType"), str):
            plan_type = snap["planType"]
        if reset_credits is None and "rateLimitResetCredits" in snap:
            reset_credits = _parse_codex_reset_credits(snap.get("rateLimitResetCredits"))

        governs_pool = (lid == selected_bucket)
        bucket_windows: list[dict[str, Any]] = []

        for slot_name in ("primary", "secondary"):
            win = snap.get(slot_name)
            if not isinstance(win, dict):
                continue
            used_pct = win.get("usedPercent")
            if not _is_num(used_pct):
                continue
            any_seen_window = True
            clamped_used = _clamp(float(used_pct), 0.0, 100.0)
            left = round(1.0 - (clamped_used / 100.0), 6)
            reset_ep = parse_epoch(win.get("resetsAt"))
            fresh = is_window_fresh(fetched_val, reset_ep, ttl_s, now_val)
            dur_label = _codex_duration_label(win.get("windowDurationMins"), slot_name)
            win_id = dur_label if lid == selected_bucket else f"{lid}:{dur_label}"
            w_entry = {
                "id": win_id,
                "bucket_id": lid,
                "slot": slot_name,
                "window_minutes": win.get("windowDurationMins"),
                "fraction_left": left if fresh else None,
                "resets_at": epoch_to_iso(reset_ep),
                "is_fresh": fresh,
                "governs_pool": governs_pool,
                "used_fraction_reported": {
                    "value": float(used_pct),
                    "unit": "percent",
                    "convention": "used",
                },
            }
            bucket_windows.append(w_entry)
            all_windows.append(w_entry)

        ind = snap.get("individualLimit")
        if isinstance(ind, dict):
            rem_pct = ind.get("remainingPercent")
            ord_allowed = ind.get("ordinaryUsageAllowed")
            reset_ep = parse_epoch(ind.get("resetsAt"))
            fresh = is_window_fresh(fetched_val, reset_ep, ttl_s, now_val)
            if _is_num(rem_pct):
                any_seen_window = True
                # CRITICAL POLARITY: Codex enterprise individualLimit.remainingPercent
                # is ALREADY % LEFT (0-100). NEVER invert with (100 - val)!
                clamped_rem = _clamp(float(rem_pct), 0.0, 100.0)
                left = 0.0 if ord_allowed is False else round(clamped_rem / 100.0, 6)
                win_id = "individual" if lid == selected_bucket else f"{lid}:individual"
                w_entry = {
                    "id": win_id,
                    "bucket_id": lid,
                    "slot": "individualLimit",
                    "window_minutes": ind.get("windowDurationMins"),
                    "fraction_left": left if fresh else None,
                    "resets_at": epoch_to_iso(reset_ep),
                    "is_fresh": fresh,
                    "governs_pool": governs_pool,
                    "ordinary_usage_allowed": ord_allowed,
                    "used_fraction_reported": {
                        "value": float(rem_pct),
                        "unit": "percent",
                        "convention": "left",
                    },
                }
                bucket_windows.append(w_entry)
                all_windows.append(w_entry)
                if governs_pool and fresh and ord_allowed is False:
                    ordinary_blocked_on_selected = True
            elif ord_allowed is False and fresh:
                any_seen_window = True
                win_id = "individual" if lid == selected_bucket else f"{lid}:individual"
                w_entry = {
                    "id": win_id,
                    "bucket_id": lid,
                    "slot": "individualLimit",
                    "window_minutes": ind.get("windowDurationMins"),
                    "fraction_left": 0.0,
                    "resets_at": epoch_to_iso(reset_ep),
                    "is_fresh": True,
                    "governs_pool": governs_pool,
                    "ordinary_usage_allowed": False,
                    "used_fraction_reported": {
                        "value": 0.0,
                        "unit": "percent_ordinary_blocked",
                        "convention": "left",
                    },
                }
                bucket_windows.append(w_entry)
                all_windows.append(w_entry)
                if governs_pool:
                    ordinary_blocked_on_selected = True

        fresh_b_wins = [w for w in bucket_windows if w["is_fresh"] and w["fraction_left"] is not None]
        if fresh_b_wins:
            gov_b = min(fresh_b_wins, key=lambda w: float(w["fraction_left"]))
            buckets_out[lid] = {
                "limit_id": lid,
                "limit_name": snap.get("limitName"),
                "fraction_left": gov_b["fraction_left"],
                "resets_at": gov_b["resets_at"],
                "status": "exhausted" if gov_b["fraction_left"] <= 0.0 else "ok",
            }
        else:
            buckets_out[lid] = {
                "limit_id": lid,
                "limit_name": snap.get("limitName"),
                "fraction_left": None,
                "resets_at": None,
                "status": "unknown",
            }

    governing_fresh = [
        w for w in all_windows
        if w.get("governs_pool") and w.get("is_fresh") and w.get("fraction_left") is not None
    ]
    if not governing_fresh:
        freshness = "stale" if any_seen_window else "missing"
        res = _unknown_pool_reading(
            pool_id=pool_id,
            provider="openai",
            source="codex_app_server",
            plan=plan_type,
            freshness=freshness,
            fetched_at=fetched_val,
            ttl_s=ttl_s,
            now=now_val,
            windows=all_windows,
            reset_credits=reset_credits,
            detail="governing Codex rate-limit window is expired or missing",
        )
        res["buckets"] = buckets_out
        return res

    gov = min(governing_fresh, key=lambda w: float(w["fraction_left"]))
    frac_left = 0.0 if ordinary_blocked_on_selected else float(gov["fraction_left"])
    status = "exhausted" if frac_left <= 0.0 else "ok"
    return {
        "pool_id": pool_id,
        "provider": "openai",
        "meter_kind": "subscription_window",
        "status": status,
        "fraction_left": frac_left,
        "resets_at": gov["resets_at"],
        "plan": plan_type,
        "source": "codex_app_server",
        "freshness": "fresh",
        "is_fresh": True,
        "fetched_at": epoch_to_iso(fetched_val),
        "fetched_at_epoch": fetched_val,
        "age_s": round(max(0.0, now_val - fetched_val), 1),
        "ttl_s": ttl_s,
        "used_fraction_reported": gov["used_fraction_reported"],
        "windows": all_windows,
        "buckets": buckets_out,
        "money_left": None,
        "currency": None,
        "reset_credits": reset_credits,
        "detail": None,
    }


def _codex_pool_id_from_home(home: Path) -> str:
    auth_file = home / "auth.json"
    try:
        if auth_file.is_file():
            obj = json.loads(auth_file.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                tokens = obj.get("tokens") if isinstance(obj.get("tokens"), dict) else {}
                acct = tokens.get("account_id") or obj.get("account_id")
                if not acct and isinstance(tokens.get("access_token"), str):
                    claims = decode_jwt_claims_unverified(tokens["access_token"])
                    oai_auth = claims.get("https://api.openai.com/auth")
                    if isinstance(oai_auth, dict):
                        acct = oai_auth.get("chatgpt_account_id")
                    acct = acct or claims.get("sub")
                if acct:
                    return f"codex:{pseudonymize_account_id(str(acct), 'codex')}"
    except Exception:
        pass
    return f"codex:{pseudonymize_account_id(str(home), 'codex-home')}"


def _codex_uses_keyring_without_bus(home: Path) -> bool:
    if os.name == "nt":
        return False
    cfg = home / "config.toml"
    try:
        if cfg.is_file():
            text = cfg.read_text(encoding="utf-8", errors="replace")
            if re.search(r'cli_auth_credentials_store\s*=\s*["\']keyring["\']', text):
                return not bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
    except Exception:
        pass
    return False


def _run_jsonrpc_stdio(
    argv: list[str],
    requests: list[dict[str, Any]],
    *,
    target_id: int = 2,
    timeout_s: float = 6.0,
    env: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, str, bool]:
    """Send JSON-RPC lines while keeping stdin OPEN until ``id == target_id`` arrives.

    Closing stdin immediately via ``communicate()`` causes ``codex app-server``
    to exit on EOF before its async HTTP ``rateLimits/read`` call completes.
    Returns ``(reply_dict_or_none, error_detail, timed_out)``.
    """
    proc: subprocess.Popen[str] | None = None
    matched: list[dict[str, Any]] = []
    stderr_lines: list[str] = []
    done_ev = threading.Event()

    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=dict(os.environ if env is None else env),
        )
    except Exception as exc:
        return None, f"spawn error: {type(exc).__name__}", False

    def _read_stdout() -> None:
        assert proc is not None and proc.stdout is not None
        try:
            for line in proc.stdout:
                s = line.strip()
                if not s.startswith("{"):
                    continue
                try:
                    msg = json.loads(s)
                except Exception:
                    continue
                if isinstance(msg, dict) and msg.get("id") == target_id:
                    matched.append(msg)
                    done_ev.set()
                    return
        except Exception:
            pass
        finally:
            done_ev.set()

    def _read_stderr() -> None:
        assert proc is not None and proc.stderr is not None
        try:
            for line in proc.stderr:
                s = line.strip()
                if s:
                    stderr_lines.append(s)
        except Exception:
            pass

    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    try:
        if proc.stdin is not None:
            for req in requests:
                proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
        finished = done_ev.wait(timeout=timeout_s)
        if not finished and not matched:
            return None, "stdio JSON-RPC timed out", True
        if matched:
            return matched[0], "", False
        tail = stderr_lines[-1] if stderr_lines else f"no JSON-RPC id={target_id} response"
        return None, tail, False
    except Exception as exc:
        return None, f"stdio error: {type(exc).__name__}", False
    finally:
        if proc is not None:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    proc.kill()
                    try:
                        proc.wait(timeout=0.5)
                    except Exception:
                        pass
            t_out.join(timeout=0.2)
            t_err.join(timeout=0.2)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass


def probe_codex_usage(
    *,
    env: dict[str, str] | None = None,
    timeout_s: float = 6.0,
    now: float | None = None,
    target_limit_id: str = "codex",
) -> tuple[dict[str, Any], str | None]:
    """Read-only zero-spend Codex ``account/rateLimits/read`` stdio RPC probe.

    Returns ``(reading, failure_kind)`` where ``failure_kind`` is ``None``,
    ``"transport"``, or ``"auth"``.
    """
    now_val = _now_ts(now)
    home = codex_home(env)
    pool_id = _codex_pool_id_from_home(home)
    binary = shutil.which("codex")
    if not binary:
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="openai", source="codex_app_server",
                freshness="unconfigured", now=now_val, detail="codex binary not found on PATH",
            ),
            None,
        )
    if _codex_uses_keyring_without_bus(home):
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="openai", source="codex_app_server",
                freshness="auth_locked", now=now_val,
                detail="codex configured for OS keyring without active DBus session",
            ),
            "transport",
        )

    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "mm-usage-feed", "version": "0.1.0"}},
        },
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "account/rateLimits/read",
            "params": {"excludeResetCreditDetails": True},
        },
    ]
    msg, err_detail, timed_out = _run_jsonrpc_stdio(
        [binary, "app-server", "--stdio"], requests, target_id=2, timeout_s=timeout_s, env=env
    )
    fetched_val = _now_ts(now)
    if msg is not None:
        if "error" in msg and msg["error"]:
            err_msg = str((msg["error"] or {}).get("message") or "rpc error")
            low = err_msg.lower()
            fkind = "auth" if ("auth" in low or "login" in low or "401" in low or "token" in low) else "transport"
            return (
                _unknown_pool_reading(
                    pool_id=pool_id, provider="openai", source="codex_app_server",
                    freshness="auth_locked" if fkind == "auth" else "error",
                    fetched_at=fetched_val, now=fetched_val, detail=err_msg[:180],
                ),
                fkind,
            )
        reading = parse_codex_rate_limits(
            msg, fetched_at=fetched_val, now=fetched_val,
            pool_id=pool_id, target_limit_id=target_limit_id,
        )
        return reading, None

    return (
        _unknown_pool_reading(
            pool_id=pool_id, provider="openai", source="codex_app_server",
            freshness="error", fetched_at=fetched_val, now=fetched_val,
            detail=("codex app-server timed out" if timed_out else err_detail[:180]),
        ),
        "transport",
    )


# ---------------------------------------------------------------------------
# 2. Grok CLI (1.0.77 verified: unified.jsonl tail + ACP _x.ai/billing)
# ---------------------------------------------------------------------------

def parse_grok_billing_config(
    payload: Any,
    *,
    fetched_at: float | None = None,
    now: float | None = None,
    ttl_s: float = GROK_TTL_S,
    pool_id: str = "grok:unknown",
    prior_bounds: tuple[str, str] | None = None,
    source: str = "grok_acp_billing",
) -> dict[str, Any]:
    """Normalize Grok ``_x.ai/billing`` or ``unified.jsonl`` ``latest_billing``."""
    now_val = _now_ts(now)
    if not isinstance(payload, dict):
        return _unknown_pool_reading(
            pool_id=pool_id, provider="xai", source=source,
            fetched_at=fetched_at, ttl_s=ttl_s, now=now_val, detail="non-object payload",
        )

    log_time_epoch: float | None = None
    body = payload
    if isinstance(payload.get("result"), dict):
        body = payload["result"]
    elif payload.get("msg") == "latest_billing" and isinstance(payload.get("ctx"), dict):
        log_time_epoch = parse_epoch(payload.get("time"))
        body = payload["ctx"]

    config = body.get("config") if isinstance(body.get("config"), dict) else None
    if config is None:
        # Reject unrelated payloads that carry no Grok billing config block
        if any(k in body for k in ("currentPeriod", "creditUsagePercent", "monthlyLimit")):
            config = body
        else:
            return _unknown_pool_reading(
                pool_id=pool_id, provider="xai", source=source,
                fetched_at=fetched_at, ttl_s=ttl_s, now=now_val,
                detail="missing Grok billing config",
            )

    fetched_val = (
        float(fetched_at)
        if fetched_at is not None
        else (log_time_epoch if log_time_epoch is not None else now_val)
    )

    tier_raw = (
        body.get("subscription_tier")
        or body.get("subscriptionTier")
        or config.get("subscription_tier")
        or config.get("subscriptionTier")
    )
    tier = str(tier_raw).strip() if isinstance(tier_raw, str) and tier_raw.strip() else None

    cur_period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    start_str = str(cur_period.get("start") or "").strip() or None
    end_str = str(cur_period.get("end") or "").strip() or None
    start_ep = parse_epoch(start_str)
    end_ep = parse_epoch(end_str)
    period_bounds = (start_str, end_str) if (start_str and end_str) else None

    # Enforce freshness FIRST: expired currentPeriod.end or expired TTL -> unknown
    if not is_window_fresh(fetched_val, end_ep, ttl_s, now_val):
        res = _unknown_pool_reading(
            pool_id=pool_id,
            provider="xai",
            source=source,
            plan=tier,
            freshness="stale",
            fetched_at=fetched_val,
            ttl_s=ttl_s,
            now=now_val,
            detail="Grok billing period or reading TTL has expired",
        )
        res["period_bounds"] = period_bounds
        return res

    used_pct = config.get("creditUsagePercent")
    legacy_used = config.get("used")
    legacy_limit = config.get("monthlyLimit")

    frac_left: float | None = None
    reported: dict[str, Any] | None = None
    detail: str | None = None

    if _is_num(used_pct):
        clamped = _clamp(float(used_pct), 0.0, 100.0)
        frac_left = round(1.0 - (clamped / 100.0), 6)
        reported = {"value": float(used_pct), "unit": "percent", "convention": "used"}
    elif _is_num(legacy_used) and _is_num(legacy_limit) and float(legacy_limit) > 0:
        pct = _clamp((float(legacy_used) / float(legacy_limit)) * 100.0, 0.0, 100.0)
        frac_left = round(1.0 - (pct / 100.0), 6)
        reported = {"value": round(pct, 4), "unit": "percent_derived_legacy", "convention": "used"}
    else:
        # Wave-3 3-way rule when creditUsagePercent is omitted by xAI:
        tier_low = (tier or "").lower()
        if "heavy" in tier_low:
            res = _unknown_pool_reading(
                pool_id=pool_id, provider="xai", source=source, plan=tier,
                freshness="missing", fetched_at=fetched_val, ttl_s=ttl_s, now=now_val,
                detail="SuperGrok Heavy omits creditUsagePercent on server",
            )
            res["period_bounds"] = period_bounds
            return res
        if tier_low == "free":
            frac_left = 0.0
            reported = {"value": 100.0, "unit": "percent_free_tier", "convention": "used"}
            detail = "Free tier has no subscription rolling quota"
        elif (
            tier
            and period_bounds is not None
            and prior_bounds is not None
            and len(prior_bounds) == 2
            and prior_bounds[0]
            and period_bounds != tuple(prior_bounds)
        ):
            prior_start_ep = parse_epoch(prior_bounds[0])
            if start_ep is not None and prior_start_ep is not None and start_ep > prior_start_ep:
                frac_left = 1.0
                reported = {
                    "value": 0.0,
                    "unit": "percent_inferred_rollover",
                    "convention": "used",
                }
                detail = "inferred fresh period after verified currentPeriod rollover"
            else:
                res = _unknown_pool_reading(
                    pool_id=pool_id, provider="xai", source=source, plan=tier,
                    freshness="missing", fetched_at=fetched_val, ttl_s=ttl_s, now=now_val,
                    detail="creditUsagePercent absent without forward period rollover",
                )
                res["period_bounds"] = period_bounds
                return res
        else:
            res = _unknown_pool_reading(
                pool_id=pool_id, provider="xai", source=source, plan=tier,
                freshness="missing", fetched_at=fetched_val, ttl_s=ttl_s, now=now_val,
                detail="creditUsagePercent absent (cold cache or suppressed by server)",
            )
            res["period_bounds"] = period_bounds
            return res

    resets_iso = epoch_to_iso(end_ep)
    win_entry = {
        "id": "rolling_period",
        "fraction_left": frac_left,
        "resets_at": resets_iso,
        "period_start": epoch_to_iso(start_ep),
        "is_fresh": True,
        "governs_pool": True,
        "used_fraction_reported": reported,
    }
    status = "exhausted" if frac_left <= 0.0 else "ok"
    return {
        "pool_id": pool_id,
        "provider": "xai",
        "meter_kind": "subscription_window",
        "status": status,
        "fraction_left": frac_left,
        "resets_at": resets_iso,
        "plan": tier,
        "source": source,
        "freshness": "fresh",
        "is_fresh": True,
        "fetched_at": epoch_to_iso(fetched_val),
        "fetched_at_epoch": fetched_val,
        "age_s": round(max(0.0, now_val - fetched_val), 1),
        "ttl_s": ttl_s,
        "used_fraction_reported": reported,
        "windows": [win_entry],
        "period_bounds": period_bounds,
        "money_left": None,
        "currency": None,
        "reset_credits": None,
        "detail": detail,
    }


def read_grok_unified_log(
    log_path: str | Path,
    *,
    tail_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Shrink-safe incremental reader for ``~/.grok/logs/unified.jsonl``."""
    p = Path(log_path)
    state = dict(tail_state or {})
    try:
        st = p.stat()
    except OSError:
        return None, {}

    inode = st.st_ino
    size = st.st_size
    prev_inode = state.get("inode")
    prev_offset = int(state.get("offset") or 0)
    prev_entry = state.get("latest_entry") if isinstance(state.get("latest_entry"), dict) else None

    if prev_inode != inode or prev_offset > size or prev_offset < 0:
        # File rotated or truncated: reset safely to tail window, drop stale offset
        start_offset = max(0, size - _GROK_TAIL_MAX_BYTES)
        prev_entry = None
    elif prev_offset == size and prev_entry is not None:
        return prev_entry, {"inode": inode, "offset": size, "latest_entry": prev_entry}
    else:
        start_offset = max(prev_offset, max(0, size - _GROK_TAIL_MAX_BYTES))

    latest = prev_entry
    try:
        with open(p, "rb") as fh:
            fh.seek(start_offset)
            chunk = fh.read()
            new_offset = fh.tell()
    except OSError:
        return prev_entry, state

    lines = chunk.splitlines()
    if start_offset > 0 and lines and prev_offset == 0:
        # Discard potentially partial first line when seeking mid-file
        lines = lines[1:]

    for raw_line in lines:
        if b"latest_billing" not in raw_line:
            continue
        try:
            obj = json.loads(raw_line.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("msg") == "latest_billing":
            latest = obj

    new_state = {"inode": inode, "offset": new_offset, "latest_entry": latest}
    return latest, new_state


def _grok_pool_id_from_home(home: Path) -> tuple[str, bool]:
    """Return ``(pool_id, token_looks_usable)`` without refreshing or writing anything."""
    auth_path = home / "auth.json"
    try:
        if auth_path.is_file():
            obj = json.loads(auth_path.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                tok = obj.get("grok_auth_token") or obj.get("access_token")
                if isinstance(tok, str) and tok.strip():
                    claims = decode_jwt_claims_unverified(tok)
                    ident = claims.get("sub") or claims.get("user_id") or tok[:24]
                    return f"grok:{pseudonymize_account_id(str(ident), 'grok')}", True
                # Grok 1.0.77 issuer-keyed format: {"https://auth.x.ai::...": {"user_id": ..., "key": ...}}
                for k, v in obj.items():
                    if isinstance(v, dict) and (v.get("key") or v.get("access_token") or v.get("user_id")):
                        ident = v.get("user_id") or v.get("principal_id") or k
                        return f"grok:{pseudonymize_account_id(str(ident), 'grok')}", True
    except Exception:
        pass
    return f"grok:{pseudonymize_account_id(str(home), 'grok-home')}", False


def probe_grok_acp_billing(
    *,
    env: dict[str, str] | None = None,
    timeout_s: float = 6.0,
    now: float | None = None,
    pool_id: str = "grok:unknown",
    prior_bounds: tuple[str, str] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Zero-turn ``grok agent stdio`` ``_x.ai/billing`` probe (verified on 1.0.77).

    Note: ``grok 1.0.77`` requires ``protocolVersion: 1`` (int) and ``params: {}``
    on ``_x.ai/billing`` (omitting ``params`` returns JSON-RPC ``-32602``).
    """
    now_val = _now_ts(now)
    binary = shutil.which("grok")
    if not binary:
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="xai", source="grok_acp_billing",
                freshness="unconfigured", now=now_val, detail="grok binary not found on PATH",
            ),
            None,
        )

    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientInfo": {"name": "mm-usage-feed", "version": "0.1.0"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "_x.ai/billing",
            "params": {},
        },
    ]
    msg, err_detail, timed_out = _run_jsonrpc_stdio(
        [binary, "agent", "stdio"], requests, target_id=2, timeout_s=timeout_s, env=env
    )
    fetched_val = _now_ts(now)
    if msg is not None:
        if "error" in msg and msg["error"]:
            err_msg = str((msg["error"] or {}).get("message") or "acp error")
            low = err_msg.lower()
            fkind = "auth" if ("auth" in low or "401" in low or "login" in low) else "transport"
            return (
                _unknown_pool_reading(
                    pool_id=pool_id, provider="xai", source="grok_acp_billing",
                    freshness="auth_locked" if fkind == "auth" else "error",
                    fetched_at=fetched_val, now=fetched_val, detail=err_msg[:180],
                ),
                fkind,
            )
        reading = parse_grok_billing_config(
            msg,
            fetched_at=fetched_val,
            now=fetched_val,
            pool_id=pool_id,
            prior_bounds=prior_bounds,
            source="grok_acp_billing",
        )
        return reading, None

    return (
        _unknown_pool_reading(
            pool_id=pool_id, provider="xai", source="grok_acp_billing",
            freshness="error", fetched_at=fetched_val, now=fetched_val,
            detail=("grok agent stdio timed out" if timed_out else err_detail[:180]),
        ),
        "transport",
    )


def probe_grok_usage(
    *,
    env: dict[str, str] | None = None,
    allow_active: bool = True,
    now: float | None = None,
    prior_bounds: tuple[str, str] | None = None,
    tail_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
    """Passive ``unified.jsonl`` check first; fallback to zero-spend ACP ``_x.ai/billing``."""
    now_val = _now_ts(now)
    home = grok_home(env)
    pool_id, has_auth = _grok_pool_id_from_home(home)
    log_path = home / "logs" / "unified.jsonl"
    latest_entry, new_tail_state = read_grok_unified_log(log_path, tail_state=tail_state)

    passive_reading: dict[str, Any] | None = None
    effective_prior = prior_bounds
    if latest_entry is not None:
        passive_reading = parse_grok_billing_config(
            latest_entry,
            now=now_val,
            pool_id=pool_id,
            prior_bounds=prior_bounds,
            source="grok_unified_log",
        )
        if passive_reading.get("status") in ("ok", "exhausted"):
            return passive_reading, None, new_tail_state
        pb = passive_reading.get("period_bounds")
        if effective_prior is None and isinstance(pb, tuple) and len(pb) == 2:
            effective_prior = pb

    if allow_active and has_auth:
        acp_reading, fkind = probe_grok_acp_billing(
            env=env, now=now_val, pool_id=pool_id, prior_bounds=effective_prior
        )
        return acp_reading, fkind, new_tail_state

    if passive_reading is not None:
        return passive_reading, None, new_tail_state

    return (
        _unknown_pool_reading(
            pool_id=pool_id,
            provider="xai",
            source="grok_unified_log",
            freshness="missing" if has_auth else "unconfigured",
            now=now_val,
            detail="no fresh Grok billing reading in unified.jsonl (refresh to query ACP)",
        ),
        None,
        new_tail_state,
    )


# ---------------------------------------------------------------------------
# 3. Claude Code CLI (2.1.310: statusLine tee 0-100 & rate_limit_event 0-1)
# ---------------------------------------------------------------------------

def parse_claude_statusline(
    payload: Any,
    *,
    fetched_at: float | None = None,
    now: float | None = None,
    ttl_s: float = CLAUDE_TTL_S,
    pool_id: str = "claude:local",
    plan_override: str | None = None,
) -> dict[str, Any]:
    """Parse Claude Code ``statusLine`` JSON (``rate_limits.*.used_percentage`` in 0..100)."""
    now_val = _now_ts(now)
    if not isinstance(payload, dict):
        return _unknown_pool_reading(
            pool_id=pool_id, provider="anthropic", source="claude_statusline_tee",
            plan=plan_override, ttl_s=ttl_s, now=now_val, detail="non-object payload",
        )

    fetched_val = (
        float(fetched_at)
        if fetched_at is not None
        else (parse_epoch(payload.get("fetched_at")) or now_val)
    )
    rl = payload.get("rate_limits")
    if not isinstance(rl, dict):
        return _unknown_pool_reading(
            pool_id=pool_id, provider="anthropic", source="claude_statusline_tee",
            plan=plan_override, fetched_at=fetched_val, ttl_s=ttl_s, now=now_val,
            detail="missing rate_limits object in Claude statusLine payload",
        )

    plan = plan_override or (str(payload.get("plan")) if payload.get("plan") else None)
    windows: list[dict[str, Any]] = []
    seen_any = False

    for raw_key, win_id, mins in (("five_hour", "5h", 300), ("seven_day", "7d", 10080)):
        block = rl.get(raw_key)
        if not isinstance(block, dict):
            continue
        used_pct = block.get("used_percentage")
        if not _is_num(used_pct):
            continue
        seen_any = True
        clamped = _clamp(float(used_pct), 0.0, 100.0)
        left = round(1.0 - (clamped / 100.0), 6)
        reset_ep = parse_epoch(block.get("resets_at"))
        fresh = is_window_fresh(fetched_val, reset_ep, ttl_s, now_val)
        windows.append({
            "id": win_id,
            "window_minutes": mins,
            "fraction_left": left if fresh else None,
            "resets_at": epoch_to_iso(reset_ep),
            "is_fresh": fresh,
            "governs_pool": True,
            "used_fraction_reported": {
                "value": float(used_pct),
                "unit": "percent",
                "convention": "used",
            },
        })

    fresh_wins = [w for w in windows if w["is_fresh"] and w["fraction_left"] is not None]
    if not fresh_wins:
        return _unknown_pool_reading(
            pool_id=pool_id,
            provider="anthropic",
            source="claude_statusline_tee",
            plan=plan,
            freshness="stale" if seen_any else "missing",
            fetched_at=fetched_val,
            ttl_s=ttl_s,
            now=now_val,
            windows=windows,
            detail="Claude statusLine windows expired or absent",
        )

    gov = min(fresh_wins, key=lambda w: float(w["fraction_left"]))
    frac_left = float(gov["fraction_left"])
    return {
        "pool_id": pool_id,
        "provider": "anthropic",
        "meter_kind": "subscription_window",
        "status": "exhausted" if frac_left <= 0.0 else "ok",
        "fraction_left": frac_left,
        "resets_at": gov["resets_at"],
        "plan": plan,
        "source": "claude_statusline_tee",
        "freshness": "fresh",
        "is_fresh": True,
        "fetched_at": epoch_to_iso(fetched_val),
        "fetched_at_epoch": fetched_val,
        "age_s": round(max(0.0, now_val - fetched_val), 1),
        "ttl_s": ttl_s,
        "used_fraction_reported": gov["used_fraction_reported"],
        "windows": windows,
        "money_left": None,
        "currency": None,
        "reset_credits": None,
        "detail": None,
    }


def parse_claude_rate_limit_event(
    payload: Any,
    *,
    fetched_at: float | None = None,
    now: float | None = None,
    ttl_s: float = CLAUDE_TTL_S,
    pool_id: str = "claude:local",
    plan_override: str | None = None,
) -> dict[str, Any]:
    """Parse Claude ``rate_limit_event`` NDJSON object (``utilization`` in 0.0..1.0)."""
    now_val = _now_ts(now)
    if not isinstance(payload, dict):
        return _unknown_pool_reading(
            pool_id=pool_id, provider="anthropic", source="claude_stream_harvest",
            plan=plan_override, ttl_s=ttl_s, now=now_val, detail="non-object payload",
        )

    fetched_val = (
        float(fetched_at)
        if fetched_at is not None
        else (parse_epoch(payload.get("fetched_at")) or now_val)
    )
    info = (
        payload.get("rate_limit_info")
        if isinstance(payload.get("rate_limit_info"), dict)
        else (payload if "unifiedWindows" in payload or "utilization" in payload else None)
    )
    if not isinstance(info, dict):
        return _unknown_pool_reading(
            pool_id=pool_id, provider="anthropic", source="claude_stream_harvest",
            plan=plan_override, fetched_at=fetched_val, ttl_s=ttl_s, now=now_val,
            detail="missing rate_limit_info",
        )

    windows: list[dict[str, Any]] = []
    seen_any = False

    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        for wkey, wobj in unified.items():
            if not isinstance(wobj, dict):
                continue
            util = wobj.get("utilization")
            # Reject > 1.0 so 0..100 values are never silently mis-parsed as 0..1 ratios!
            if not _is_num(util) or float(util) < 0.0 or float(util) > 1.0:
                continue
            seen_any = True
            left = round(1.0 - float(util), 6)
            reset_ep = parse_epoch(wobj.get("reset") or wobj.get("resetsAt") or wobj.get("resets_at"))
            fresh = is_window_fresh(fetched_val, reset_ep, ttl_s, now_val)
            windows.append({
                "id": str(wkey),
                "fraction_left": left if fresh else None,
                "resets_at": epoch_to_iso(reset_ep),
                "is_fresh": fresh,
                "governs_pool": True,
                "used_fraction_reported": {
                    "value": float(util),
                    "unit": "ratio",
                    "convention": "used",
                },
            })

    if not windows and _is_num(info.get("utilization")):
        util = float(info["utilization"])
        if 0.0 <= util <= 1.0:
            seen_any = True
            left = round(1.0 - util, 6)
            reset_ep = parse_epoch(info.get("resetsAt") or info.get("reset") or info.get("resets_at"))
            fresh = is_window_fresh(fetched_val, reset_ep, ttl_s, now_val)
            rtype = str(info.get("rateLimitType") or "primary")
            win_id = "5h" if "five" in rtype or "5h" in rtype else ("7d" if "seven" in rtype or "7d" in rtype else rtype)
            windows.append({
                "id": win_id,
                "fraction_left": left if fresh else None,
                "resets_at": epoch_to_iso(reset_ep),
                "is_fresh": fresh,
                "governs_pool": True,
                "used_fraction_reported": {
                    "value": util,
                    "unit": "ratio",
                    "convention": "used",
                },
            })

    fresh_wins = [w for w in windows if w["is_fresh"] and w["fraction_left"] is not None]
    if not fresh_wins:
        return _unknown_pool_reading(
            pool_id=pool_id,
            provider="anthropic",
            source="claude_stream_harvest",
            plan=plan_override,
            freshness="stale" if seen_any else "missing",
            fetched_at=fetched_val,
            ttl_s=ttl_s,
            now=now_val,
            windows=windows,
            detail="Claude rate_limit_event windows expired or invalid",
        )

    gov = min(fresh_wins, key=lambda w: float(w["fraction_left"]))
    frac_left = float(gov["fraction_left"])
    return {
        "pool_id": pool_id,
        "provider": "anthropic",
        "meter_kind": "subscription_window",
        "status": "exhausted" if frac_left <= 0.0 else "ok",
        "fraction_left": frac_left,
        "resets_at": gov["resets_at"],
        "plan": plan_override,
        "source": "claude_stream_harvest",
        "freshness": "fresh",
        "is_fresh": True,
        "fetched_at": epoch_to_iso(fetched_val),
        "fetched_at_epoch": fetched_val,
        "age_s": round(max(0.0, now_val - fetched_val), 1),
        "ttl_s": ttl_s,
        "used_fraction_reported": gov["used_fraction_reported"],
        "windows": windows,
        "money_left": None,
        "currency": None,
        "reset_credits": None,
        "detail": None,
    }


def _atomic_write_json_0600(dest: Path, obj: dict[str, Any]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=str(dest.parent))
    try:
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
        os.replace(tmp_name, dest)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def write_claude_statusline_tee(
    stdin_text: str,
    cache_path: str | Path,
    *,
    chain_cmd: str | None = None,
    now: float | None = None,
) -> tuple[bool, str]:
    """Sanitize and persist Claude ``statusLine`` quota JSON (0600); optionally chain."""
    now_val = _now_ts(now)
    wrote = False
    try:
        raw = json.loads(stdin_text or "")
    except Exception:
        raw = None

    if isinstance(raw, dict) and isinstance(raw.get("rate_limits"), dict):
        # Strip workspace paths, transcripts, and session IDs — store quota fields only!
        model_block = raw.get("model") if isinstance(raw.get("model"), dict) else {}
        sanitized = {
            "fetched_at": epoch_to_iso(now_val),
            "version": raw.get("version"),
            "model_id": model_block.get("id"),
            "rate_limits": raw["rate_limits"],
        }
        try:
            _atomic_write_json_0600(Path(cache_path), sanitized)
            wrote = True
        except Exception:
            wrote = False

    passthrough_out = ""
    if chain_cmd:
        try:
            import shlex

            argv = shlex.split(chain_cmd, posix=(os.name != "nt"))
            if argv:
                proc = subprocess.run(
                    argv,
                    input=stdin_text,
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                passthrough_out = proc.stdout or ""
        except Exception:
            passthrough_out = ""
    return wrote, passthrough_out


def harvest_claude_output(
    raw_stdout: str,
    data_dir: str | Path | None = None,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Opportunistically harvest ``rate_limit_event`` or ``rate_limits`` from Claude stdout."""
    if not raw_stdout:
        return None
    now_val = _now_ts(now)
    latest_event: dict[str, Any] | None = None
    latest_status: dict[str, Any] | None = None

    for line in raw_stdout.splitlines():
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            obj = json.loads(s)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "rate_limit_event" or isinstance(obj.get("rate_limit_info"), dict):
            latest_event = obj
        elif isinstance(obj.get("rate_limits"), dict):
            latest_status = obj

    target_dir = Path(data_dir) if data_dir is not None else None
    if target_dir is None:
        env_dir = os.environ.get("HCL_DATA_DIR")
        if env_dir:
            target_dir = Path(env_dir)

    if latest_event is not None:
        to_save = {
            "fetched_at": epoch_to_iso(now_val),
            "type": "rate_limit_event",
            "rate_limit_info": latest_event.get("rate_limit_info") or latest_event,
        }
        if target_dir is not None and target_dir.is_dir():
            try:
                _atomic_write_json_0600(target_dir / CLAUDE_HARVEST_FILENAME, to_save)
            except Exception:
                pass
        return parse_claude_rate_limit_event(to_save, fetched_at=now_val, now=now_val)

    if latest_status is not None:
        to_save = {
            "fetched_at": epoch_to_iso(now_val),
            "rate_limits": latest_status["rate_limits"],
        }
        if target_dir is not None and target_dir.is_dir():
            try:
                _atomic_write_json_0600(target_dir / CLAUDE_HARVEST_FILENAME, to_save)
            except Exception:
                pass
        return parse_claude_statusline(to_save, fetched_at=now_val, now=now_val)

    return None


def _claude_auth_plan_readonly(env: dict[str, str] | None = None, timeout_s: float = 4.0) -> tuple[str | None, bool]:
    binary = shutil.which("claude")
    if not binary:
        return None, False
    try:
        proc = subprocess.run(
            [binary, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=dict(os.environ if env is None else env),
        )
        obj = json.loads(proc.stdout or "{}")
        if isinstance(obj, dict):
            logged_in = bool(obj.get("loggedIn"))
            sub = obj.get("subscriptionType")
            return (str(sub) if sub else None), logged_in
    except Exception:
        pass
    return None, True


def probe_claude_usage(
    *,
    data_dir: str | Path | None = None,
    env: dict[str, str] | None = None,
    allow_auth_status: bool = True,
    now: float | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Read passive Claude tee / harvest files; optionally enrich plan via ``claude auth status``."""
    now_val = _now_ts(now)
    cdir = claude_config_dir(env)
    pool_id = f"claude:{pseudonymize_account_id(str(cdir), 'claude-home')}"

    plan: str | None = None
    logged_in = (cdir / ".credentials.json").is_file() or bool(shutil.which("claude"))
    if allow_auth_status and shutil.which("claude"):
        plan, logged_in = _claude_auth_plan_readonly(env=env)

    candidates: list[tuple[float, dict[str, Any]]] = []
    stale_seen: dict[str, Any] | None = None

    search_dirs: list[Path] = []
    if data_dir is not None:
        search_dirs.append(Path(data_dir))
    if cdir.is_dir():
        search_dirs.append(cdir)

    for sdir in search_dirs:
        for fname in (CLAUDE_STATUSLINE_FILENAME, CLAUDE_HARVEST_FILENAME):
            fpath = sdir / fname
            if not fpath.is_file():
                continue
            try:
                raw = json.loads(fpath.read_text(encoding="utf-8"))
                st_mtime = fpath.stat().st_mtime
            except Exception:
                continue
            if not isinstance(raw, dict):
                continue
            f_ep = parse_epoch(raw.get("fetched_at")) or st_mtime
            if "rate_limits" in raw:
                parsed = parse_claude_statusline(
                    raw, fetched_at=f_ep, now=now_val, pool_id=pool_id, plan_override=plan
                )
            else:
                parsed = parse_claude_rate_limit_event(
                    raw, fetched_at=f_ep, now=now_val, pool_id=pool_id, plan_override=plan
                )
            if parsed.get("status") in ("ok", "exhausted"):
                candidates.append((f_ep, parsed))
            elif stale_seen is None:
                stale_seen = parsed

    if candidates:
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return candidates[0][1], None

    if stale_seen is not None:
        return stale_seen, None

    if not logged_in:
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="anthropic", source="claude_statusline_tee",
                plan=plan, freshness="unconfigured", ttl_s=CLAUDE_TTL_S, now=now_val,
                detail="Claude CLI not logged in",
            ),
            None,
        )

    return (
        _unknown_pool_reading(
            pool_id=pool_id,
            provider="anthropic",
            source="claude_statusline_tee",
            plan=plan,
            freshness="missing",
            ttl_s=CLAUDE_TTL_S,
            now=now_val,
            detail="no Claude statusLine tee or stream harvest yet (wire `hermes critic claude-statusline-tee`)",
        ),
        None,
    )


# ---------------------------------------------------------------------------
# 4. Antigravity CLI (1.5.5 verified: version gate >= 1.1.11 + /usage json)
# ---------------------------------------------------------------------------

def check_agy_version_gate(version_text: str | None) -> tuple[bool, tuple[int, int, int] | None, str]:
    """Hard billing guard: ``agy <= 1.1.10`` dispatched ``/usage`` as a paid model prompt."""
    text = (version_text or "").strip()
    m = _SEMVER_RE.search(text)
    if not m:
        return False, None, f"cannot parse agy version from {text!r}"
    ver = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if ver < AGY_MIN_SAFE_VERSION:
        return (
            False,
            ver,
            f"agy {ver[0]}.{ver[1]}.{ver[2]} < 1.1.11: /usage headless probe blocked to prevent paid model turn",
        )
    return True, ver, f"agy {ver[0]}.{ver[1]}.{ver[2]} >= 1.1.11"


def parse_agy_usage_json(
    payload: Any,
    *,
    fetched_at: float | None = None,
    now: float | None = None,
    ttl_s: float = AGY_TTL_S,
    pool_id: str = "agy:os-keyring",
    group_filter: str | None = "gemini",
) -> dict[str, Any]:
    """Normalize ``agy -p "/usage" --output-format json`` (``remaining_fraction`` is LEFT)."""
    now_val = _now_ts(now)
    fetched_val = _now_ts(fetched_at) if fetched_at is not None else now_val

    if not isinstance(payload, dict):
        return _unknown_pool_reading(
            pool_id=pool_id, provider="google", source="agy_print_usage",
            fetched_at=fetched_val, ttl_s=ttl_s, now=now_val, detail="non-object payload",
        )

    if "status" in payload and payload.get("status") != "SUCCESS":
        return _unknown_pool_reading(
            pool_id=pool_id, provider="google", source="agy_print_usage",
            fetched_at=fetched_val, ttl_s=ttl_s, now=now_val,
            detail=f"agy /usage returned status={payload.get('status')!r}",
        )

    if "num_turns" in payload and payload.get("num_turns") != 0:
        return _unknown_pool_reading(
            pool_id=pool_id, provider="google", source="agy_print_usage",
            fetched_at=fetched_val, ttl_s=ttl_s, now=now_val,
            detail=f"agy /usage num_turns={payload.get('num_turns')} != 0 (unexpected model turn)",
        )

    # Support BOTH agy 1.5.5 actual print-mode shape (`command.data.groups` with `name`)
    # AND statusLine / legacy shape (`quota` with `group_name`)!
    quota = payload.get("quota")
    if not isinstance(quota, list):
        cmd_obj = payload.get("command") if isinstance(payload.get("command"), dict) else {}
        cmd_data = cmd_obj.get("data") if isinstance(cmd_obj.get("data"), dict) else {}
        quota = cmd_data.get("groups")

    if not isinstance(quota, list) or not quota:
        return _unknown_pool_reading(
            pool_id=pool_id, provider="google", source="agy_print_usage",
            fetched_at=fetched_val, ttl_s=ttl_s, now=now_val, detail="missing quota/groups array",
        )

    # Filter out the broken pre-1.3.13 "All Models" fallback group
    valid_groups = [
        g for g in quota
        if isinstance(g, dict)
        and str(g.get("group_name") or g.get("name") or "").strip().lower() != "all models"
    ]
    if not valid_groups:
        return _unknown_pool_reading(
            pool_id=pool_id, provider="google", source="agy_print_usage",
            fetched_at=fetched_val, ttl_s=ttl_s, now=now_val,
            detail="only fallback 'All Models' group returned (empty server quota)",
        )

    windows: list[dict[str, Any]] = []
    buckets_by_group: dict[str, list[dict[str, Any]]] = {}
    seen_any = False
    gf_low = group_filter.lower() if group_filter else None

    for grp in valid_groups:
        gname = str(grp.get("group_name") or grp.get("name") or "unknown")
        g_matches = (
            True
            if gf_low is None
            else (gf_low in gname.lower())
        )
        raw_buckets = grp.get("buckets")
        if not isinstance(raw_buckets, list):
            continue
        for b in raw_buckets:
            if not isinstance(b, dict):
                continue
            bid = str(b.get("id") or b.get("label") or b.get("name") or "window")
            reset_ep = parse_epoch(b.get("reset_time") or b.get("resetTime"))
            raw_rem = b.get("remaining_fraction")
            if raw_rem is None and "remainingFraction" in b:
                raw_rem = b.get("remainingFraction")

            # Proto3 zero-omission rule (Wave 2 report 04): omitted remaining_fraction
            # WITH a valid future reset_time means exact 0.0 remaining!
            if _is_num(raw_rem):
                seen_any = True
                rem = round(_clamp(float(raw_rem), 0.0, 1.0), 6)
                unit_tag = "ratio"
            elif raw_rem is None and reset_ep is not None and reset_ep > now_val:
                seen_any = True
                rem = 0.0
                unit_tag = "ratio_proto3_omitted_zero"
            else:
                continue

            fresh = is_window_fresh(fetched_val, reset_ep, ttl_s, now_val)
            w_entry = {
                "id": bid,
                "label": b.get("label") or b.get("name"),
                "group_name": gname,
                # CRITICAL POLARITY: Antigravity remaining_fraction is ALREADY LEFT (0-1).
                "fraction_left": rem if fresh else None,
                "resets_at": epoch_to_iso(reset_ep),
                "is_fresh": fresh,
                "governs_pool": g_matches,
                "used_fraction_reported": {
                    "value": float(raw_rem) if _is_num(raw_rem) else 0.0,
                    "unit": unit_tag,
                    "convention": "left",
                },
            }
            windows.append(w_entry)
            buckets_by_group.setdefault(gname, []).append(w_entry)

    gov_candidates = [
        w for w in windows
        if w.get("governs_pool") and w.get("is_fresh") and w.get("fraction_left") is not None
    ]
    if not gov_candidates and gf_low is not None:
        # Fallback if group names changed upstream
        gov_candidates = [
            w for w in windows if w.get("is_fresh") and w.get("fraction_left") is not None
        ]

    if not gov_candidates:
        res = _unknown_pool_reading(
            pool_id=pool_id,
            provider="google",
            source="agy_print_usage",
            freshness="stale" if seen_any else "missing",
            fetched_at=fetched_val,
            ttl_s=ttl_s,
            now=now_val,
            windows=windows,
            detail="Antigravity quota buckets expired or missing",
        )
        res["buckets_by_group"] = buckets_by_group
        return res

    gov = min(gov_candidates, key=lambda w: float(w["fraction_left"]))
    frac_left = float(gov["fraction_left"])
    return {
        "pool_id": pool_id,
        "provider": "google",
        "meter_kind": "subscription_window",
        "status": "exhausted" if frac_left <= 0.0 else "ok",
        "fraction_left": frac_left,
        "resets_at": gov["resets_at"],
        "plan": "google_ai_pro_or_ultra",
        "source": "agy_print_usage",
        "freshness": "fresh",
        "is_fresh": True,
        "fetched_at": epoch_to_iso(fetched_val),
        "fetched_at_epoch": fetched_val,
        "age_s": round(max(0.0, now_val - fetched_val), 1),
        "ttl_s": ttl_s,
        "used_fraction_reported": gov["used_fraction_reported"],
        "windows": windows,
        "buckets_by_group": buckets_by_group,
        "money_left": None,
        "currency": None,
        "reset_credits": None,
        "detail": None,
    }


def parse_agy_statusline(
    payload: Any,
    *,
    fetched_at: float | None = None,
    now: float | None = None,
    ttl_s: float = AGY_TTL_S,
    pool_id: str = "agy:os-keyring",
) -> dict[str, Any]:
    """Parse Antigravity statusLine JSON envelope when configured."""
    if isinstance(payload, dict) and isinstance(payload.get("quota"), list):
        sub = dict(payload)
        sub.setdefault("status", "SUCCESS")
        sub.setdefault("num_turns", 0)
        res = parse_agy_usage_json(sub, fetched_at=fetched_at, now=now, ttl_s=ttl_s, pool_id=pool_id)
        res["source"] = "agy_statusline_tee"
        return res
    return _unknown_pool_reading(
        pool_id=pool_id, provider="google", source="agy_statusline_tee",
        fetched_at=fetched_at, ttl_s=ttl_s, now=now, detail="missing quota array in agy statusLine",
    )


def probe_agy_usage(
    *,
    env: dict[str, str] | None = None,
    timeout_s: float = 8.0,
    now: float | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Enforce ``agy >= 1.1.11`` then invoke ``agy -p "/usage" --output-format json``."""
    now_val = _now_ts(now)
    pool_id = "agy:os-keyring"
    binary = shutil.which("agy")
    if not binary:
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="google", source="agy_print_usage",
                freshness="unconfigured", now=now_val, detail="agy binary not found on PATH",
            ),
            None,
        )

    try:
        vproc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=4.0,
            env=dict(os.environ if env is None else env),
        )
        ver_out = (vproc.stdout or "") + " " + (vproc.stderr or "")
    except Exception as exc:
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="google", source="agy_print_usage",
                freshness="error", now=now_val, detail=f"agy --version failed: {type(exc).__name__}",
            ),
            "transport",
        )

    ok_ver, _parsed_ver, ver_reason = check_agy_version_gate(ver_out)
    if not ok_ver:
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="google", source="agy_print_usage",
                freshness="unconfigured", now=now_val, detail=ver_reason,
            ),
            None,
        )

    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            [binary, "-p", "/usage", "--output-format", "json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ if env is None else env),
        )
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        if proc is not None:
            proc.kill()
            try:
                proc.communicate(timeout=1.0)
            except Exception:
                pass
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="google", source="agy_print_usage",
                freshness="auth_locked", now=now_val,
                detail="agy -p /usage timed out (OS keyring likely locked in headless scope)",
            ),
            "transport",
        )
    except Exception as exc:
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="google", source="agy_print_usage",
                freshness="error", now=now_val, detail=f"agy spawn failed: {type(exc).__name__}",
            ),
            "transport",
        )
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()

    fetched_val = _now_ts(now)
    if proc.returncode != 0:
        err_text = ((stderr or "") + " " + (stdout or "")).strip()
        low = err_text.lower()
        is_auth = "login" in low or "auth" in low or "credential" in low or "keyring" in low
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="google", source="agy_print_usage",
                freshness="auth_locked" if is_auth else "error",
                fetched_at=fetched_val, now=fetched_val,
                detail=(err_text[:180] or f"agy exited {proc.returncode}"),
            ),
            "auth" if is_auth else "transport",
        )

    try:
        obj = json.loads(stdout or "")
    except Exception:
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="google", source="agy_print_usage",
                freshness="error", fetched_at=fetched_val, now=fetched_val,
                detail="agy /usage output was not valid JSON",
            ),
            "transport",
        )
    return parse_agy_usage_json(obj, fetched_at=fetched_val, now=fetched_val, pool_id=pool_id), None


# ---------------------------------------------------------------------------
# 5. Kimi Code CLI (post-2026-09-15 usages object + legacy usage/limits rows)
# ---------------------------------------------------------------------------

def _parse_float_str(val: Any) -> float | None:
    if _is_num(val):
        return float(val)
    if isinstance(val, str) and val.strip():
        try:
            f = float(val.strip())
            return f if math.isfinite(f) else None
        except ValueError:
            return None
    return None


def parse_kimi_usages(
    payload: Any,
    *,
    fetched_at: float | None = None,
    now: float | None = None,
    ttl_s: float = KIMI_TTL_S,
    pool_id: str = "kimi:unknown",
) -> dict[str, Any]:
    """Parse Kimi ``/usages`` in either post-2026-09-15 ``usages`` or legacy ``limits[]`` form."""
    now_val = _now_ts(now)
    fetched_val = _now_ts(fetched_at) if fetched_at is not None else now_val

    if not isinstance(payload, dict):
        return _unknown_pool_reading(
            pool_id=pool_id, provider="moonshot", source="kimi_usages_api",
            fetched_at=fetched_val, ttl_s=ttl_s, now=now_val, detail="non-object payload",
        )

    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    windows: list[dict[str, Any]] = []
    seen_any = False
    plan: str | None = None
    freeze_exhausted = False

    user_obj = body.get("user") if isinstance(body.get("user"), dict) else {}
    membership = user_obj.get("membership") if isinstance(user_obj.get("membership"), dict) else {}
    if membership.get("level"):
        plan = str(membership["level"])

    # Schema A (post-2026-09-15): {"usages": {"limit_5h": {"used_ratio": 0.35, ...}, "parallel": ..., "totalQuota": ...}}
    usages_map = body.get("usages") if isinstance(body.get("usages"), dict) else None
    if usages_map is not None:
        tq = usages_map.get("totalQuota")
        if isinstance(tq, dict):
            # Ignore sticky placeholder remaining="99"; check binary freeze flag used > 0!
            tq_used = _parse_float_str(tq.get("used"))
            if tq_used is not None and tq_used > 0:
                freeze_exhausted = True

        for k, v in usages_map.items():
            if k in ("parallel", "totalQuota") or not isinstance(v, dict):
                continue
            ur = _parse_float_str(v.get("used_ratio") if "used_ratio" in v else v.get("usedRatio"))
            if ur is None or ur < 0.0 or ur > 1.0:
                continue
            seen_any = True
            left = round(1.0 - _clamp(ur, 0.0, 1.0), 6)
            reset_ep = parse_epoch(v.get("reset_at") or v.get("resetAt") or v.get("resetTime"))
            fresh = is_window_fresh(fetched_val, reset_ep, ttl_s, now_val)
            win_id = k.replace("limit_", "") if k.startswith("limit_") else k
            windows.append({
                "id": win_id,
                "fraction_left": (0.0 if freeze_exhausted else left) if fresh else None,
                "resets_at": epoch_to_iso(reset_ep),
                "is_fresh": fresh,
                "governs_pool": True,
                "used_fraction_reported": {
                    "value": ur,
                    "unit": "ratio",
                    "convention": "used",
                },
            })

    # Schema B (legacy rows): {"usage": {"limit": "100", "used": "25", ...}, "limits": [...]}
    if not windows and (isinstance(body.get("usage"), dict) or isinstance(body.get("limits"), list)):
        def _row_to_win(detail_obj: dict[str, Any], wid: str) -> None:
            nonlocal seen_any
            lim = _parse_float_str(detail_obj.get("limit"))
            used = _parse_float_str(detail_obj.get("used"))
            rem = _parse_float_str(detail_obj.get("remaining"))
            if lim is None or lim <= 0:
                return
            if used is not None:
                ratio = _clamp(used / lim, 0.0, 1.0)
                rep = {"value": ratio, "unit": "ratio_derived_used", "convention": "used"}
            elif rem is not None:
                ratio = _clamp(1.0 - (rem / lim), 0.0, 1.0)
                rep = {"value": ratio, "unit": "ratio_derived_remaining", "convention": "used"}
            else:
                return
            seen_any = True
            left = round(1.0 - ratio, 6)
            reset_ep = parse_epoch(detail_obj.get("resetTime") or detail_obj.get("reset_at"))
            fresh = is_window_fresh(fetched_val, reset_ep, ttl_s, now_val)
            windows.append({
                "id": wid,
                "fraction_left": left if fresh else None,
                "resets_at": epoch_to_iso(reset_ep),
                "is_fresh": fresh,
                "governs_pool": True,
                "used_fraction_reported": rep,
            })

        if isinstance(body.get("usage"), dict):
            _row_to_win(body["usage"], "weekly")
        for idx, item in enumerate(body.get("limits") or []):
            if not isinstance(item, dict):
                continue
            det = item.get("detail") if isinstance(item.get("detail"), dict) else item
            win_meta = item.get("window") if isinstance(item.get("window"), dict) else {}
            dur = win_meta.get("duration")
            unit = str(win_meta.get("timeUnit") or "")
            wid = f"{dur}m" if isinstance(dur, int) and "MINUTE" in unit else f"limit_{idx}"
            if wid == "300m":
                wid = "5h"
            _row_to_win(det, wid)

    if freeze_exhausted and not windows:
        return {
            "pool_id": pool_id,
            "provider": "moonshot",
            "meter_kind": "subscription_window",
            "status": "exhausted",
            "fraction_left": 0.0,
            "resets_at": None,
            "plan": plan,
            "source": "kimi_usages_api",
            "freshness": "fresh",
            "is_fresh": True,
            "fetched_at": epoch_to_iso(fetched_val),
            "fetched_at_epoch": fetched_val,
            "age_s": round(max(0.0, now_val - fetched_val), 1),
            "ttl_s": ttl_s,
            "used_fraction_reported": {"value": 1.0, "unit": "total_quota_freeze", "convention": "used"},
            "windows": [],
            "money_left": None,
            "currency": None,
            "reset_credits": None,
            "detail": "Kimi totalQuota freeze flag active (used > 0)",
        }

    fresh_wins = [w for w in windows if w["is_fresh"] and w["fraction_left"] is not None]
    if not fresh_wins:
        return _unknown_pool_reading(
            pool_id=pool_id,
            provider="moonshot",
            source="kimi_usages_api",
            plan=plan,
            freshness="stale" if seen_any else "missing",
            fetched_at=fetched_val,
            ttl_s=ttl_s,
            now=now_val,
            windows=windows,
            detail="Kimi usage windows expired or absent",
        )

    gov = min(fresh_wins, key=lambda w: float(w["fraction_left"]))
    frac_left = 0.0 if freeze_exhausted else float(gov["fraction_left"])
    return {
        "pool_id": pool_id,
        "provider": "moonshot",
        "meter_kind": "subscription_window",
        "status": "exhausted" if frac_left <= 0.0 else "ok",
        "fraction_left": frac_left,
        "resets_at": gov["resets_at"],
        "plan": plan,
        "source": "kimi_usages_api",
        "freshness": "fresh",
        "is_fresh": True,
        "fetched_at": epoch_to_iso(fetched_val),
        "fetched_at_epoch": fetched_val,
        "age_s": round(max(0.0, now_val - fetched_val), 1),
        "ttl_s": ttl_s,
        "used_fraction_reported": gov["used_fraction_reported"],
        "windows": windows,
        "money_left": None,
        "currency": None,
        "reset_credits": None,
        "detail": "Kimi totalQuota freeze active" if freeze_exhausted else None,
    }


def probe_kimi_usage(
    *,
    env: dict[str, str] | None = None,
    timeout_s: float = 5.0,
    now: float | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Read-only Kimi ``/usages`` GET using an existing non-expired token (NEVER refreshes)."""
    now_val = _now_ts(now)
    home = kimi_code_home(env)
    cred_path = home / "credentials" / "kimi-code.json"
    pool_id = f"kimi:{pseudonymize_account_id(str(home), 'kimi-home')}"
    if not cred_path.is_file():
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="moonshot", source="kimi_usages_api",
                freshness="unconfigured", now=now_val, detail="kimi-code.json credentials absent",
            ),
            None,
        )
    try:
        cred = json.loads(cred_path.read_text(encoding="utf-8"))
    except Exception:
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="moonshot", source="kimi_usages_api",
                freshness="error", now=now_val, detail="cannot parse kimi-code.json",
            ),
            "auth",
        )
    tok = cred.get("access_token") if isinstance(cred, dict) else None
    exp = parse_epoch(cred.get("expires_at")) if isinstance(cred, dict) else None
    if not isinstance(tok, str) or not tok.strip():
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="moonshot", source="kimi_usages_api",
                freshness="auth_locked", now=now_val, detail="missing Kimi access_token",
            ),
            "auth",
        )
    pool_id = f"kimi:{pseudonymize_account_id(tok[:24], 'kimi')}"
    # Hard rule R2: NEVER refresh Kimi OAuth tokens (rotation invalidates CLI session)!
    if exp is not None and now_val >= (exp - 30.0):
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="moonshot", source="kimi_usages_api",
                freshness="auth_locked", now=now_val,
                detail="Kimi access_token expired (read-only rule: never refresh CLI token)",
            ),
            "auth",
        )

    e = env if env is not None else os.environ
    base_url = (e.get("KIMI_CODE_BASE_URL") or "https://api.kimi.com/coding/v1").rstrip("/")
    req = urllib.request.Request(
        f"{base_url}/usages",
        headers={"Authorization": f"Bearer {tok.strip()}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
        fetched_val = _now_ts(now)
        return parse_kimi_usages(raw, fetched_at=fetched_val, now=fetched_val, pool_id=pool_id), None
    except urllib.error.HTTPError as exc:
        fkind = "auth" if exc.code in (401, 403) else "transport"
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="moonshot", source="kimi_usages_api",
                freshness="auth_locked" if fkind == "auth" else "error",
                now=now_val, detail=f"Kimi /usages HTTP {exc.code}",
            ),
            fkind,
        )
    except Exception as exc:
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="moonshot", source="kimi_usages_api",
                freshness="error", now=now_val, detail=f"Kimi /usages error: {type(exc).__name__}",
            ),
            "transport",
        )


# ---------------------------------------------------------------------------
# 6. Prepaid API Balances, Local Ollama, and Hermes Account Usage Cache
# ---------------------------------------------------------------------------

def parse_deepseek_balance(
    payload: Any,
    *,
    fetched_at: float | None = None,
    now: float | None = None,
    ttl_s: float = API_TTL_S,
    pool_id: str = "api:deepseek:unknown",
) -> dict[str, Any]:
    """Parse DeepSeek ``GET /user/balance`` (prepaid currency balance, no % denominator)."""
    now_val = _now_ts(now)
    fetched_val = _now_ts(fetched_at) if fetched_at is not None else now_val
    if not is_window_fresh(fetched_val, None, ttl_s, now_val):
        return _unknown_pool_reading(
            pool_id=pool_id, provider="deepseek", source="deepseek_balance_api",
            meter_kind="prepaid_balance", freshness="stale",
            fetched_at=fetched_val, ttl_s=ttl_s, now=now_val, detail="DeepSeek balance TTL expired",
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("balance_infos"), list) or not payload["balance_infos"]:
        return _unknown_pool_reading(
            pool_id=pool_id, provider="deepseek", source="deepseek_balance_api",
            meter_kind="prepaid_balance", fetched_at=fetched_val, ttl_s=ttl_s, now=now_val,
            detail="missing balance_infos in DeepSeek payload",
        )
    infos = [b for b in payload["balance_infos"] if isinstance(b, dict)]
    chosen = next((b for b in infos if b.get("currency") == "USD"), infos[0] if infos else None)
    if chosen is None:
        return _unknown_pool_reading(
            pool_id=pool_id, provider="deepseek", source="deepseek_balance_api",
            meter_kind="prepaid_balance", fetched_at=fetched_val, ttl_s=ttl_s, now=now_val,
        )
    total = _parse_float_str(chosen.get("total_balance"))
    if total is None:
        return _unknown_pool_reading(
            pool_id=pool_id, provider="deepseek", source="deepseek_balance_api",
            meter_kind="prepaid_balance", fetched_at=fetched_val, ttl_s=ttl_s, now=now_val,
            detail="unparseable DeepSeek total_balance",
        )
    avail_flag = payload.get("is_available")
    exhausted = (avail_flag is False) or (total <= 0.0)
    return {
        "pool_id": pool_id,
        "provider": "deepseek",
        "meter_kind": "prepaid_balance",
        "status": "exhausted" if exhausted else "ok",
        "fraction_left": None,
        "resets_at": None,
        "plan": "prepaid_api",
        "source": "deepseek_balance_api",
        "freshness": "fresh",
        "is_fresh": True,
        "fetched_at": epoch_to_iso(fetched_val),
        "fetched_at_epoch": fetched_val,
        "age_s": round(max(0.0, now_val - fetched_val), 1),
        "ttl_s": ttl_s,
        "used_fraction_reported": None,
        "windows": [],
        "money_left": round(total, 4),
        "currency": str(chosen.get("currency") or "USD"),
        "reset_credits": None,
        "detail": None,
    }


def parse_moonshot_balance(
    payload: Any,
    *,
    fetched_at: float | None = None,
    now: float | None = None,
    ttl_s: float = API_TTL_S,
    pool_id: str = "api:moonshot:unknown",
    currency: str = "USD",
) -> dict[str, Any]:
    """Parse Moonshot ``GET /v1/users/me/balance``."""
    now_val = _now_ts(now)
    fetched_val = _now_ts(fetched_at) if fetched_at is not None else now_val
    if not is_window_fresh(fetched_val, None, ttl_s, now_val):
        return _unknown_pool_reading(
            pool_id=pool_id, provider="moonshot", source="moonshot_balance_api",
            meter_kind="prepaid_balance", freshness="stale",
            fetched_at=fetched_val, ttl_s=ttl_s, now=now_val, detail="Moonshot balance TTL expired",
        )
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else None
    if data is None:
        return _unknown_pool_reading(
            pool_id=pool_id, provider="moonshot", source="moonshot_balance_api",
            meter_kind="prepaid_balance", fetched_at=fetched_val, ttl_s=ttl_s, now=now_val,
            detail="missing data block in Moonshot balance payload",
        )
    avail = _parse_float_str(data.get("available_balance"))
    if avail is None:
        return _unknown_pool_reading(
            pool_id=pool_id, provider="moonshot", source="moonshot_balance_api",
            meter_kind="prepaid_balance", fetched_at=fetched_val, ttl_s=ttl_s, now=now_val,
            detail="missing available_balance",
        )
    return {
        "pool_id": pool_id,
        "provider": "moonshot",
        "meter_kind": "prepaid_balance",
        "status": "exhausted" if avail <= 0.0 else "ok",
        "fraction_left": None,
        "resets_at": None,
        "plan": "prepaid_api",
        "source": "moonshot_balance_api",
        "freshness": "fresh",
        "is_fresh": True,
        "fetched_at": epoch_to_iso(fetched_val),
        "fetched_at_epoch": fetched_val,
        "age_s": round(max(0.0, now_val - fetched_val), 1),
        "ttl_s": ttl_s,
        "used_fraction_reported": None,
        "windows": [],
        "money_left": round(avail, 4),
        "currency": currency,
        "reset_credits": None,
        "detail": None,
    }


def parse_openrouter_key(
    payload: Any,
    *,
    credits_payload: Any = None,
    fetched_at: float | None = None,
    now: float | None = None,
    ttl_s: float = API_TTL_S,
    pool_id: str = "api:openrouter:unknown",
) -> dict[str, Any]:
    """Parse OpenRouter ``GET /api/v1/key`` (and optional ``/api/v1/credits``) via ``effective_budget``."""
    now_val = _now_ts(now)
    fetched_val = _now_ts(fetched_at) if fetched_at is not None else now_val
    if not is_window_fresh(fetched_val, None, ttl_s, now_val):
        return _unknown_pool_reading(
            pool_id=pool_id, provider="openrouter", source="openrouter_key_api",
            meter_kind="prepaid_balance", freshness="stale",
            fetched_at=fetched_val, ttl_s=ttl_s, now=now_val, detail="OpenRouter key TTL expired",
        )
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else None
    if data is None:
        return _unknown_pool_reading(
            pool_id=pool_id, provider="openrouter", source="openrouter_key_api",
            meter_kind="prepaid_balance", fetched_at=fetched_val, ttl_s=ttl_s, now=now_val,
            detail="missing data block in OpenRouter /key payload",
        )

    key_limit = _parse_float_str(data.get("limit"))
    key_rem = _parse_float_str(data.get("limit_remaining"))
    usage_val = _parse_float_str(data.get("usage"))

    acct_rem: float | None = None
    acct_total: float | None = None
    if isinstance(credits_payload, dict) and isinstance(credits_payload.get("data"), dict):
        cdata = credits_payload["data"]
        tc = _parse_float_str(cdata.get("total_credits"))
        tu = _parse_float_str(cdata.get("total_usage"))
        if tc is not None and tu is not None:
            acct_total = tc
            acct_rem = max(0.0, tc - tu)

    candidates_rem: list[tuple[float, float | None]] = []
    if key_limit is not None and key_limit > 0:
        kr = key_rem if key_rem is not None else max(0.0, key_limit - (usage_val or 0.0))
        candidates_rem.append((max(0.0, kr), key_limit))
    if acct_rem is not None:
        candidates_rem.append((acct_rem, acct_total))

    if not candidates_rem:
        # Uncapped key (limit=null) without management /credits -> ceiling is genuinely unknown!
        res = _unknown_pool_reading(
            pool_id=pool_id,
            provider="openrouter",
            source="openrouter_key_api",
            meter_kind="prepaid_balance",
            plan="free_key" if data.get("is_free_tier") else "uncapped_key",
            freshness="missing",
            fetched_at=fetched_val,
            ttl_s=ttl_s,
            now=now_val,
            detail="OpenRouter key has limit=null; account ceiling requires management key",
        )
        if usage_val is not None:
            res["usage_usd"] = round(usage_val, 4)
        return res

    eff_rem, eff_cap = min(candidates_rem, key=lambda pair: pair[0])
    frac_left = (
        round(_clamp(eff_rem / eff_cap, 0.0, 1.0), 6)
        if (eff_cap is not None and eff_cap > 0)
        else None
    )
    return {
        "pool_id": pool_id,
        "provider": "openrouter",
        "meter_kind": "prepaid_balance",
        "status": "exhausted" if eff_rem <= 0.0 else "ok",
        "fraction_left": frac_left,
        "resets_at": None,
        "plan": "capped_key" if key_limit is not None else "prepaid_credits",
        "source": "openrouter_key_api",
        "freshness": "fresh",
        "is_fresh": True,
        "fetched_at": epoch_to_iso(fetched_val),
        "fetched_at_epoch": fetched_val,
        "age_s": round(max(0.0, now_val - fetched_val), 1),
        "ttl_s": ttl_s,
        "used_fraction_reported": (
            {"value": round(1.0 - frac_left, 6), "unit": "ratio_effective_budget", "convention": "used"}
            if frac_left is not None
            else None
        ),
        "windows": [],
        "money_left": round(eff_rem, 4),
        "currency": "USD",
        "reset_credits": None,
        "detail": None,
    }


def parse_ollama_ps(
    payload: Any,
    *,
    fetched_at: float | None = None,
    now: float | None = None,
    ttl_s: float = OLLAMA_TTL_S,
    pool_id: str = "local:ollama:127.0.0.1:11434",
) -> dict[str, Any]:
    """Parse Ollama ``GET /api/ps`` into an ``unmetered_local`` reading (never fake 100% quota)."""
    now_val = _now_ts(now)
    fetched_val = _now_ts(fetched_at) if fetched_at is not None else now_val
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return _unknown_pool_reading(
            pool_id=pool_id, provider="alibaba", source="ollama_api_ps",
            meter_kind="unmetered_local", fetched_at=fetched_val, ttl_s=ttl_s, now=now_val,
            detail="invalid Ollama /api/ps response",
        )
    loaded = []
    vram_used = 0
    for m in payload["models"]:
        if isinstance(m, dict):
            if m.get("name"):
                loaded.append(str(m["name"]))
            sv = m.get("size_vram")
            if isinstance(sv, int) and not isinstance(sv, bool) and sv > 0:
                vram_used += sv
    return {
        "pool_id": pool_id,
        "provider": "local_ollama",
        "meter_kind": "unmetered_local",
        "status": "ok",
        "fraction_left": None,
        "resets_at": None,
        "plan": "local_compute",
        "source": "ollama_api_ps",
        "freshness": "fresh",
        "is_fresh": True,
        "fetched_at": epoch_to_iso(fetched_val),
        "fetched_at_epoch": fetched_val,
        "age_s": round(max(0.0, now_val - fetched_val), 1),
        "ttl_s": ttl_s,
        "used_fraction_reported": None,
        "windows": [],
        "money_left": None,
        "currency": None,
        "reset_credits": None,
        "loaded_models": loaded,
        "vram_used_bytes": vram_used,
        "detail": f"local Ollama online ({len(loaded)} model(s) loaded in VRAM)",
    }


def probe_ollama_ps(
    host_url: str = "http://127.0.0.1:11434",
    *,
    timeout_s: float = 1.5,
    now: float | None = None,
) -> tuple[dict[str, Any], str | None]:
    now_val = _now_ts(now)
    clean_host = host_url.replace("http://", "").replace("https://", "").rstrip("/")
    pool_id = f"local:ollama:{clean_host}"
    req = urllib.request.Request(f"{host_url.rstrip('/')}/api/ps", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
        fetched_val = _now_ts(now)
        return parse_ollama_ps(raw, fetched_at=fetched_val, now=fetched_val, pool_id=pool_id), None
    except Exception as exc:
        return (
            _unknown_pool_reading(
                pool_id=pool_id, provider="local_ollama", source="ollama_api_ps",
                meter_kind="unmetered_local", freshness="error", now=now_val,
                detail=f"Ollama unreachable: {type(exc).__name__}",
            ),
            "transport",
        )


def read_hermes_provider_cache(
    provider: str = "nous",
    *,
    data_dir: str | Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Read an optional public ``hermes_usage_snapshot.json`` file without private Hermes imports.

    Obeys Hermes Catalog Rule 9 (never import private ``agent.*`` internals).
    When no public usage snapshot is present, fails closed to ``status = "unknown"``.
    """
    now_val = _now_ts(now)
    prov = (provider or "nous").strip().lower()
    pool_id = f"hermes:{prov}"
    snap: Any = None
    if data_dir is not None:
        snap_path = Path(data_dir) / "hermes_usage_snapshot.json"
        if snap_path.is_file():
            try:
                raw_obj = json.loads(snap_path.read_text(encoding="utf-8"))
                snap = raw_obj.get(prov) if isinstance(raw_obj, dict) and prov in raw_obj else raw_obj
            except Exception:
                snap = None

    if isinstance(snap, dict):
        fetched_ep = parse_epoch(snap.get("fetched_at"))
        plan = str(snap.get("plan_type")) if snap.get("plan_type") else None
        windows: list[dict[str, Any]] = []
        for slot_key, wid in (("five_hour", "5h"), ("weekly", "7d")):
            w = snap.get(slot_key)
            if isinstance(w, dict) and _is_num(w.get("used_percent")):
                up = _clamp(float(w["used_percent"]), 0.0, 100.0)
                left = round(1.0 - (up / 100.0), 6)
                reset_ep = parse_epoch(w.get("resets_at"))
                fresh = is_window_fresh(fetched_ep, reset_ep, HERMES_TTL_S, now_val)
                windows.append({
                    "id": wid,
                    "fraction_left": left if fresh else None,
                    "resets_at": epoch_to_iso(reset_ep),
                    "is_fresh": fresh,
                    "governs_pool": True,
                    "used_fraction_reported": {
                        "value": float(w["used_percent"]),
                        "unit": "percent",
                        "convention": "used",
                    },
                })
        fresh_wins = [w for w in windows if w["is_fresh"] and w["fraction_left"] is not None]
        if fresh_wins and fetched_ep is not None:
            gov = min(fresh_wins, key=lambda w: float(w["fraction_left"]))
            frac = float(gov["fraction_left"])
            return {
                "pool_id": pool_id,
                "provider": prov,
                "meter_kind": "subscription_window",
                "status": "exhausted" if frac <= 0.0 else "ok",
                "fraction_left": frac,
                "resets_at": gov["resets_at"],
                "plan": plan,
                "source": "hermes_account_cache",
                "freshness": "fresh",
                "is_fresh": True,
                "fetched_at": epoch_to_iso(fetched_ep),
                "fetched_at_epoch": fetched_ep,
                "age_s": round(max(0.0, now_val - fetched_ep), 1),
                "ttl_s": HERMES_TTL_S,
                "used_fraction_reported": gov["used_fraction_reported"],
                "windows": windows,
                "money_left": None,
                "currency": None,
                "reset_credits": None,
                "detail": None,
            }

    return _unknown_pool_reading(
        pool_id=pool_id,
        provider=prov,
        source="hermes_account_cache",
        meter_kind="session_credits",
        freshness="missing",
        ttl_s=HERMES_TTL_S,
        now=now_val,
        detail=f"Hermes '{prov}' session pool does not publish a plan-usage denominator yet",
    )


# ---------------------------------------------------------------------------
# Cache with >=300s poll floor, single-flight per-pool lock, failure asymmetry
# ---------------------------------------------------------------------------

class UsageCache:
    """Thread-safe pool usage cache enforcing poll floors and transport-vs-auth asymmetry."""

    def __init__(self, min_poll_interval_s: float = MIN_POLL_INTERVAL_S) -> None:
        self.min_poll_interval_s = float(min_poll_interval_s)
        self._guard = threading.Lock()
        self._pool_locks: dict[str, threading.Lock] = {}
        self._entries: dict[str, dict[str, Any]] = {}
        self._last_active_poll: dict[str, float] = {}
        self._grok_prior_bounds: dict[str, tuple[str, str]] = {}
        self._grok_tail_state: dict[str, dict[str, Any]] = {}

    def _lock_for(self, pool_id: str) -> threading.Lock:
        with self._guard:
            lk = self._pool_locks.get(pool_id)
            if lk is None:
                lk = threading.Lock()
                self._pool_locks[pool_id] = lk
            return lk

    def get(self, pool_id: str, now: float | None = None) -> dict[str, Any] | None:
        with self._guard:
            entry = self._entries.get(pool_id)
        if entry is None:
            return None
        return recheck_reading_freshness(entry, now=now)

    def put(self, reading: dict[str, Any]) -> dict[str, Any]:
        pid = str(reading.get("pool_id") or "unknown")
        copy = dict(reading)
        pb = copy.get("period_bounds")
        with self._guard:
            self._entries[pid] = copy
            if isinstance(pb, (list, tuple)) and len(pb) == 2 and pb[0] and pb[1]:
                self._grok_prior_bounds[pid] = (str(pb[0]), str(pb[1]))
        return copy

    def record_probe_result(
        self,
        pool_id: str,
        new_reading: dict[str, Any],
        failure_kind: str | None,
        *,
        now: float | None = None,
        active_polled: bool = False,
    ) -> dict[str, Any]:
        """Apply failure asymmetry:

        - ``failure_kind == "transport"``: preserve existing cached reading with
          its ORIGINAL ``fetched_at`` untouched so it ages naturally to ``unknown``.
        - ``failure_kind == "auth"``: immediately invalidate to ``unknown``.
        - ``failure_kind is None``: store ``new_reading``.
        """
        now_val = _now_ts(now)
        with self._guard:
            if active_polled:
                self._last_active_poll[pool_id] = now_val
            existing = self._entries.get(pool_id)

        if failure_kind == "transport" and existing is not None:
            preserved = dict(existing)
            preserved["last_probe_error"] = new_reading.get("detail") or "transient probe error"
            with self._guard:
                self._entries[pool_id] = preserved
            return recheck_reading_freshness(preserved, now=now_val)

        if failure_kind == "auth":
            invalidated = dict(new_reading)
            invalidated["status"] = "unknown"
            invalidated["fraction_left"] = None
            invalidated["freshness"] = "auth_locked"
            invalidated["is_fresh"] = False
            with self._guard:
                self._entries[pool_id] = invalidated
            return invalidated

        stored = self.put(new_reading)
        return recheck_reading_freshness(stored, now=now_val)

    def can_active_poll(self, pool_id: str, now: float | None = None) -> bool:
        now_val = _now_ts(now)
        with self._guard:
            last = self._last_active_poll.get(pool_id)
        if last is None:
            return True
        return (now_val - last) >= self.min_poll_interval_s

    def run_singleflight(self, pool_id: str, fn):
        lk = self._lock_for(pool_id)
        with lk:
            return fn()


GLOBAL_USAGE_CACHE = UsageCache()


# ---------------------------------------------------------------------------
# Seat -> Pool mapping, aggregation, pre-flight, and human-readable report
# ---------------------------------------------------------------------------

def pool_kind_for_seat(seat_id: str, executor: Any = None, env: dict[str, str] | None = None) -> tuple[str, str]:
    """Return ``(driver_kind, pool_id)`` for one configured seat."""
    cls_name = type(executor).__name__ if executor is not None else ""
    sid = str(seat_id or "")

    if cls_name == "HermesModelExecutor" or sid.startswith("mm-"):
        prov = str(getattr(executor, "provider", None) or "nous").strip().lower()
        return "hermes", f"hermes:{prov}"
    if cls_name == "CodexExecutor" or sid == "codex":
        return "codex", _codex_pool_id_from_home(codex_home(env))
    if cls_name == "GrokExecutor" or sid == "grok":
        pid, _ = _grok_pool_id_from_home(grok_home(env))
        return "grok", pid
    if cls_name == "AgyExecutor" or sid == "agy":
        return "agy", "agy:os-keyring"
    if cls_name == "ClaudeExecutor" or sid == "claude":
        cdir = claude_config_dir(env)
        return "claude", f"claude:{pseudonymize_account_id(str(cdir), 'claude-home')}"
    if cls_name == "KimiExecutor" or sid == "kimi":
        home = kimi_code_home(env)
        return "kimi", f"kimi:{pseudonymize_account_id(str(home), 'kimi-home')}"
    if cls_name == "OllamaQwenExecutor" or sid == "qwen-local":
        base = str(getattr(executor, "base_url", None) or "http://127.0.0.1:11434/v1")
        host = base.replace("/v1", "").replace("http://", "").replace("https://", "").rstrip("/")
        return "ollama", f"local:ollama:{host}"
    if cls_name == "DeepSeekApiExecutor" or sid == "deepseek":
        e = env if env is not None else os.environ
        key = e.get("DEEPSEEK_API_KEY") or ""
        return "deepseek_api", f"api:deepseek:{pseudonymize_account_id(key, 'deepseek')}"
    if cls_name == "KimiApiExecutor" or sid == "kimi-api":
        e = env if env is not None else os.environ
        key = e.get("MOONSHOT_API_KEY") or ""
        return "moonshot_api", f"api:moonshot:{pseudonymize_account_id(key, 'moonshot')}"
    if cls_name == "LocalSolverExecutor" or sid.startswith("solver-"):
        return "local_solver", "local:solver"
    return "other", f"seat:{sid}"


def _probe_driver_pool(
    driver: str,
    pool_id: str,
    *,
    cache: UsageCache,
    data_dir: Path | None,
    refresh: bool,
    env: dict[str, str] | None,
    now_val: float,
) -> dict[str, Any]:
    def _execute() -> dict[str, Any]:
        cached = cache.get(pool_id, now=now_val)
        allow_active = bool(refresh) and cache.can_active_poll(pool_id, now=now_val)

        if driver == "hermes":
            prov = pool_id.split(":", 1)[1] if ":" in pool_id else "nous"
            return cache.put(read_hermes_provider_cache(prov, data_dir=data_dir, now=now_val))

        if driver == "local_solver":
            return cache.put({
                "pool_id": pool_id,
                "provider": "local",
                "meter_kind": "unmetered_local",
                "status": "ok",
                "fraction_left": None,
                "resets_at": None,
                "plan": "builtin_solver",
                "source": "builtin",
                "freshness": "fresh",
                "is_fresh": True,
                "fetched_at": epoch_to_iso(now_val),
                "fetched_at_epoch": now_val,
                "age_s": 0.0,
                "ttl_s": 3600.0,
                "used_fraction_reported": None,
                "windows": [],
                "money_left": None,
                "currency": None,
                "reset_credits": None,
                "detail": "built-in deterministic solver (unmetered)",
            })

        if driver == "claude":
            # Passive tee/harvest is always fast; only spawn `claude auth status` when refreshing or uncached
            if cached is not None and not allow_active:
                # Re-check passive files without spawning subprocess
                reading, _ = probe_claude_usage(
                    data_dir=data_dir, env=env, allow_auth_status=False, now=now_val
                )
                if reading.get("status") in ("ok", "exhausted"):
                    return cache.put(reading)
                return cached
            reading, fkind = probe_claude_usage(
                data_dir=data_dir, env=env, allow_auth_status=allow_active, now=now_val
            )
            return cache.record_probe_result(
                pool_id, reading, fkind, now=now_val, active_polled=allow_active
            )

        if driver == "grok":
            prior_b = cache._grok_prior_bounds.get(pool_id)
            tail_st = cache._grok_tail_state.get(pool_id)
            reading, fkind, new_tail = probe_grok_usage(
                env=env, allow_active=allow_active, now=now_val,
                prior_bounds=prior_b, tail_state=tail_st,
            )
            cache._grok_tail_state[pool_id] = new_tail
            if not allow_active and cached is not None and reading.get("status") == "unknown":
                return cached
            return cache.record_probe_result(
                pool_id, reading, fkind, now=now_val, active_polled=allow_active
            )

        if not allow_active:
            if cached is not None:
                return cached
            return _unknown_pool_reading(
                pool_id=pool_id,
                provider=driver,
                source=f"{driver}_passive_cache",
                meter_kind="unmetered_local" if driver == "ollama" else "subscription_window",
                freshness="missing",
                now=now_val,
                detail="not yet polled (call /usage?refresh=true or `hermes critic usage`)",
            )

        if driver == "codex":
            reading, fkind = probe_codex_usage(env=env, now=now_val)
            return cache.record_probe_result(pool_id, reading, fkind, now=now_val, active_polled=True)

        if driver == "agy":
            reading, fkind = probe_agy_usage(env=env, now=now_val)
            return cache.record_probe_result(pool_id, reading, fkind, now=now_val, active_polled=True)

        if driver == "kimi":
            reading, fkind = probe_kimi_usage(env=env, now=now_val)
            return cache.record_probe_result(pool_id, reading, fkind, now=now_val, active_polled=True)

        if driver == "ollama":
            host_part = pool_id.split("local:ollama:", 1)[-1]
            reading, fkind = probe_ollama_ps(f"http://{host_part}", now=now_val)
            return cache.record_probe_result(pool_id, reading, fkind, now=now_val, active_polled=True)

        return _unknown_pool_reading(
            pool_id=pool_id, provider=driver, source="unconfigured",
            freshness="unconfigured", now=now_val, detail=f"no live usage probe configured for {driver}",
        )

    return cache.run_singleflight(pool_id, _execute)


def _discover_host_cli_pools(env: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Return installed standalone CLI / local compute pools on this machine."""
    pools: list[tuple[str, str]] = []
    if shutil.which("codex") or (codex_home(env) / "auth.json").is_file():
        pools.append(("codex", _codex_pool_id_from_home(codex_home(env))))
    if shutil.which("grok") or (grok_home(env) / "auth.json").is_file():
        gpid, _ = _grok_pool_id_from_home(grok_home(env))
        pools.append(("grok", gpid))
    if shutil.which("agy"):
        pools.append(("agy", "agy:os-keyring"))
    if shutil.which("claude") or (claude_config_dir(env) / ".credentials.json").is_file():
        cdir = claude_config_dir(env)
        pools.append(("claude", f"claude:{pseudonymize_account_id(str(cdir), 'claude-home')}"))
    if shutil.which("kimi") or (kimi_code_home(env) / "credentials" / "kimi-code.json").is_file():
        khome = kimi_code_home(env)
        pools.append(("kimi", f"kimi:{pseudonymize_account_id(str(khome), 'kimi-home')}"))
    if shutil.which("ollama"):
        pools.append(("ollama", "local:ollama:127.0.0.1:11434"))
    return pools


def _seat_projection(seat_id: str, executor: Any, pool_reading: dict[str, Any]) -> dict[str, Any]:
    return {
        "seat_id": seat_id,
        "family": getattr(executor, "family", None) if executor is not None else None,
        "pool_id": pool_reading["pool_id"],
        "meter_kind": pool_reading["meter_kind"],
        "status": pool_reading["status"],
        "fraction_left": pool_reading["fraction_left"],
        "resets_at": pool_reading["resets_at"],
        "plan": pool_reading["plan"],
        "source": pool_reading["source"],
        "freshness": pool_reading["freshness"],
        "fetched_at": pool_reading["fetched_at"],
        "money_left": pool_reading.get("money_left"),
        "currency": pool_reading.get("currency"),
        "reset_credits": pool_reading.get("reset_credits"),
    }


def collect_usage_feed(
    executors: dict[str, Any] | None = None,
    *,
    data_dir: str | Path | None = None,
    refresh: bool = False,
    include_cli_pools: bool = True,
    env: dict[str, str] | None = None,
    now: float | None = None,
    cache: UsageCache | None = None,
) -> dict[str, Any]:
    """Collect deduplicated per-pool readings and project them onto every panel seat.

    Read-only contract: never creates ``data_dir`` or ``data.db``. Persists
    ``usage_feed_cache.json`` ONLY when ``data_dir`` already exists on disk and
    ``refresh=True``.
    """
    now_val = _now_ts(now)
    use_cache = cache if cache is not None else GLOBAL_USAGE_CACHE
    ddir = Path(data_dir) if data_dir is not None else None

    # Hydrate in-memory cache from existing disk snapshot if cold
    if ddir is not None and ddir.is_dir():
        disk_file = ddir / USAGE_CACHE_FILENAME
        if disk_file.is_file() and not use_cache._entries:
            try:
                saved = json.loads(disk_file.read_text(encoding="utf-8"))
                for p_reading in (saved.get("pools") or {}).values():
                    if isinstance(p_reading, dict) and p_reading.get("pool_id"):
                        use_cache.put(p_reading)
            except Exception:
                pass

    exec_map = dict(executors or {})
    seat_to_pool: dict[str, str] = {}
    pool_drivers: dict[str, str] = {}

    for sid, ex in exec_map.items():
        drv, pid = pool_kind_for_seat(sid, ex, env=env)
        seat_to_pool[sid] = pid
        pool_drivers.setdefault(pid, drv)

    if include_cli_pools:
        for drv, pid in _discover_host_cli_pools(env=env):
            pool_drivers.setdefault(pid, drv)

    pools_out: dict[str, dict[str, Any]] = {}
    for pid, drv in pool_drivers.items():
        pools_out[pid] = _probe_driver_pool(
            drv, pid, cache=use_cache, data_dir=ddir,
            refresh=refresh, env=env, now_val=now_val,
        )

    seats_out: dict[str, dict[str, Any]] = {}
    for sid, ex in exec_map.items():
        pid = seat_to_pool[sid]
        pools_entry = pools_out[pid]
        seats_out[sid] = _seat_projection(sid, ex, pools_entry)

    known_count = sum(1 for p in pools_out.values() if p.get("status") in ("ok", "exhausted"))
    exhausted_count = sum(1 for p in pools_out.values() if p.get("status") == "exhausted")
    unknown_count = sum(1 for p in pools_out.values() if p.get("status") == "unknown")

    result = {
        "generated_at": epoch_to_iso(now_val),
        "pools": pools_out,
        "seats": seats_out,
        "summary": {
            "pools_total": len(pools_out),
            "pools_known": known_count,
            "pools_exhausted": exhausted_count,
            "pools_unknown": unknown_count,
        },
    }

    # Strictly read-only when ddir does not exist; only update cache file inside an existing ddir
    if refresh and ddir is not None and ddir.is_dir():
        try:
            _atomic_write_json_0600(ddir / USAGE_CACHE_FILENAME, result)
        except Exception:
            pass

    return result


def evaluate_panel_preflight(
    executors: dict[str, Any],
    contestants: list[str],
    min_usage_fraction: float,
    *,
    data_dir: str | Path | None = None,
    cache: UsageCache | None = None,
    now: float | None = None,
) -> dict[str, str]:
    """Pool-deduped pre-flight check for ``contract.min_usage_fraction``.

    Returns ``{seat_id: skip_reason}`` ONLY for seats whose pool has a fresh,
    verified reading below ``min_usage_fraction`` (or ``status == "exhausted"``
    when ``min_usage_fraction > 0``). ``unknown`` and ``unmetered_local`` pools
    are NEVER falsely blocked.
    """
    threshold = float(min_usage_fraction)
    subset = {sid: executors[sid] for sid in contestants if sid in executors}
    feed = collect_usage_feed(
        subset, data_dir=data_dir, refresh=True,
        include_cli_pools=False, now=now, cache=cache,
    )
    skips: dict[str, str] = {}
    for sid in contestants:
        seat_u = feed["seats"].get(sid)
        if not seat_u:
            continue
        st = seat_u.get("status")
        frac = seat_u.get("fraction_left")
        pid = seat_u.get("pool_id")
        if st == "exhausted" and threshold > 0.0:
            skips[sid] = f"pool {pid} quota exhausted (fraction_left={frac})"
        elif st == "ok" and frac is not None and float(frac) < threshold:
            skips[sid] = (
                f"pool {pid} fraction_left={frac:.3f} below "
                f"contract min_usage_fraction={threshold:.3f}"
            )
    return skips


def format_usage_report(feed: dict[str, Any]) -> str:
    """Human-readable summary for ``hermes critic usage`` and ``/mm usage``."""
    lines: list[str] = []
    summ = feed.get("summary") or {}
    lines.append(
        f"Millionaire Matrix Plan-Usage Feed ({feed.get('generated_at') or 'now'}): "
        f"{summ.get('pools_known', 0)}/{summ.get('pools_total', 0)} pools metered "
        f"({summ.get('pools_unknown', 0)} unknown, {summ.get('pools_exhausted', 0)} exhausted)"
    )
    pools = feed.get("pools") or {}
    if pools:
        lines.append("  Quota & Compute Pools (deduplicated):")
        for pid, p in pools.items():
            st = str(p.get("status") or "unknown").upper()
            frac = p.get("fraction_left")
            money = p.get("money_left")
            mk = p.get("meter_kind")
            if frac is not None:
                val_str = f"{frac * 100.0:5.1f}% left"
            elif money is not None:
                val_str = f"{money:.2f} {p.get('currency') or 'USD'}"
            elif mk == "unmetered_local" and st == "OK":
                val_str = "local unmetered"
            else:
                val_str = f"unknown ({p.get('freshness') or 'missing'})"
            plan = f" plan={p['plan']}" if p.get("plan") else ""
            resets = f" resets={p['resets_at']}" if p.get("resets_at") else ""
            rc = p.get("reset_credits")
            rc_str = (
                f" reset_credits={rc['available_count']}"
                if isinstance(rc, dict) and rc.get("available_count") is not None
                else ""
            )
            src = p.get("source") or "?"
            lines.append(f"    - {pid:<28} [{st:<9}] {val_str:<18}{plan}{resets}{rc_str} ({src})")
            for w in p.get("windows") or []:
                wf = w.get("fraction_left")
                wf_str = f"{wf * 100.0:5.1f}% left" if wf is not None else "stale/unknown"
                wr = f" -> {w['resets_at']}" if w.get("resets_at") else ""
                lines.append(f"        window {w.get('id'):<22} {wf_str}{wr}")

    seats = feed.get("seats") or {}
    if seats:
        lines.append("  Panel Seat -> Pool Mapping:")
        for sid, s in seats.items():
            frac = s.get("fraction_left")
            f_txt = f"{frac * 100.0:.1f}% left" if frac is not None else s.get("status", "unknown")
            lines.append(
                f"    - {sid:<14} [{str(s.get('family') or '?'):<10}] -> {s.get('pool_id')} ({f_txt})"
            )
    return "\n".join(lines)
