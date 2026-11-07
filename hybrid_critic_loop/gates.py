"""Decidable gates: provenance, hard (command exit), measured (sensors).

Gate order is a rank order, cheapest and most mechanical first:

1. provenance -- a candidate that is not a real generated artifact with
   a hashed evidence log never reaches a gate at all. This is the
   enforcement point that keeps mock / human artifacts out.
2. hard -- shell commands with an expected exit code.
3. measured -- sensor commands emitting JSON metrics, each compared
   against a threshold with an explicit operator. Every reading is
   stored, pass or fail, so a gate decision is always auditable.

Judged gates (rubrics + cross-family panels) live in judges.py and run
only on candidates that survive all three of these.
"""

from __future__ import annotations

import json
import operator
from dataclasses import dataclass, field

from .errors import GateError
from .shellrun import run_command

_OPS = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}

_TAIL = 2000


def compare(value, op, threshold) -> bool:
    """Compare ``value`` against ``threshold`` with a string operator."""
    fn = _OPS.get(op)
    if fn is None:
        raise GateError(f"unknown comparison op: {op!r}")
    return bool(fn(value, threshold))


@dataclass
class HardResult:
    cmd: str
    expected_exit: int
    exit_code: int | None
    passed: bool
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass
class MeasureResult:
    sensor: str
    metrics: list = field(default_factory=list)  # stored measurement rows, as dicts
    passed: bool = False


@dataclass
class CandidateGateResult:
    candidate_id: str
    provenance_ok: bool
    eliminated_at: str | None = None  # None | "provenance" | "hard" | "measured"
    hard: list = field(default_factory=list)
    measured: list = field(default_factory=list)
    hard_pass: bool = False
    measured_pass: bool | None = None  # None when no measured items or not reached


def _acceptance(contract: dict) -> list:
    if isinstance(contract, (list, tuple)):
        return list(contract)
    items = contract.get("acceptance")
    if items is None:
        items = contract.get("items", [])
    return list(items)


def _candidate_id(candidate: dict):
    cid = candidate.get("id")
    if cid is None:
        cid = candidate.get("candidate_id")
    return cid


def check_provenance(candidate: dict) -> bool:
    """True only for a generated candidate carrying a hashed evidence log.

    A mock or human artifact must never pass a gate; this check is the
    enforcement point and runs before any command is executed.
    """
    if not isinstance(candidate, dict):
        return False
    if candidate.get("provenance") != "generated":
        return False
    evidence = candidate.get("evidence")
    if not isinstance(evidence, dict):
        return False
    return bool(evidence.get("log_hash"))


def run_hard_items(contract: dict, cwd) -> list:
    """Run every hard acceptance item; passed iff exit == expected exit."""
    results = []
    for item in _acceptance(contract):
        if item.get("class") != "hard":
            continue
        cmd = item["cmd"]
        expected = (item.get("expect") or {}).get("exit", 0)
        res = run_command(cmd, cwd, timeout_s=item.get("timeout_s", 900))
        results.append(
            HardResult(
                cmd=cmd,
                expected_exit=expected,
                exit_code=res.exit_code,
                passed=(res.exit_code == expected),
                stdout_tail=(res.stdout or "")[-_TAIL:],
                stderr_tail=(res.stderr or "")[-_TAIL:],
            )
        )
    return results


def run_measured_items(store, contract: dict, candidate_id, cwd) -> list:
    """Run every measured acceptance item and store every reading.

    A sensor command whose stdout is not a JSON object of metrics, or
    that omits a metric the contract names, is a GateError -- never a
    silent pass or a fabricated zero.
    """
    results = []
    for item in _acceptance(contract):
        if item.get("class") != "measured":
            continue
        sensor_cmd = item["sensor_cmd"]
        sensor = item.get("sensor", item["sensor_cmd"])
        res = run_command(sensor_cmd, cwd, timeout_s=item.get("timeout_s", 900))
        raw = res.stdout or ""
        try:
            parsed = json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - surface raw tail as evidence
            raise GateError(
                f"sensor {sensor_cmd!r} produced unparseable output: {exc}; "
                f"raw tail: {raw[-500:]!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise GateError(
                f"sensor {sensor_cmd!r} produced non-object JSON; "
                f"raw tail: {raw[-500:]!r}"
            )
        rows = []
        all_passed = True
        for spec in item["metrics"]:
            name = spec["name"]
            if name not in parsed:
                raise GateError(
                    f"sensor {sensor_cmd!r} did not report metric {name!r}; "
                    f"got keys {sorted(parsed)}"
                )
            value = parsed[name]
            passed_metric = compare(value, spec["op"], spec["value"])
            store.add_measurement(
                candidate_id, sensor, name, value, spec["op"], spec["value"], passed_metric
            )
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "sensor": sensor,
                    "metric": name,
                    "value": value,
                    "op": spec["op"],
                    "threshold": spec["value"],
                    "passed": passed_metric,
                }
            )
            all_passed = all_passed and passed_metric
        results.append(MeasureResult(sensor=sensor, metrics=rows, passed=all_passed))
    return results


def evaluate_candidate(store, contract: dict, candidate: dict, cwd) -> CandidateGateResult:
    """Evaluate one candidate through provenance -> hard -> measured.

    Elimination is sticky and ordered: a provenance failure runs no
    commands at all; a hard failure skips the measured sensors.
    """
    candidate_id = _candidate_id(candidate)
    if not check_provenance(candidate):
        return CandidateGateResult(
            candidate_id=candidate_id,
            provenance_ok=False,
            eliminated_at="provenance",
            hard=[],
            measured=[],
            hard_pass=False,
            measured_pass=None,
        )

    hard = run_hard_items(contract, cwd)
    hard_pass = all(h.passed for h in hard)
    if not hard_pass:
        return CandidateGateResult(
            candidate_id=candidate_id,
            provenance_ok=True,
            eliminated_at="hard",
            hard=hard,
            measured=[],
            hard_pass=False,
            measured_pass=None,
        )

    items = _acceptance(contract)
    if any(i.get("class") == "measured" for i in items):
        measured = run_measured_items(store, contract, candidate_id, cwd)
        measured_pass = all(m.passed for m in measured)
        return CandidateGateResult(
            candidate_id=candidate_id,
            provenance_ok=True,
            eliminated_at=None if measured_pass else "measured",
            hard=hard,
            measured=measured,
            hard_pass=True,
            measured_pass=measured_pass,
        )

    return CandidateGateResult(
        candidate_id=candidate_id,
        provenance_ok=True,
        eliminated_at=None,
        hard=hard,
        measured=[],
        hard_pass=True,
        measured_pass=None,
    )
