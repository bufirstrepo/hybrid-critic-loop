"""Lessons: derive them from real gate evidence, store them, inject them.

A lesson's outcome/cause/rule are *derived* from what the gate stack
actually measured — never invented prose.  A measured failure names the
metric, value and threshold; a hard failure names the command and exit
code; an escalation records the reason verbatim; a clean win records that
the gate stack decided it.  Route changes are only attached when a gate
actually eliminated a candidate whose expert is known.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .router import category_of

_MEASURED_CAUSES = {
    "alloc_bytes_per_tick": "per-tick allocation in the hot loop",
    "ops_per_tick": "per-tick recomputation in the hot loop (work per push grows with buffer size)",
    "tick_ms_p95": "per-frame work over the tick budget in the hot loop",
}

_MEASURED_RULES = {
    "alloc_bytes_per_tick": "pre-allocate buffers; check hot-loop allocations before the judged gate",
    "ops_per_tick": "keep per-tick work O(1): pre-allocate the buffer and maintain a running total instead of recomputing",
    "tick_ms_p95": "profile the hot loop and remove per-frame allocation/recomputation before the judged gate",
}

LESSONS_HEADER = "# Hybrid Critic Loop — lessons (plugin store)"


def _contract_id(contract, verdict):
    contract = contract or {}
    verdict = verdict or {}
    return (
        contract.get("id")
        or contract.get("contract_id")
        or verdict.get("contract_id")
        or "unknown"
    )


def derive_outcome_cause(contract, verdict, gate_details):
    """Derive (outcome, cause, rule, route_change, evidence) from gate evidence."""
    contract = contract or {}
    verdict = verdict or {}
    gate_details = gate_details or {}
    contract_id = _contract_id(contract, verdict)
    domain = contract.get("domain") or verdict.get("domain") or "code"
    category = category_of(domain)
    candidate_experts = gate_details.get("candidate_experts") or {}

    # 1. Measured gate failure — first failing measurement row wins.
    for row in gate_details.get("measurements") or []:
        if not isinstance(row, dict):
            continue
        # Store rows carry "pass"; raw gate rows carry "passed". Either
        # explicit False marks the measured failure.
        failed = row.get("pass") is False or (
            "pass" not in row and row.get("passed") is False
        )
        if not failed:
            continue
        candidate_id = row.get("candidate_id")
        metric = row.get("metric") or row.get("name")
        value = row.get("value")
        op = row.get("op")
        threshold = row.get("threshold", row.get("value_threshold"))
        outcome = (
            f"{candidate_id} failed the measured gate: {metric} {value}, "
            f"threshold {op} {threshold}"
        )
        cause = _MEASURED_CAUSES.get(
            metric, f"measured metric {metric} breached its threshold"
        )
        rule = _MEASURED_RULES.get(
            metric, f"check {metric} against its threshold before the judged gate"
        )
        expert = candidate_experts.get(candidate_id)
        route_change = (
            {"category": category, "demote": expert, "reason": "lost on measured gate"}
            if expert
            else None
        )
        evidence = [f"verdict:{contract_id}", f"measurement:{candidate_id}:{metric}"]
        return outcome, cause, rule, route_change, evidence

    # 2. Hard gate failure — first failing acceptance command.
    hard_failures = gate_details.get("hard_failures") or []
    if hard_failures:
        first = hard_failures[0]
        candidate_id = first.get("candidate_id")
        cmd = first.get("cmd")
        exit_code = first.get("exit_code")
        outcome = f"{candidate_id} failed the hard gate: {cmd} exited {exit_code}"
        cause = "acceptance command failed"
        rule = "run the contract's acceptance commands locally before submitting the candidate"
        expert = candidate_experts.get(candidate_id)
        route_change = (
            {"category": category, "demote": expert, "reason": "lost on hard gate"}
            if expert
            else None
        )
        evidence = [f"verdict:{contract_id}", f"hard_failure:{candidate_id}:{cmd}"]
        return outcome, cause, rule, route_change, evidence

    # 3. Escalation — no automatic learning beyond the recorded reason.
    escalation_reason = gate_details.get("escalation_reason")
    if escalation_reason:
        outcome = f"contract {contract_id} escalated: {escalation_reason}"
        cause = escalation_reason
        rule = "operator review required; no automatic route change"
        evidence = [f"verdict:{contract_id}"]
        return outcome, cause, rule, None, evidence

    # 4. Clean win.
    winner = (
        gate_details.get("winner_expert")
        or verdict.get("winner")
        or verdict.get("winner_expert")
        or "unknown"
    )
    outcome = f"{winner} won contract {contract_id}"
    cause = "decided by the gate stack"
    judged = gate_details.get("judged") or verdict.get("judged")
    if isinstance(judged, dict):
        median = judged.get("median", judged.get("median_score"))
        if median is not None:
            cause = f"decided by the gate stack (judged median: {median})"
    rule = "no failure recorded; keep current route"
    evidence = [f"verdict:{contract_id}"]
    return outcome, cause, rule, None, evidence


def write_lesson(store, data_dir, contract, verdict, gate_details):
    """Derive a lesson, store it, and append a readable block to lessons.md.

    The store row is the source of truth. The markdown mirror is written
    only when ``data_dir`` is a real location; ``None`` (an in-memory or
    path-less store) means no mirror — never the current directory.
    """
    contract = contract or {}
    contract_id = _contract_id(contract, verdict)
    domain = contract.get("domain") or "code"
    tags = list(contract.get("tags") or [])
    outcome, cause, rule, route_change, evidence = derive_outcome_cause(
        contract, verdict, gate_details
    )
    lesson_id = store.create_lesson(
        contract_id=contract_id,
        domain=domain,
        tags=tags,
        outcome=outcome,
        cause=cause,
        rule=rule,
        route_change=route_change,
        evidence=evidence,
        written_to="plugin_store",
        provenance="generated",
    )

    try:
        if data_dir is None:
            raise FileNotFoundError("no data dir: in-memory store, no lessons.md mirror")
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        lessons_file = data_dir / "lessons.md"
        if not lessons_file.exists():
            lessons_file.write_text(LESSONS_HEADER + "\n\n", encoding="utf-8")
        date = datetime.now(timezone.utc).date().isoformat()
        block = (
            f"## Lesson {lesson_id} — {date}\n\n"
            f"- id: {lesson_id}\n"
            f"- date: {date}\n"
            f"- contract: {contract_id}\n"
            f"- domain: {domain}\n"
            f"- rule: {rule}\n"
            f"- outcome: {outcome}\n"
            f"- cause: {cause}\n"
            f"- evidence: {', '.join(str(e) for e in evidence)}\n\n"
        )
        with lessons_file.open("a", encoding="utf-8") as fh:
            fh.write(block)
    except Exception:
        # The store row is the source of truth; the markdown mirror is a
        # readable extra and must not lose the lesson if the FS fails.
        pass

    get_lesson = getattr(store, "get_lesson", None)
    if callable(get_lesson):
        try:
            stored = get_lesson(lesson_id)
        except Exception:
            stored = None
        if stored:
            return stored
    return {
        "id": lesson_id,
        "contract_id": contract_id,
        "domain": domain,
        "tags": tags,
        "outcome": outcome,
        "cause": cause,
        "rule": rule,
        "route_change": route_change,
        "evidence": evidence,
        "written_to": "plugin_store",
        "provenance": "generated",
    }


def _lesson_line(lesson):
    lesson_id = lesson.get("id", lesson.get("lesson_id", "?"))
    rule = lesson.get("rule", "")
    outcome = lesson.get("outcome", "")
    evidence = lesson.get("evidence") or []
    if isinstance(evidence, (list, tuple)):
        evidence_text = ", ".join(str(e) for e in evidence)
    else:
        evidence_text = str(evidence)
    return f"- [lsn {lesson_id}] RULE: {rule} (from: {outcome}; evidence: {evidence_text})"


def lessons_context(store, domain, tags=None, limit=None, max_chars=10000):
    """Render prior lessons as injectable context, capped at ``max_chars``.

    The store ranks the lessons (domain match — exact first, then the same
    family — then tag overlap, then newest); with the default ``limit=None``
    every matching lesson is fetched and the block is filled to the cap on
    lesson boundaries, so the closing note counts the lessons the cap really
    dropped, not a fetch window (fix pass Wave 2.3, packet F4). Returns ""
    when there are no lessons.
    """
    try:
        lessons = store.lessons_for(domain=domain, tags=tags, limit=limit)
    except TypeError:
        lessons = store.lessons_for(domain, tags, limit)
    if not lessons:
        return ""
    header = f"## Lessons from prior Hybrid Critic Loop runs (domain: {domain})"
    lines = [header]
    total = len(header)
    shown = 0
    for lesson in lessons:
        line = _lesson_line(lesson)
        # Reserve room for the truncation note while more lessons remain.
        reserve = 64 if shown < len(lessons) - 1 else 0
        if total + 1 + len(line) > max_chars - reserve:
            break
        lines.append(line)
        total += 1 + len(line)
        shown += 1
    not_shown = len(lessons) - shown
    if not_shown > 0:
        note = f"({not_shown} more lessons not shown — {max_chars}-char injection cap)"
        if total + 1 + len(note) <= max_chars:
            lines.append(note)
        else:
            # Make room by dropping trailing lesson lines if needed.
            while len(lines) > 1 and total + 1 + len(note) > max_chars:
                removed = lines.pop()
                total -= 1 + len(removed)
                shown -= 1
                not_shown += 1
                note = f"({not_shown} more lessons not shown — {max_chars}-char injection cap)"
            lines.append(note)
    return "\n".join(lines)
