"""Contract loading, validation, and the baseline rule.

A Contract is the spec's testable unit: a task plus STRUCTURED acceptance
items. Hard items are shell commands with an expected exit code; measured
items are sensor commands emitting a JSON object of metrics, each with an
op and threshold; judged items name a versioned rubric, a minimum, and a
judge count. Prose acceptance is rejected -- a gate the system cannot
evaluate is decoration.

Baseline rule (spec, Object schema): the acceptance tests must execute
and FAIL against the baseline before any candidate runs. If everything
passes on the baseline, the contract is vacuous and the loop refuses it.

OWNER REVIEW item 1 (v0.3, resolved): a judged item carries TWO named
dissent numbers, not one. ``spread_threshold`` (default 2.0) bounds the
spread of judge *means* for a candidate -- wide panel disagreement
escalates instead of averaging away. ``dissent_threshold`` (default
3.0) bounds how far a single judge's score on one matrix/rubric row may
deviate from that row's median before it is flagged as dissent. The
spec left both numbers as an open question; it is closed here with
these defaults, and both are validated as numbers >= 0 (anything else
is a ContractError).
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .errors import ContractError, GateError
from .gates import run_gate_command
from .lessons import METRIC_NAME_RE

_OPS = {"==", "!=", "<", "<=", ">", ">="}
_STAGES = {"spec", "decompose", "implement", "integrate"}
_MODES = {"fix", "improve"}

# Contract ids become store keys, branch-name components, and worktree
# path segments (fix pass round 4, spec G7, round-4 security F6): a
# conservative charset — letters, digits, dash, underscore; no path
# separators, no dots, never a leading dash — so no id can escape the
# worktree root or corrupt a ref.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

# Gate commands get at most an hour per item, however the contract asks
# (spec G7: timeout_s carried through validation, capped at a sane max).
_MAX_TIMEOUT_S = 3600


def _validated_threshold(item, key, default, index):
    """A judged-item threshold: a real number >= 0, else ContractError."""
    raw = item.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ContractError(
            f"acceptance[{index}] judged item {key!r} must be a number, "
            f"got {raw!r}"
        )
    value = float(raw)
    if value < 0:
        raise ContractError(
            f"acceptance[{index}] judged item {key!r} must be >= 0, "
            f"got {value}"
        )
    return value


def _validated_id(raw: dict) -> str:
    """The contract id: required, conservative charset (spec G7).

    A missing id used to normalize to None and key a whole run by a
    phantom (round-4 contest-core F11); an unsanitized one could carry
    path separators into worktree creation (round-4 security F6). Both
    fail loudly here instead.
    """
    cid = raw.get("id")
    if cid is None:
        raise ContractError("contract.id is required (string)")
    if not isinstance(cid, str) or not _ID_RE.match(cid):
        raise ContractError(
            "contract.id must be 1-128 characters of letters, digits, "
            f"'-' or '_', starting with a letter or digit; got {cid!r}"
        )
    return cid


def _validated_attempt(raw: dict) -> int:
    """The attempt counter: an integer >= 1, ContractError on bad types
    (round-4 contest-core F11 — the coercion used to leak a bare
    ValueError past callers catching the documented error type)."""
    value = raw.get("attempt", 1)
    if isinstance(value, bool):
        raise ContractError(f"contract.attempt must be an integer, got {value!r}")
    try:
        attempt = int(value)
    except (TypeError, ValueError):
        raise ContractError(
            f"contract.attempt must be an integer, got {value!r}"
        ) from None
    if isinstance(value, float) and not value.is_integer():
        raise ContractError(f"contract.attempt must be an integer, got {value!r}")
    if attempt < 1:
        raise ContractError(f"contract.attempt must be >= 1, got {attempt}")
    return attempt


def _validated_timeout(item, index):
    """An item's timeout_s: carried through normalization (spec G7,
    round-4 contest-core F5 — validation used to drop it, so every gate
    ran at the 900 s default). Returns None when the item sets none;
    otherwise a positive number capped at _MAX_TIMEOUT_S. Bad types are
    ContractError, like every other validation failure."""
    if "timeout_s" not in item or item.get("timeout_s") is None:
        return None
    raw = item["timeout_s"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ContractError(
            f"acceptance[{index}] timeout_s must be a number of seconds, "
            f"got {raw!r}"
        )
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ContractError(
            f"acceptance[{index}] timeout_s must be positive and finite, "
            f"got {raw!r}"
        )
    value = min(value, float(_MAX_TIMEOUT_S))
    return int(value) if value.is_integer() else value


def _validated_expect_exit(item, index):
    """expect.exit, strictly typed (spec G7, round-4 contest-core F11).

    A bool stays a bool and an int stays an int — the value is stored
    with its type intact and the gate comparison is type-strict
    (gates.exit_matches), so ``{"exit": true}`` is never satisfied by a
    command exiting 1. Any other type is a ContractError.
    """
    expect = item.get("expect") or {}
    if not isinstance(expect, dict):
        raise ContractError(f"acceptance[{index}] expect must be an object")
    exit_code = expect.get("exit", 0)
    if isinstance(exit_code, bool):
        return exit_code
    if not isinstance(exit_code, int):
        raise ContractError(
            f"acceptance[{index}] expect.exit must be an int or bool, "
            f"got {exit_code!r}"
        )
    return exit_code


def _validated_judges(item, index) -> int:
    """A judged item's judge count: an integer >= 1 when present
    (round-4 contest-core F11 — ``judges: 0`` used to validate cleanly
    and only fail downstream in panel seating). Defaults to 8 for
    ``validity_matrix_v1`` (the 8-family peer panel) and 3 for legacy rubrics."""
    default_count = 8 if item.get("rubric") == "validity_matrix_v1" else 3
    raw = item.get("judges", default_count)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ContractError(
            f"acceptance[{index}] judged item 'judges' must be an integer, "
            f"got {raw!r}"
        )
    if raw < 1:
        raise ContractError(
            f"acceptance[{index}] judged item 'judges' must be >= 1, got {raw}"
        )
    return raw


def _validated_relpath(path_val, field_name: str) -> str:
    if not isinstance(path_val, str) or not path_val.strip():
        raise ContractError(f"{field_name} must be a non-empty relative path string, got {path_val!r}")
    cleaned = path_val.strip()
    if cleaned.startswith("/") or ".." in Path(cleaned).parts or ".git" in Path(cleaned).parts:
        raise ContractError(f"{field_name} must stay inside the repository without '..' or '.git': got {path_val!r}")
    return cleaned


def load_contract_file(path) -> dict:
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surface as contract error
        raise ContractError(f"cannot read contract file {p}: {exc}") from exc
    return validate_contract(raw)


def validate_contract(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ContractError("contract must be a JSON object")
    contract_id = _validated_id(raw)
    task = raw.get("task")
    if not task or not isinstance(task, str):
        raise ContractError("contract.task is required (string)")
    domain = raw.get("domain")
    if not domain or not isinstance(domain, str):
        raise ContractError("contract.domain is required (string)")
    stage = raw.get("stage", "implement")
    if stage not in _STAGES:
        raise ContractError(f"contract.stage must be one of {sorted(_STAGES)}")
    mode = raw.get("mode", "fix")
    if mode not in _MODES:
        raise ContractError(f"contract.mode must be one of {sorted(_MODES)}, got {mode!r}")
    raw_target_files = raw.get("target_files")
    norm_target_files = None
    if raw_target_files is not None:
        if not isinstance(raw_target_files, list) or not raw_target_files:
            raise ContractError("contract.target_files must be a non-empty list of relative paths when present")
        norm_target_files = [
            _validated_relpath(p, f"target_files[{idx}]")
            for idx, p in enumerate(raw_target_files)
        ]
    acceptance = raw.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        raise ContractError("contract.acceptance must be a non-empty list")

    normalized = []
    has_decidable = False
    for i, item in enumerate(acceptance):
        if not isinstance(item, dict):
            raise ContractError(f"acceptance[{i}] must be an object")
        cls = item.get("class")
        if cls == "hard":
            cmd = item.get("cmd")
            if not cmd:
                raise ContractError(f"acceptance[{i}] hard item needs 'cmd'")
            entry = {
                "class": "hard",
                "cmd": cmd,
                "expect": {"exit": _validated_expect_exit(item, i)},
            }
            timeout_s = _validated_timeout(item, i)
            if timeout_s is not None:
                entry["timeout_s"] = timeout_s
            normalized.append(entry)
            has_decidable = True
        elif cls == "measured":
            sensor_cmd = item.get("sensor_cmd") or item.get("cmd")
            if not sensor_cmd:
                raise ContractError(f"acceptance[{i}] measured item needs 'sensor_cmd'")
            metrics = item.get("metrics")
            if not isinstance(metrics, list) or not metrics:
                raise ContractError(f"acceptance[{i}] measured item needs non-empty 'metrics'")
            norm_metrics = []
            for j, m in enumerate(metrics):
                if not isinstance(m, dict) or "name" not in m or "op" not in m or "value" not in m:
                    raise ContractError(f"acceptance[{i}].metrics[{j}] needs name/op/value")
                # Declared metric names are charset-checked at the
                # boundary (round-6 fix spec J5, round-6 security F3):
                # lessons.sanitize_metric_name trusts the contract's
                # declared set, so a hostile name declared here would
                # ride unsanitized into every later lesson.  The rule
                # is the sanitizer's own charset, imported from
                # lessons (METRIC_NAME_RE) so the two cannot drift.
                if not isinstance(m["name"], str) or not METRIC_NAME_RE.match(m["name"]):
                    raise ContractError(
                        f"acceptance[{i}].metrics[{j}] name {m['name']!r} is not "
                        "a valid metric name: metric names must match "
                        "[A-Za-z0-9_.-]{1,64}"
                    )
                if m["op"] not in _OPS:
                    raise ContractError(f"acceptance[{i}].metrics[{j}] op must be one of {sorted(_OPS)}")
                norm_metrics.append({"name": m["name"], "op": m["op"], "value": m["value"]})
            entry = {
                "class": "measured",
                "sensor": item.get("sensor", ""),
                "sensor_cmd": sensor_cmd,
                "metrics": norm_metrics,
            }
            timeout_s = _validated_timeout(item, i)
            if timeout_s is not None:
                entry["timeout_s"] = timeout_s
            normalized.append(entry)
            has_decidable = True
        elif cls == "judged":
            rubric = item.get("rubric")
            if not rubric:
                raise ContractError(f"acceptance[{i}] judged item needs 'rubric'")
            if "min" not in item:
                raise ContractError(f"acceptance[{i}] judged item needs 'min'")
            # Validated at load like the thresholds (fix pass Wave 2.10,
            # review 1B-18): a bad minimum must fail before any seat is paid.
            min_score = _validated_threshold(item, "min", 0.0, i)
            if min_score > 10:
                raise ContractError(
                    f"acceptance[{i}] judged item 'min' must be on the 0-10 scale, "
                    f"got {min_score}"
                )
            normalized.append(
                {
                    "class": "judged",
                    "rubric": rubric,
                    "min": min_score,
                    "judges": _validated_judges(item, i),
                    "spread_threshold": _validated_threshold(
                        item, "spread_threshold", 2.0, i
                    ),
                    "dissent_threshold": _validated_threshold(
                        item, "dissent_threshold", 3.0, i
                    ),
                }
            )
        else:
            raise ContractError(f"acceptance[{i}] class must be hard|measured|judged, got {cls!r}")

    if not has_decidable:
        # Spec: every contract also carries a hard or measured gate; a
        # judged-only contract is refused.
        raise ContractError("contract has no hard or measured acceptance item; judged-only contracts are refused")

    # Step-down bracket config (owner refinement): the cut size is
    # configurable per contract. Default: eliminate 2 per round
    # (cut_mode "fixed", cut_per_round 2); cut_mode "half" selects the
    # halving ladder. The ladder always lands on a final two, then a
    # final scored round to one (see bracket.py).
    raw_bracket = raw.get("bracket") or {}
    if not isinstance(raw_bracket, dict):
        raise ContractError("'bracket' must be an object when present")
    cut_mode = raw_bracket.get("cut_mode", "fixed")
    if cut_mode not in ("fixed", "half"):
        raise ContractError(f"bracket.cut_mode must be 'fixed' or 'half', got {cut_mode!r}")
    cut_per_round = raw_bracket.get("cut_per_round", 2)
    if not isinstance(cut_per_round, int) or isinstance(cut_per_round, bool) or cut_per_round < 1:
        raise ContractError("bracket.cut_per_round must be an integer >= 1")
    bracket = {"cut_mode": cut_mode, "cut_per_round": cut_per_round}

    min_usage_fraction = None
    if "min_usage_fraction" in raw and raw.get("min_usage_fraction") is not None:
        raw_muf = raw["min_usage_fraction"]
        if isinstance(raw_muf, bool) or not isinstance(raw_muf, (int, float)):
            raise ContractError(
                f"contract.min_usage_fraction must be a number in [0.0, 1.0], got {raw_muf!r}"
            )
        muf_val = float(raw_muf)
        if not math.isfinite(muf_val) or muf_val < 0.0 or muf_val > 1.0:
            raise ContractError(
                f"contract.min_usage_fraction must be in [0.0, 1.0], got {raw_muf!r}"
            )
        min_usage_fraction = muf_val

    return {
        "id": contract_id,
        "target_id": raw.get("target_id"),
        "stage": stage,
        "mode": mode,
        "attempt": _validated_attempt(raw),
        "task": task,
        "domain": domain,
        "tags": list(raw.get("tags", [])),
        "depends_on": list(raw.get("depends_on", [])),
        "acceptance": normalized,
        "test_author_family": raw.get("test_author_family"),
        "target_file": raw.get("target_file"),
        "target_files": norm_target_files,
        "ask": raw.get("ask"),
        "constraints": list(raw.get("constraints", [])),
        "budget": raw.get("budget"),
        "bracket": bracket,
        "min_usage_fraction": min_usage_fraction,
    }


def verify_baseline(store, contract: dict, repo_path, worktree_mgr) -> tuple[bool, dict]:
    """Run acceptance against a pristine baseline worktree.

    Returns (proceed, detail). Proceed is True iff the acceptance suite
    FAILS on the baseline in the expected way: at least one hard item
    fails, or at least one measured metric fails (fix pass round 4,
    spec G9, round-3 contest-core F3 — the rule used to refuse a mixed
    contract whose hard items passed and measured items failed, which
    its own docstring never said). Records acceptance_baseline_run on
    the contract row. Commands run with the scrubbed gate environment,
    each bounded by its item's timeout_s (spec G7 — the baseline path
    used to pass no timeout at all).
    """
    from .gates import compare, exit_matches  # lazy: gates is a sibling workstream

    contract_id = contract["id"]
    baseline_wt = worktree_mgr.create(contract_id, "baseline")
    detail: dict = {"hard": [], "measured": []}
    hard_items = [a for a in contract["acceptance"] if a["class"] == "hard"]
    measured_items = [a for a in contract["acceptance"] if a["class"] == "measured"]

    hard_failed = False
    for item in hard_items:
        res = run_gate_command(
            item["cmd"], baseline_wt, timeout_s=item.get("timeout_s", 900)
        )
        passed = exit_matches(res.exit_code, item["expect"]["exit"])
        hard_failed = hard_failed or not passed
        detail["hard"].append(
            {"cmd": item["cmd"], "exit_code": res.exit_code, "passed": passed}
        )

    measured_failed = False
    for item in measured_items:
        res = run_gate_command(
            item["sensor_cmd"], baseline_wt, timeout_s=item.get("timeout_s", 900)
        )
        entry = {"sensor_cmd": item["sensor_cmd"], "exit_code": res.exit_code, "metrics": []}
        try:
            values = json.loads(res.stdout)
        except Exception:  # noqa: BLE001
            values = None
        if not isinstance(values, dict):
            # Sensor itself did not produce readings on the baseline
            # (e.g. the stub raises). That is a baseline failure of the
            # measured gate, which is what the rule wants to see.
            measured_failed = True
            entry["error"] = "sensor produced no JSON metrics on baseline"
        else:
            for spec in item["metrics"]:
                if spec["name"] not in values:
                    measured_failed = True
                    entry["metrics"].append({"name": spec["name"], "passed": False, "error": "missing"})
                    continue
                try:
                    ok = compare(values[spec["name"]], spec["op"], spec["value"])
                except GateError as exc:
                    # A reading the comparison cannot judge (a string or
                    # boolean where a number belongs) is a baseline
                    # failure of that metric, recorded as such — the
                    # baseline path treats broken sensors as data.
                    measured_failed = True
                    entry["metrics"].append(
                        {"name": spec["name"], "value": values[spec["name"]],
                         "passed": False, "error": str(exc)}
                    )
                    continue
                measured_failed = measured_failed or not ok
                entry["metrics"].append(
                    {"name": spec["name"], "value": values[spec["name"]], "passed": ok}
                )
        detail["measured"].append(entry)

    if contract.get("mode", "fix") == "improve":
        proceed = not (hard_failed or measured_failed)
        store.set_baseline_run(contract_id, "pass_improve" if proceed else "fail_improve")
    else:
        proceed = hard_failed or measured_failed
        store.set_baseline_run(contract_id, "fail" if proceed else "pass")
    worktree_mgr.remove(baseline_wt)
    return proceed, detail
