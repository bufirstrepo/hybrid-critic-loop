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
from pathlib import Path

from .errors import ContractError
from .shellrun import run_command

_OPS = {"==", "!=", "<", "<=", ">", ">="}
_STAGES = {"spec", "decompose", "implement", "integrate"}


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
    task = raw.get("task")
    if not task or not isinstance(task, str):
        raise ContractError("contract.task is required (string)")
    domain = raw.get("domain")
    if not domain or not isinstance(domain, str):
        raise ContractError("contract.domain is required (string)")
    stage = raw.get("stage", "implement")
    if stage not in _STAGES:
        raise ContractError(f"contract.stage must be one of {sorted(_STAGES)}")
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
            expect = item.get("expect") or {}
            exit_code = expect.get("exit", 0)
            if not isinstance(exit_code, int):
                raise ContractError(f"acceptance[{i}] expect.exit must be an int")
            normalized.append({"class": "hard", "cmd": cmd, "expect": {"exit": exit_code}})
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
                if m["op"] not in _OPS:
                    raise ContractError(f"acceptance[{i}].metrics[{j}] op must be one of {sorted(_OPS)}")
                norm_metrics.append({"name": m["name"], "op": m["op"], "value": m["value"]})
            normalized.append(
                {
                    "class": "measured",
                    "sensor": item.get("sensor", ""),
                    "sensor_cmd": sensor_cmd,
                    "metrics": norm_metrics,
                }
            )
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
                    "judges": int(item.get("judges", 3)),
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

    return {
        "id": raw.get("id"),
        "target_id": raw.get("target_id"),
        "stage": stage,
        "attempt": int(raw.get("attempt", 1)),
        "task": task,
        "domain": domain,
        "tags": list(raw.get("tags", [])),
        "depends_on": list(raw.get("depends_on", [])),
        "acceptance": normalized,
        "test_author_family": raw.get("test_author_family"),
        "target_file": raw.get("target_file"),
        "ask": raw.get("ask"),
        "constraints": list(raw.get("constraints", [])),
        "budget": raw.get("budget"),
        "bracket": bracket,
    }


def verify_baseline(store, contract: dict, repo_path, worktree_mgr) -> tuple[bool, dict]:
    """Run acceptance against a pristine baseline worktree.

    Returns (proceed, detail). Proceed is True iff the acceptance suite
    FAILS on the baseline in the expected way: at least one hard item
    fails, or (with no hard items) at least one measured metric fails.
    Records acceptance_baseline_run on the contract row.
    """
    from .gates import compare  # lazy: gates is a sibling workstream

    contract_id = contract["id"]
    baseline_wt = worktree_mgr.create(contract_id, "baseline")
    detail: dict = {"hard": [], "measured": []}
    hard_items = [a for a in contract["acceptance"] if a["class"] == "hard"]
    measured_items = [a for a in contract["acceptance"] if a["class"] == "measured"]

    hard_failed = False
    for item in hard_items:
        res = run_command(item["cmd"], baseline_wt)
        passed = res.exit_code == item["expect"]["exit"]
        hard_failed = hard_failed or not passed
        detail["hard"].append(
            {"cmd": item["cmd"], "exit_code": res.exit_code, "passed": passed}
        )

    measured_failed = False
    for item in measured_items:
        res = run_command(item["sensor_cmd"], baseline_wt)
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
                ok = compare(values[spec["name"]], spec["op"], spec["value"])
                measured_failed = measured_failed or not ok
                entry["metrics"].append(
                    {"name": spec["name"], "value": values[spec["name"]], "passed": ok}
                )
        detail["measured"].append(entry)

    proceed = hard_failed or (not hard_items and measured_failed)
    store.set_baseline_run(contract_id, "fail" if proceed else "pass")
    worktree_mgr.remove(baseline_wt)
    return proceed, detail
