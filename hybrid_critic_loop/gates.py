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

import fnmatch
import json
import operator
import os
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

_ORDERED_OPS = ("<", "<=", ">", ">=")

_TAIL = 2000


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compare(value, op, threshold) -> bool:
    """Compare ``value`` against ``threshold`` with a string operator.

    Type discipline (fix pass round 4, spec G7, round-4 security F8 /
    contest-core F6): a measured value is a number. A boolean on either
    side, a non-numeric value under an ordered op, or a numeric /
    non-numeric mix under == / != raises GateError — the gate path
    records it as that candidate's gate failure (the loop converts it
    to an elimination), never a TypeError escaping the gate and never
    a silent ``True == 1`` pass. Two non-numeric values may still be
    compared with == / != (a string metric against a string threshold
    is a legitimate equality check).
    """
    fn = _OPS.get(op)
    if fn is None:
        raise GateError(f"unknown comparison op: {op!r}")
    if isinstance(value, bool) or isinstance(threshold, bool):
        raise GateError(
            f"measured comparison {op!r} got a boolean operand "
            f"(value={value!r}, threshold={threshold!r}); measured values are numbers"
        )
    if op in _ORDERED_OPS:
        if not _is_number(value) or not _is_number(threshold):
            raise GateError(
                f"measured comparison {op!r} needs numeric operands, got "
                f"value={value!r} ({type(value).__name__}) and "
                f"threshold={threshold!r} ({type(threshold).__name__})"
            )
    elif _is_number(value) != _is_number(threshold):
        raise GateError(
            f"measured comparison {op!r} mixes a numeric and a non-numeric "
            f"operand (value={value!r}, threshold={threshold!r})"
        )
    return bool(fn(value, threshold))


def exit_matches(exit_code, expected) -> bool:
    """Strict exit-code comparison (fix pass round 4, spec G7, round-4
    contest-core F11): a boolean expectation is only ever satisfied by
    the identical boolean — never by an integer exit code (``1 == True``
    must not pass a hard gate). Integer expectations compare as ints."""
    if isinstance(expected, bool):
        return exit_code is expected
    return exit_code == expected


# ------------------------------------------------------- gate environment

_SECRET_ENV_PATTERNS = (
    "*_KEY", "*_TOKEN", "*_SECRET", "*PASSWORD*",
    # Round-5 fix spec H6 (round-5 security F3): suffix carriers the
    # original four missed — anything ending in credentials or a DSN.
    "*_CREDENTIALS", "*_DSN",
)

#: Exact names stripped regardless of suffix shape (round-5 spec H6,
#: round-5 security F3): standard credential carriers that end in
#: none of the patterns — a database URL carries an embedded password,
#: the Google variable points at a credential FILE, and the AWS pair
#: ends in _ID / is named outright.
_SECRET_ENV_NAMES = frozenset({
    "DATABASE_URL",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
})


def scrubbed_env_names(env=None) -> list:
    """Environment variable names removed for gate/sensor commands.

    Fix pass round 4 (spec G7, round-4 security F1): a contract is an
    arbitrary-command document whose gate commands start running at
    baseline time; they do not get the operator's secret-bearing
    environment. Keys matching *_KEY, *_TOKEN, *_SECRET or *PASSWORD*
    are stripped from the env copy handed to gate commands. Vendor CLI
    processes use a separate minimal allowlist.

    Round-5 spec H6 (round-5 security F3) adds the *_CREDENTIALS and
    *_DSN suffixes and the _SECRET_ENV_NAMES exact names.  Honest
    scope: this scrubs the child's OWN environment — a gate command
    runs as the operator and can still read the parent process's
    environment (e.g. /proc), so the scrub is hygiene against casual
    inheritance; the code-execution disclosure is the real control.
    """
    source = os.environ if env is None else env
    return sorted(
        name for name in source
        if name in _SECRET_ENV_NAMES
        or any(fnmatch.fnmatchcase(name, pattern)
               for pattern in _SECRET_ENV_PATTERNS)
    )


def run_gate_command(cmd: str, cwd, timeout_s=900):
    """Run a gate/sensor command with a scrubbed explicit environment.

    The child retains ordinary variables but not known secret names. This is
    hygiene, not a sandbox: commands run with the operator's authority.
    Python bytecode generation is disabled to preserve the candidate tree.
    """
    # An explicit, scrubbed copy avoids inheriting provider secrets. Disable
    # Python bytecode output so ordinary test discovery does not mutate the
    # candidate tree that must remain identical to the judged snapshot.
    child_env = {k: v for k, v in os.environ.items() if k not in scrubbed_env_names()}
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    return run_command(cmd, cwd, timeout_s=timeout_s, env=child_env)



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
    """Run every hard acceptance item; passed iff exit == expected exit.

    Commands run with the scrubbed gate environment (see
    run_gate_command), each bounded by the item's own timeout_s, and
    the exit comparison is type-strict (see exit_matches).
    """
    results = []
    for item in _acceptance(contract):
        if item.get("class") != "hard":
            continue
        cmd = item["cmd"]
        expected = (item.get("expect") or {}).get("exit", 0)
        res = run_gate_command(cmd, cwd, timeout_s=item.get("timeout_s", 900))
        results.append(
            HardResult(
                cmd=cmd,
                expected_exit=expected,
                exit_code=res.exit_code,
                passed=exit_matches(res.exit_code, expected),
                stdout_tail=(res.stdout or "")[-_TAIL:],
                stderr_tail=(res.stderr or "")[-_TAIL:],
            )
        )
    return results


def run_measured_items(store, contract: dict, candidate_id, cwd) -> list:
    """Run every measured acceptance item and store every reading.

    A sensor command whose stdout is not a JSON object of metrics, or
    that omits a metric the contract names, is a GateError -- never a
    silent pass or a fabricated zero. So is a reading whose type the
    comparison cannot judge (see compare). An item's rows are stored
    only once the WHOLE item has been read and compared (fix pass
    round 4, spec G7, round-4 contest-core F6): a multi-metric item
    records all of its rows or none, so a gate that errored midway
    leaves no partial evidence behind. Sensor commands run with the
    scrubbed gate environment, bounded by the item's timeout_s.
    """
    results = []
    for item in _acceptance(contract):
        if item.get("class") != "measured":
            continue
        sensor_cmd = item["sensor_cmd"]
        sensor = item.get("sensor", item["sensor_cmd"])
        res = run_gate_command(sensor_cmd, cwd, timeout_s=item.get("timeout_s", 900))
        if res.timed_out or res.exit_code != 0:
            raise GateError(
                f"sensor {sensor_cmd!r} failed (exit={res.exit_code}, "
                f"timed_out={res.timed_out}); no readings accepted"
            )
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
        # The item is fully read: only now do its rows become evidence.
        for row in rows:
            store.add_measurement(
                candidate_id, sensor, row["metric"], row["value"],
                row["op"], row["threshold"], row["passed"],
            )
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
