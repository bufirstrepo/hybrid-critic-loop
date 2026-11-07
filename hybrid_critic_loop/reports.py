"""Human-readable reports.  Reports print ONLY database rows.

Anything that was never recorded prints "not run" / "not reported" rather
than an invented number — the ledger, verdict, and lessons stores are the
only sources here.

v0.3 additions: the ledger carries cost-per-validity-point (cpvp) and,
when recorded, calibration; the validity matrix (rubric
``validity_matrix_v1``, gate ``"matrix"`` score rows) renders both as
the plugin's JSON view (:func:`build_matrix_view`, consumed by the
``critic_matrix`` tool) and as text (:func:`matrix_report`); verdict
reports surface dissent entries and panel status when the judged block
recorded them.  All v0.3 keys are read defensively (``.get`` / hasattr)
so these reports work against stores that predate them.
"""

from __future__ import annotations

import statistics

MATRIX_RUBRIC_ID = "validity_matrix_v1"

# Canonical validity-matrix rows, used only when neither the rubric
# registry nor any recorded matrix score row names the rows.  Rows are
# otherwise taken from the validity_matrix_v1 rubric (when a sibling
# module registers it) and/or the recorded score items themselves.
MATRIX_ROWS = ["correctness", "completeness", "evidence", "reproducibility"]

_DEFAULT_DISSENT_THRESHOLD = 3.0


def _win_rate_text(row):
    win_rate = row.get("win_rate")
    if win_rate is None:
        return "not run"
    wins = row.get("wins")
    losses = row.get("losses")
    try:
        rate_text = f"{float(win_rate):.2f}"
    except (TypeError, ValueError):
        rate_text = str(win_rate)
    if wins is not None and losses is not None:
        return f"{wins}W-{losses}L ({rate_text})"
    return rate_text


def _value_or_not_reported(value):
    return "not reported" if value is None else str(value)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _format_number(value):
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------

def _cpvp_text(row):
    value = row.get("cost_per_validity_point")
    if value is None:
        return "not reported"
    return _format_number(value)


def _calibration_text(row):
    """``cal=<value>(n=<n>)`` when the summary row carries calibration."""
    cal = None
    n = None
    for key in ("calibration", "judge_calibration", "calibration_score", "cal"):
        if row.get(key) is not None:
            cal = row.get(key)
            break
    if isinstance(cal, dict):
        for n_key in ("n", "count", "samples"):
            if cal.get(n_key) is not None:
                n = cal.get(n_key)
                break
        for v_key in ("value", "score", "calibration", "mean"):
            if cal.get(v_key) is not None:
                cal = cal.get(v_key)
                break
        if isinstance(cal, dict):
            cal = None
    if cal is None:
        return None
    if n is None:
        for key in ("calibration_n", "calibration_count",
                    "calibration_samples", "cal_n", "n"):
            if row.get(key) is not None:
                n = row.get(key)
                break
    n_text = str(n) if n is not None else "not reported"
    return f"cal={_format_number(cal)}(n={n_text})"


def _events_lines(store):
    """Text lines for the store's event log, when the store has one.

    Defensive by design: stores without ``events_for`` (it lands in a
    sibling workstream) simply produce no events section.
    """
    if not hasattr(store, "events_for"):
        return []
    try:
        events = store.events_for(limit=50)
    except TypeError:
        try:
            events = store.events_for(50)
        except Exception:
            return []
    except Exception:
        return []
    if not events:
        return ["events: not run"]
    lines = ["events:"]
    for event in events:
        if isinstance(event, dict):
            parts = []
            for key in ("id", "kind", "type", "contract_id", "candidate_id",
                        "expert_id", "detail", "message", "created_at"):
                if event.get(key) is not None:
                    parts.append(f"{key}={event[key]}")
            lines.append(", ".join(parts) if parts else str(event))
        else:
            lines.append(str(event))
    return lines


def ledger_report(store) -> str:
    """Text table of the expert ledger, or "ledger: not run"."""
    try:
        rows = store.ledger_rows()
    except TypeError:
        rows = store.ledger_rows(None)
    try:
        summary = store.ledger_summary()
    except Exception:
        summary = []
    if not rows and not summary:
        text = "ledger: not run"
        events = _events_lines(store)
        return "\n".join([text] + events) if events else text
    lines = ["ledger:"]
    for row in summary or []:
        expert = row.get("expert_id", row.get("expert", "?"))
        line = (
            f"{expert}: dispatches={row.get('dispatches', 0)} "
            f"completed={row.get('completed', 0)} "
            f"refused={row.get('refused', 0)} "
            f"timeout={row.get('timeout', row.get('timeouts', row.get('timed_out', 0)))} "
            f"tokens_in={_value_or_not_reported(row.get('tokens_in'))} "
            f"tokens_out={_value_or_not_reported(row.get('tokens_out'))} "
            f"cost_usd={_value_or_not_reported(row.get('cost_usd'))} "
            f"win_rate={_win_rate_text(row)} "
            f"cpvp={_cpvp_text(row)}"
        )
        if row.get("validity_points") is not None:
            line += f" validity_points={row.get('validity_points')}"
        cal_text = _calibration_text(row)
        if cal_text is not None:
            line += f" {cal_text}"
        lines.append(line)
    if not summary and rows:
        # Rows exist but no summary implementation produced aggregates.
        for row in rows:
            lines.append(str(row))
    lines.extend(_events_lines(store))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Validity matrix
# --------------------------------------------------------------------------

def _matrix_rubric_rows():
    try:
        from .rubrics import get_rubric

        rubric = get_rubric(MATRIX_RUBRIC_ID)
        items = rubric.get("items") if isinstance(rubric, dict) else None
        return list(items) if items else []
    except Exception:
        return []


def _dissent_threshold_for(store, contract_id):
    """The contract's judged dissent_threshold, defaulting to 3.0."""
    get_contract = getattr(store, "get_contract", None)
    if callable(get_contract):
        try:
            contract = get_contract(contract_id)
        except Exception:
            contract = None
        if isinstance(contract, dict):
            for item in contract.get("acceptance") or []:
                if isinstance(item, dict) and item.get("class") == "judged":
                    value = item.get("dissent_threshold")
                    if _is_number(value) and float(value) >= 0:
                        return float(value)
                    break
    return _DEFAULT_DISSENT_THRESHOLD


def _parse_matrix_cell(score_row):
    """Split one matrix score row into (items, confidence, details).

    Tolerant of the shapes a score row's ``items`` may take: bare
    per-row numbers, per-row dicts carrying score/confidence/evidence,
    and cell-level ``confidence`` / ``details`` carried either as score
    columns or as meta keys inside ``items``.
    """
    items = {}
    confidences = []
    details = {}
    if _is_number(score_row.get("confidence")):
        confidences.append(float(score_row["confidence"]))
    if isinstance(score_row.get("details"), dict):
        details.update(score_row["details"])
    raw_items = score_row.get("items")
    if isinstance(raw_items, dict):
        for key, value in raw_items.items():
            if key in ("confidence", "_confidence"):
                if _is_number(value):
                    confidences.append(float(value))
                continue
            if key in ("details", "_details"):
                if isinstance(value, dict):
                    details.update(value)
                continue
            if isinstance(value, dict):
                score = value.get("score", value.get("value"))
                if _is_number(score):
                    items[key] = float(score)
                if _is_number(value.get("confidence")):
                    confidences.append(float(value["confidence"]))
                detail = value.get(
                    "details", value.get("evidence", value.get("rationale"))
                )
                if detail is not None:
                    details[key] = detail
            elif _is_number(value):
                items[key] = float(value)
    confidence = None
    if confidences:
        confidence = (
            confidences[0]
            if len(set(confidences)) == 1
            else sum(confidences) / len(confidences)
        )
    return items, confidence, details


def _normalize_dissent(raw, default_candidate=None):
    """Canonicalize one dissent entry, keeping any extra recorded keys."""
    entry = dict(raw) if isinstance(raw, dict) else {}
    candidate = (
        entry.get("candidate_id") or entry.get("candidate") or default_candidate
    )
    judge = entry.get("judge_id") or entry.get("judge")
    entry["candidate_id"] = candidate
    entry["candidate"] = candidate
    entry["row"] = entry.get("row", entry.get("item"))
    entry["judge_id"] = judge
    entry["judge"] = judge
    if entry.get("score") is None:
        entry["score"] = entry.get("value")
    if entry.get("median") is None:
        entry["median"] = entry.get("row_median", entry.get("median_score"))
    if entry.get("rationale") is None:
        entry["rationale"] = entry.get("reason", entry.get("evidence"))
    return entry


def _collect_dissent(judged):
    """All dissent entries recorded in a verdict's judged block."""
    entries = []
    if not isinstance(judged, dict):
        return entries

    def _take(container, default_candidate=None):
        if isinstance(container, dict) and isinstance(
            container.get("dissent"), list
        ):
            for raw in container["dissent"]:
                if isinstance(raw, dict):
                    entries.append(_normalize_dissent(raw, default_candidate))

    _take(judged)
    for key in ("candidates", "per_candidate"):
        container = judged.get(key)
        if isinstance(container, dict):
            for cid, sub in container.items():
                _take(sub, cid)
    for key, sub in judged.items():
        if key not in ("candidates", "per_candidate"):
            _take(sub, key)
    return entries


def _candidate_judged(judged, cid):
    if not isinstance(judged, dict):
        return {}
    direct = judged.get(cid)
    if isinstance(direct, dict):
        return direct
    for key in ("candidates", "per_candidate"):
        container = judged.get(key)
        if isinstance(container, dict) and isinstance(container.get(cid), dict):
            return container[cid]
    return {}


def _row_results_for(judged, cid, rows):
    sub = _candidate_judged(judged, cid)
    if isinstance(sub.get("row_results"), (dict, list)):
        return sub["row_results"]
    if isinstance(sub.get("rows"), (dict, list)):
        return sub["rows"]
    by_row = {k: sub[k] for k in sub if k in set(rows)}
    if by_row:
        return by_row
    if isinstance(judged, dict):
        top = judged.get("row_results")
        if isinstance(top, dict) and isinstance(top.get(cid), (dict, list)):
            return top[cid]
    return {}


def build_matrix_view(store, contract_id, attempt=None) -> dict:
    """Assemble the validity-matrix view for one contract from DB rows.

    Returns the JSON-able dict the ``critic_matrix`` tool serves:
    candidates x peer judges, one cell per judge with per-row scores,
    plus per-candidate row results and dissent (recorded in the verdict
    and/or computed from the recorded scores against the contract's
    dissent_threshold, default 3.0).  ``note`` is "not run" when no
    matrix scores exist.
    """
    candidates = []
    candidates_for = getattr(store, "candidates_for", None)
    if callable(candidates_for):
        try:
            candidates = candidates_for(contract_id, attempt)
        except TypeError:
            try:
                candidates = candidates_for(contract_id)
            except Exception:
                candidates = []
        except Exception:
            candidates = []
    if attempt is not None and candidates:
        candidates = [
            c for c in candidates
            if not (isinstance(c, dict) and c.get("attempt") is not None)
            or c.get("attempt") == attempt
        ]

    verdict = None
    latest_verdict = getattr(store, "latest_verdict", None)
    if callable(latest_verdict):
        try:
            verdict = latest_verdict(contract_id)
        except Exception:
            verdict = None
    judged = verdict.get("judged") if isinstance(verdict, dict) else None
    if not isinstance(judged, dict):
        judged = None

    panel_status = None
    if judged is not None and judged.get("panel_status") is not None:
        panel_status = judged.get("panel_status")
    elif isinstance(verdict, dict) and verdict.get("panel_status") is not None:
        panel_status = verdict.get("panel_status")

    threshold = _dissent_threshold_for(store, contract_id)
    recorded_dissent = _collect_dissent(judged)

    scores_for = getattr(store, "scores_for", None)
    view_candidates = {}
    observed_rows = []
    has_matrix_scores = False
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        cid = candidate.get("id") or candidate.get("candidate_id")
        if cid is None:
            continue
        cells = {}
        if callable(scores_for):
            try:
                score_rows = scores_for(cid) or []
            except Exception:
                score_rows = []
        else:
            score_rows = []
        for score_row in score_rows:
            if not isinstance(score_row, dict):
                continue
            gate = score_row.get("gate")
            if gate is not None and gate != "matrix":
                continue
            items, confidence, details = _parse_matrix_cell(score_row)
            judge_id = score_row.get("judge_id")
            cells[judge_id] = {
                "judge_family": score_row.get("judge_family"),
                "items": items,
                "confidence": confidence,
                "details": details,
                "mean": score_row.get("mean"),
            }
            has_matrix_scores = True
            for row_name in items:
                if row_name not in observed_rows:
                    observed_rows.append(row_name)

        # Dissent: recorded entries for this candidate plus any judge
        # whose row score deviates from the row median past threshold.
        dissent = []
        seen = set()
        for entry in recorded_dissent:
            if entry.get("candidate_id") in (None, cid):
                key = (entry.get("row"), entry.get("judge_id"))
                if key not in seen:
                    seen.add(key)
                    dissent.append(entry)
        for row_name in observed_rows:
            row_scores = [
                (judge_id, cell["items"][row_name])
                for judge_id, cell in cells.items()
                if row_name in cell["items"]
            ]
            if len(row_scores) < 2:
                continue
            median = float(
                statistics.median([s for _, s in row_scores])
            )
            for judge_id, score in row_scores:
                deviation = abs(score - median)
                if deviation > threshold and (row_name, judge_id) not in seen:
                    seen.add((row_name, judge_id))
                    dissent.append(
                        _normalize_dissent(
                            {
                                "candidate_id": cid,
                                "row": row_name,
                                "judge_id": judge_id,
                                "score": score,
                                "median": median,
                                "deviation": deviation,
                                "rationale": (
                                    f"score {score} deviates from row median "
                                    f"{median} by {deviation:.2f} "
                                    f"(> dissent_threshold {threshold})"
                                ),
                            },
                            cid,
                        )
                    )
        view_candidates[cid] = {
            "expert_id": candidate.get("expert_id"),
            "family": candidate.get("family"),
            "cells": cells,
            "row_results": _row_results_for(judged, cid, observed_rows),
            "dissent": dissent,
        }

    rows = _matrix_rubric_rows()
    for row_name in observed_rows:
        if row_name not in rows:
            rows.append(row_name)
    if not rows:
        rows = list(MATRIX_ROWS)

    return {
        "contract_id": contract_id,
        "rubric": MATRIX_RUBRIC_ID,
        "rows": rows,
        "candidates": view_candidates,
        "panel_status": panel_status,
        "note": None if has_matrix_scores else "not run",
    }


def matrix_report(store, contract_id) -> str:
    """Text rendering of the validity matrix, or "matrix: not run"."""
    view = build_matrix_view(store, contract_id)
    if view["note"] == "not run":
        return "matrix: not run"
    panel_status = view["panel_status"]
    lines = [
        f"matrix for {contract_id} ({view['rubric']}):",
        f"panel_status: {panel_status if panel_status is not None else 'not reported'}",
    ]
    for cid, candidate in view["candidates"].items():
        lines.append(
            f"candidate {cid} "
            f"(expert={candidate.get('expert_id')}, "
            f"family={candidate.get('family')}):"
        )
        dissent_keys = {
            (d.get("row"), d.get("judge_id")) for d in candidate["dissent"]
        }
        for row_name in view["rows"]:
            cells = []
            for judge_id, cell in candidate["cells"].items():
                if row_name in cell["items"]:
                    text = f"{judge_id}={cell['items'][row_name]}"
                    if (row_name, judge_id) in dissent_keys:
                        text += " DISSENT"
                    cells.append(text)
            lines.append(
                f"{row_name}: {', '.join(cells) if cells else 'not reported'}"
            )
        for entry in candidate["dissent"]:
            lines.append(
                f"dissent: candidate={entry.get('candidate')} "
                f"row={entry.get('row')} judge={entry.get('judge')} "
                f"score={_value_or_not_reported(entry.get('score'))} "
                f"vs median={_value_or_not_reported(entry.get('median'))} "
                f"— {_value_or_not_reported(entry.get('rationale'))}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Verdict / lessons
# --------------------------------------------------------------------------

def _judged_summary(judged):
    if judged is None:
        return "not run"
    if isinstance(judged, dict):
        parts = []
        for key in ("median", "median_score", "min", "winner", "candidate_id"):
            if judged.get(key) is not None:
                parts.append(f"{key}={judged[key]}")
        if parts:
            return ", ".join(parts)
        return str(judged)
    return str(judged)


def verdict_report(store, contract_id) -> str:
    """Lines from the latest verdict row, or "verdict: not run"."""
    verdict = None
    try:
        verdict = store.latest_verdict(contract_id)
    except Exception:
        verdict = None
    if not verdict:
        return "verdict: not run"
    eliminated = verdict.get("eliminated")
    if isinstance(eliminated, (list, tuple)):
        eliminated_text = ", ".join(str(e) for e in eliminated) or "none"
    else:
        eliminated_text = str(eliminated) if eliminated is not None else "not run"
    lines = [
        f"verdict for {contract_id}:",
        f"winner: {verdict.get('winner')}",
        f"eliminated: {eliminated_text}",
        f"hard: {verdict.get('hard')}",
        f"measured: {verdict.get('measured')}",
        f"judged: {_judged_summary(verdict.get('judged'))}",
        f"escalated: {verdict.get('escalated')}",
    ]
    if verdict.get("escalation_reason"):
        lines.append(f"escalation_reason: {verdict.get('escalation_reason')}")
    judged = verdict.get("judged")
    for entry in _collect_dissent(judged):
        lines.append(
            f"dissent: candidate={entry.get('candidate')} "
            f"row={entry.get('row')} judge={entry.get('judge')} "
            f"score={_value_or_not_reported(entry.get('score'))} "
            f"vs median={_value_or_not_reported(entry.get('median'))} "
            f"— {_value_or_not_reported(entry.get('rationale'))}"
        )
    panel_status = None
    if isinstance(judged, dict) and judged.get("panel_status") is not None:
        panel_status = judged.get("panel_status")
    elif verdict.get("panel_status") is not None:
        panel_status = verdict.get("panel_status")
    if panel_status is not None:
        lines.append(f"panel_status: {panel_status}")
    hold = verdict.get("route_hold")
    if isinstance(hold, dict) and hold:
        change = hold.get("route_change") or {}
        lines.append(
            f"route_change: held ({hold.get('panel_status') or 'degraded'} panel) — "
            f"demote {_value_or_not_reported(change.get('demote'))} not applied; "
            f"operator review required (lesson {_value_or_not_reported(hold.get('lesson_id'))})"
        )
    lines.append(f"human_gate: {verdict.get('human_gate')}")
    return "\n".join(lines)


def lessons_report(store, domain=None, tags=None, limit=10) -> str:
    """Numbered lesson lines, newest first, or "lessons: not run"."""
    try:
        lessons = store.lessons_for(domain=domain, tags=tags, limit=limit)
    except TypeError:
        lessons = store.lessons_for(domain, tags, limit)
    if not lessons:
        return "lessons: not run"
    lines = []
    for i, lesson in enumerate(lessons, start=1):
        lesson_id = lesson.get("id", lesson.get("lesson_id", "?"))
        lines.append(
            f"{i}. [{lesson_id}] {lesson.get('domain')}: "
            f"{lesson.get('rule')} — {lesson.get('outcome')}"
        )
    return "\n".join(lines)
