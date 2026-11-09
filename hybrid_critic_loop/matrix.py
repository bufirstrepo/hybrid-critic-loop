"""Peer validity matrix: the v0.3 judged gate as peer review.

Every family in the panel scores every *other* family's candidate,
blind, on the same four criteria rows; nobody scores itself. The
self-cell exclusion is enforced here in code -- :func:`build_matrix_cells`
is the queue-side artifact that strips self-cells before dispatch, and
:func:`run_matrix` only ever builds packets from a peer's eligible
(non-self) candidates, so a self-cell cannot be scored even by a
hand-built executor dict.

Rows (``validity_matrix_v1``):

* ``question_fidelity`` -- scored against the original contract task /
  acceptance text, which the packet carries verbatim, never against a
  candidate's restatement of it.
* ``completeness`` -- every acceptance item addressed.
* ``evidence`` -- every claim / API points at a file:line, a test, or a
  measurement.
* ``test_agreement`` -- the judge is shown the hard / measured results
  and cannot pass what the tests failed (the matrix only ever runs on
  hard/measured survivors; it never overrules those gates).

Every cell carries score 0-10, confidence 0-1, an evidence pointer and
a one-line rationale per row. A cell missing any of those for any row
is discarded whole: no rationale / evidence, no score.

Aggregation: per row, the weighted median across peers, each peer
weighted by its *effective* judge calibration times the cell's
confidence for that row; per candidate, the weighted median of per-peer
means (weight = effective calibration x mean cell confidence). Pass
rule (unchanged): median-of-means >= min AND every row's weighted
median >= min - 2.

Dissent: a cell further than ``dissent_threshold`` from its row's
weighted median is flagged and returned with its evidence and
rationale. When a dissent cites a gate the majority may have missed --
a measurement metric name or a hard test command appearing in its
evidence / rationale -- :func:`run_matrix` reports it in
``rerun_requests`` so the loop can re-run that gate class before
merging. Detection only; this module never re-runs gates itself.

Panel floor: a candidate scored by fewer than 2 distinct peer families
is DEGRADED (``panel_status == "degraded"``). Low mean confidence
(< 0.4) or a peer-mean spread above threshold escalates the outcome
instead of letting the candidate pass quietly.

Calibration (:func:`compute_calibration`) is agreement-based, with
Laplace smoothing toward the 0.5 prior; weighting only activates once a
family has >= 5 scored cells on decidable candidates.

NAMED GAP -- GAP-STYLE-PREFERENCE-PENALTY: the v0.3 owner/spec text
defines a style-preference penalty via a proxy (a family's mean score
to candidates in contests where it also had a candidate, minus the
panel median for those candidates). On inspection that proxy is not
coherent as a *style-preference* measure, so the penalty is NOT
applied here:

* In a peer matrix, every scoring family is a contestant by
  construction, so "contests where F also had a candidate" is *all*
  matrix contests. The proxy therefore reduces to F's overall leniency
  versus the panel median -- it contains no information about style
  similarity between F's own output and the candidates F favours,
  because no style-similarity quantity enters it anywhere.
* Penalising calibration for raw leniency would punish an accurate but
  generous judge twice: agreement with decidable outcomes (the
  calibration measured here) already captures whether the scores were
  *right*, lenient or not.

The bias quantity itself is still computed and returned per family
(``bias``) so the owner can see it, and a coherent penalty could be
built later from either a real style-similarity feature or a
difference-in-differences against non-contest judging (the old judged
gate), which the matrix design on its own never produces. Until then:
agreement-only calibration, penalty named as a gap, never faked.
"""

from __future__ import annotations

import json
import random
import re
import statistics
import string
from dataclasses import dataclass, field

from .judges import _measured_summary_for_labels, blind_text

VALIDITY_ROWS = (
    "question_fidelity",
    "completeness",
    "evidence",
    "test_agreement",
)

VALIDITY_RUBRIC = {
    "id": "validity_matrix_v1",
    "version": 1,
    "scale": 10,
    "items": list(VALIDITY_ROWS),
}

#: Gates whose score rows count as cells for calibration purposes.
_CELL_GATES = ("matrix", "judged")

#: Calibration weighting activates at this many scored (decidable) cells.
CALIBRATION_MIN_CELLS = 5

#: Families start here; also the weight used until activation.
CALIBRATION_PRIOR = 0.5

#: Mean confidence below this escalates a candidate.
LOW_CONFIDENCE_THRESHOLD = 0.4


def validity_rubric() -> dict:
    """Return a defensive copy of the validity-matrix rubric dict.

    Defined locally (not in rubrics.py): the loop passes this dict
    through to judges, and judges.get_rubric stays untouched.
    """
    result = dict(VALIDITY_RUBRIC)
    result["items"] = list(VALIDITY_RUBRIC["items"])
    return result


# ---------------------------------------------------------------- helpers

def _candidate_id(candidate):
    if not isinstance(candidate, dict):
        return None
    cid = candidate.get("id")
    if cid is None:
        cid = candidate.get("candidate_id")
    return cid


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fmt(value: float) -> str:
    return ("%.3f" % value).rstrip("0").rstrip(".")


def _labels(count: int) -> list:
    pool = list(string.ascii_uppercase)
    if count <= len(pool):
        return pool[:count]
    labels = list(pool)
    for first in string.ascii_uppercase:
        for second in string.ascii_uppercase:
            labels.append(first + second)
            if len(labels) >= count:
                return labels[:count]
    return labels[:count]


def weighted_median(values_weights, weights=None):
    """Lower weighted median of (value, weight) pairs.

    Sort by value; return the first value whose cumulative weight
    reaches at least half of the total weight. Also accepts a
    ``{value: weight}`` dict, or parallel ``(values, weights)`` lists
    via the second argument. Returns None when there are no pairs or
    the total weight is not positive.
    """
    if weights is not None:
        pairs = list(zip(values_weights, weights))
    elif isinstance(values_weights, dict):
        pairs = list(values_weights.items())
    else:
        pairs = list(values_weights or [])
    cleaned = []
    for value, weight in pairs:
        if not _is_number(value) or not _is_number(weight):
            continue
        if weight <= 0:
            continue
        cleaned.append((float(value), float(weight)))
    if not cleaned:
        return None
    cleaned.sort(key=lambda pair: pair[0])
    total = sum(w for _, w in cleaned)
    if total <= 0:
        return None
    half = total / 2.0
    cumulative = 0.0
    for value, weight in cleaned:
        cumulative += weight
        if cumulative >= half:
            return float(value)
    return float(cleaned[-1][0])


def _weighted_or_plain(pairs):
    """Weighted median, falling back to the plain median if all
    weights are zero (possible only with degenerate calibrations)."""
    result = weighted_median(pairs)
    if result is not None:
        return result
    values = [float(v) for v, _ in pairs if _is_number(v)]
    if not values:
        return 0.0
    return float(statistics.median(values))


# ---------------------------------------------------------------- cells

def _family_of_entry(entry):
    """Extract a family name from a panel entry (str / dict / object)."""
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("family") or entry.get("judge_family")
    return getattr(entry, "family", None)


def build_matrix_cells(panel_families_in_order, candidates) -> list:
    """The dispatch queue, in code: peer x candidate minus self-cells.

    ``panel_families_in_order`` is the panel's family list in panel
    order (entries may be bare family names, dicts with a "family"
    key, or objects with a .family attribute). Returns one cell dict
    per (judge family, candidate) pair where the judge family is NOT
    the candidate's own family, in panel order then candidate order.
    For N candidates of distinct families and the full panel, that is
    N x (peers - 1) cells.
    """
    cells = []
    seen_families = []
    for entry in panel_families_in_order or []:
        family = _family_of_entry(entry)
        if family is not None and family not in seen_families:
            seen_families.append(family)
    for family in seen_families:
        for candidate in candidates or []:
            if not isinstance(candidate, dict):
                continue
            candidate_family = candidate.get("family")
            if candidate_family == family:
                continue  # self-cell: stripped here, never dispatched
            cells.append({
                "judge_family_slot": family,
                "judge_family": family,
                "candidate_id": _candidate_id(candidate),
                "candidate_family": candidate_family,
            })
    return cells


# ---------------------------------------------------------------- packet

def _acceptance_summary(contract) -> str:
    """Compact listing of the contract's acceptance items."""
    if isinstance(contract, (list, tuple)):
        items = list(contract)
    elif isinstance(contract, dict):
        items = contract.get("acceptance") or contract.get("items") or []
    else:
        items = []
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        cls = item.get("class")
        if cls == "hard":
            expect = item.get("expect") or {}
            lines.append(
                f"- hard: {item.get('cmd')} (expect exit {expect.get('exit', 0)})"
            )
        elif cls == "measured":
            metrics = ", ".join(
                f"{m.get('name')} {m.get('op')} {m.get('value')}"
                for m in item.get("metrics") or [] if isinstance(m, dict)
            )
            sensor = item.get("sensor") or item.get("sensor_cmd") or ""
            lines.append(f"- measured: {sensor}: {metrics}")
        elif cls == "judged":
            lines.append(
                f"- judged: rubric {item.get('rubric')}, min {item.get('min')}"
            )
        else:
            lines.append(f"- {cls}: {item}")
    return "\n".join(lines)


def build_matrix_packet(labelled_diffs: dict, contract, measured_summary: str,
                        rubric: dict) -> str:
    """Assemble the blinded packet one peer scores.

    Header (rubric id / items / scale), then the ORIGINAL contract
    task and a compact acceptance listing (question_fidelity is scored
    against this text, never a candidate's restatement), then the
    labelled hard/measured summary, then one blinded section per
    label. Diffs arrive pre-blinded by the caller; everything is run
    through blind_text again for path scrubbing, like
    judges.build_packet.
    """
    task = ""
    if isinstance(contract, dict):
        task = contract.get("task") or ""
    lines = [
        f"RUBRIC: {rubric.get('id', 'validity_matrix_v1')} "
        f"(version {rubric.get('version', '?')})",
        f"SCALE: 0-{rubric.get('scale', 10)} per item",
        f"ITEMS: {', '.join(rubric.get('items', []))}",
        "",
        "CONTRACT TASK (original text -- score question_fidelity against this):",
        blind_text(task, []),
        "",
        "CONTRACT ACCEPTANCE:",
        blind_text(_acceptance_summary(contract), []),
        "",
        "MEASURED / TEST SUMMARY:",
        blind_text(measured_summary or "", []),
        "",
    ]
    for label, diff in (labelled_diffs or {}).items():
        lines.append(f"=== CANDIDATE {label} ===")
        lines.append(blind_text(diff or "", []))
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- validation

def _validate_matrix_cell(entry, rubric: dict):
    """Validate one peer's score of one candidate.

    Returns (scores, confidences, details) dicts keyed by rubric row,
    or None when the cell is invalid. Every row must be present with a
    numeric score in [0, scale], a numeric confidence in [0, 1], and
    non-empty evidence AND rationale strings -- otherwise the whole
    cell is discarded ("no rationale/evidence, no score").
    """
    if not isinstance(entry, dict):
        return None
    items_raw = entry.get("items")
    if not isinstance(items_raw, dict):
        return None
    scale = rubric.get("scale", 10)
    scores, confidences, details = {}, {}, {}
    for row in rubric["items"]:
        raw = items_raw.get(row)
        if not isinstance(raw, dict):
            return None
        score = raw.get("score", raw.get("value"))
        confidence = raw.get("confidence")
        evidence = raw.get("evidence")
        rationale = raw.get("rationale")
        if not _is_number(score) or not (0 <= float(score) <= float(scale)):
            return None
        if not _is_number(confidence) or not (0.0 <= float(confidence) <= 1.0):
            return None
        if not (isinstance(evidence, str) and evidence.strip()):
            return None
        if not (isinstance(rationale, str) and rationale.strip()):
            return None
        scores[row] = float(score)
        confidences[row] = float(confidence)
        details[row] = {
            "score": float(score),
            "confidence": float(confidence),
            "evidence": evidence,
            "rationale": rationale,
        }
    return scores, confidences, details


# ---------------------------------------------------------------- dissent

def _metric_names_for_candidate(candidate_id, gate_results) -> list:
    result = (gate_results or {}).get(candidate_id)
    names = []
    if result is None:
        return names
    measured = result.get("measured") if isinstance(result, dict) else getattr(result, "measured", None)
    for measure in measured or []:
        metrics = measure.get("metrics") if isinstance(measure, dict) else getattr(measure, "metrics", None)
        for row in metrics or []:
            name = row.get("metric") if isinstance(row, dict) else getattr(row, "metric", None)
            if name and str(name) not in names:
                names.append(str(name))
    return names


def _hard_cmds(contract, candidate_id, gate_results) -> list:
    cmds = []
    if isinstance(contract, dict):
        for item in contract.get("acceptance") or []:
            if isinstance(item, dict) and item.get("class") == "hard" and item.get("cmd"):
                if str(item["cmd"]) not in cmds:
                    cmds.append(str(item["cmd"]))
    result = (gate_results or {}).get(candidate_id)
    if result is not None:
        hard = result.get("hard") if isinstance(result, dict) else getattr(result, "hard", None)
        for entry in hard or []:
            cmd = entry.get("cmd") if isinstance(entry, dict) else getattr(entry, "cmd", None)
            if cmd and str(cmd) not in cmds:
                cmds.append(str(cmd))
    return cmds


_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9_./-]+")
_TOKEN_STOP = {"python", "python3", "python.exe", "-m", "--", "-", "run", "test",
               "tests", "unittest", "pytest", "discover", "dotnet", "build", "node", "npm"}


def _hard_tokens(cmds) -> list:
    """The distinctive tokens of the hard commands: path-like ones only
    (containing ``/`` or ``.``, or a ``test_`` name) — never a plain word
    such as ``tests`` or ``discover`` (fix pass Wave 2.10, review 1B-17),
    because a citation triggers a paid gate re-run."""
    tokens = []
    for cmd in cmds:
        for token in _TOKEN_SPLIT.split(cmd or ""):
            token = token.strip().strip(".")
            low = token.lower()
            if len(token) < 4 or low in _TOKEN_STOP:
                continue
            path_like = "/" in low or "." in low or low.startswith("test_")
            if not path_like:
                continue
            if low not in tokens:
                tokens.append(low)
    return tokens


def _cites_gate(text: str, metric_names, hard_cmds):
    """Detect a gate citation in a dissent's evidence + rationale.

    "measured" when a measurement metric name for this candidate
    appears; "hard" when a full hard cmd string, or a path-like token of
    one (a test file, a module path, a project file), appears. Plain
    words of the command never cite. Case-insensitive substring matching
    throughout.
    """
    low = (text or "").lower()
    if not low.strip():
        return None
    for name in metric_names:
        if name and name.lower() in low:
            return "measured"
    for cmd in hard_cmds:
        if cmd and cmd.lower() in low:
            return "hard"
    for token in _hard_tokens(hard_cmds):
        if token in low:
            return "hard"
    return None


# ---------------------------------------------------------------- calibration input

def _effective_calibration(family, calibrations):
    """Effective weight for a family, honouring the activation rule.

    Families start at the 0.5 prior; a supplied calibration only
    weights once the family has >= CALIBRATION_MIN_CELLS scored cells
    (when the entry carries an ``n`` / ``active`` marker, as
    compute_calibration's output does). A bare numeric entry carries
    no count, so it is taken as already active.
    Returns (weight, n_or_None, active).
    """
    if not calibrations or family not in (calibrations or {}):
        return CALIBRATION_PRIOR, None, False
    entry = calibrations[family]
    if isinstance(entry, dict):
        n = entry.get("n")
        value = entry.get("calibration", entry.get("value", CALIBRATION_PRIOR))
        if not _is_number(value):
            return CALIBRATION_PRIOR, n, False
        if _is_number(n) and int(n) < CALIBRATION_MIN_CELLS:
            return CALIBRATION_PRIOR, int(n), False
        if entry.get("active") is False:
            return CALIBRATION_PRIOR, n, False
        return min(max(float(value), 0.0), 1.0), n, True
    if _is_number(entry):
        return min(max(float(entry), 0.0), 1.0), None, True
    return CALIBRATION_PRIOR, None, False


# ---------------------------------------------------------------- run

@dataclass
class MatrixOutcome:
    per_candidate: dict = field(default_factory=dict)
    dissent: list = field(default_factory=list)
    rerun_requests: dict = field(default_factory=dict)
    panel_status: str = "full"
    escalated: bool = False
    escalation_reason: str | None = None
    peers_used: list = field(default_factory=list)
    effective_calibrations: dict = field(default_factory=dict)


def run_matrix(store, contract, candidates, gate_results, diffs,
               peer_executors, *, min_score, dissent_threshold=3.0,
               spread_threshold=2.0, calibrations=None, seed=0,
               round_no=None) -> MatrixOutcome:
    """Run the peer validity matrix over hard/measured survivors.

    ``peer_executors`` maps expert_id -> executor (``.family`` and
    ``.judge(packet, rubric)``). A peer is only ever shown candidates
    from other families (self-cells stripped by build_matrix_cells'
    rule, applied here per peer). Valid cells are persisted via
    ``store.add_score(..., "matrix", "validity_matrix_v1", ...)``
    with confidence / details kwargs.

    ``round_no`` tags each persisted cell with its bracket round (fix
    pass round 4, spec G9, round-3 contest-core F6): the loop re-runs
    the matrix for the SAME round after an anchored dissent, and the
    re-run's rows supersede the first pass's rows for that round in
    calibration instead of double-counting them. Callers who do not
    name a round leave rows untagged, and calibration counts untagged
    rows individually, exactly as before.
    """
    rubric = validity_rubric()
    candidates = [c for c in (candidates or []) if isinstance(c, dict)]
    peer_executors = peer_executors or {}

    panel_families = []
    for executor in peer_executors.values():
        family = getattr(executor, "family", None)
        if family is not None and family not in panel_families:
            panel_families.append(family)
    # The queue artifact: exactly the cells that may be dispatched.
    planned = build_matrix_cells(panel_families, candidates)
    eligible_by_family: dict = {}
    for cell in planned:
        eligible_by_family.setdefault(cell["judge_family"], []).append(
            cell["candidate_id"])

    effective: dict = {}
    for family in panel_families:
        weight, n, active = _effective_calibration(family, calibrations)
        effective[family] = {"calibration": weight, "n": n, "active": active}

    # Strip terms: no expert id or family name reaches any peer,
    # including the peers' own identities (same rule as judges.py).
    strip_terms: list = []
    for candidate in candidates:
        for key in ("expert_id", "expert", "author_id", "author", "family"):
            value = candidate.get(key)
            if value and str(value) not in strip_terms:
                strip_terms.append(str(value))
    for judge_id, executor in peer_executors.items():
        if str(judge_id) not in strip_terms:
            strip_terms.append(str(judge_id))
        family = getattr(executor, "family", None)
        if family and str(family) not in strip_terms:
            strip_terms.append(str(family))

    # cid -> list of valid cell dicts
    collected: dict = {_candidate_id(c): [] for c in candidates}
    peers_used: list = []

    for judge_id, executor in peer_executors.items():
        judge_family = getattr(executor, "family", None)
        if judge_family is None:
            continue  # cannot verify the family -> cannot strip self-cells
        eligible_ids = set(eligible_by_family.get(judge_family, []))
        order = [c for c in candidates if _candidate_id(c) in eligible_ids]
        rng = random.Random(hash((seed, judge_id)) & 0xFFFFFFFF)
        rng.shuffle(order)
        labels = _labels(len(order))
        labelled_diffs = {}
        for label, candidate in zip(labels, order):
            cid = _candidate_id(candidate)
            labelled_diffs[label] = blind_text((diffs or {}).get(cid, ""), strip_terms)
        summary = _measured_summary_for_labels(order, labels, gate_results)
        packet = build_matrix_packet(labelled_diffs, contract, summary, rubric)
        try:
            raw = executor.judge(packet, rubric)
        except Exception:  # noqa: BLE001 - a failing peer yields no cells
            raw = None
        if not isinstance(raw, dict):
            continue
        peer_scored_any = False
        for position, (label, candidate) in enumerate(zip(labels, order), start=1):
            validated = _validate_matrix_cell(raw.get(label), rubric)
            if validated is None:
                continue  # no rationale/evidence, no score -- discarded
            scores, confidences, details = validated
            cid = _candidate_id(candidate)
            mean = sum(scores.values()) / len(scores) if scores else 0.0
            mean_confidence = (
                sum(confidences.values()) / len(confidences) if confidences else 0.0
            )
            # The round tag rides inside each row's detail dict (the
            # cell's metadata bag): items/rubric/confidence keep their
            # pinned shapes, and calibration reads the tag back to let
            # a same-round re-run supersede this pass (round-3 F6).
            persisted_details = details
            if round_no is not None:
                persisted_details = {
                    row: {**detail, "round": round_no}
                    for row, detail in details.items()
                }
            store.add_score(
                cid, "matrix", "validity_matrix_v1", judge_id, judge_family,
                True, position, items=scores, mean=mean,
                confidence=mean_confidence, details=persisted_details,
            )
            collected[cid].append({
                "judge_id": judge_id,
                "judge_family": judge_family,
                "scores": scores,
                "confidences": confidences,
                "details": details,
                "mean": mean,
                "mean_confidence": mean_confidence,
            })
            peer_scored_any = True
        if peer_scored_any:
            peers_used.append(judge_id)

    # ---- aggregation -------------------------------------------------
    per_candidate: dict = {}
    for candidate in candidates:
        cid = _candidate_id(candidate)
        cells = collected.get(cid, [])
        peer_families = []
        for cell in cells:
            if cell["judge_family"] not in peer_families:
                peer_families.append(cell["judge_family"])
        candidate_status = (
            "degraded" if (len(peer_families) < 2 or len(cells) < 2) else "full"
        )
        if not cells:
            # Nobody produced a valid cell for this candidate: it is
            # UNSCORED — no median, no rows, no confidence, no spread —
            # not a candidate that scored zero. It cannot pass, and the
            # loop records no validity for it (Phase 3 live-run finding,
            # 2026-11-07: the two-seat matrix wrote 0.0 everywhere and the
            # ledger credited 0.0 points nobody measured).
            per_candidate[cid] = {
                "rows": {row: None for row in VALIDITY_ROWS},
                "median_mean": None,
                "min_row": None,
                "spread": None,
                "pass": False,
                "scored": False,
                "mean_confidence": None,
                "panel_status": candidate_status,
                "peers": peer_families,
            }
            continue
        rows: dict = {}
        for row in VALIDITY_ROWS:
            pairs = [
                (cell["scores"][row],
                 effective.get(cell["judge_family"], {}).get(
                     "calibration", CALIBRATION_PRIOR)
                 * cell["confidences"][row])
                for cell in cells
            ]
            rows[row] = _weighted_or_plain(pairs)
        peer_mean_pairs = [
            (cell["mean"],
             effective.get(cell["judge_family"], {}).get(
                 "calibration", CALIBRATION_PRIOR)
             * cell["mean_confidence"])
            for cell in cells
        ]
        median_mean = _weighted_or_plain(peer_mean_pairs)
        peer_means = [cell["mean"] for cell in cells]
        spread = (max(peer_means) - min(peer_means)) if len(peer_means) >= 2 else 0.0
        all_confidences = [
            conf for cell in cells for conf in cell["confidences"].values()
        ]
        mean_confidence = sum(all_confidences) / len(all_confidences)
        min_row = min(rows.values())
        passed = median_mean >= min_score and min_row >= min_score - 2
        per_candidate[cid] = {
            "rows": rows,
            "median_mean": float(median_mean),
            "min_row": float(min_row),
            "spread": float(spread),
            "pass": bool(passed),
            "scored": True,
            "mean_confidence": float(mean_confidence),
            "panel_status": candidate_status,
            "peers": peer_families,
        }

    # ---- dissent ------------------------------------------------------
    dissent: list = []
    rerun_requests: dict = {}
    for candidate in candidates:
        cid = _candidate_id(candidate)
        cells = collected.get(cid, [])
        if not cells:
            continue
        metric_names = _metric_names_for_candidate(cid, gate_results)
        hard_cmds = _hard_cmds(contract, cid, gate_results)
        for row in VALIDITY_ROWS:
            row_median = per_candidate[cid]["rows"][row]
            for cell in cells:
                score = cell["scores"][row]
                if abs(score - row_median) <= dissent_threshold:
                    continue
                detail = cell["details"][row]
                cites = _cites_gate(
                    f"{detail.get('evidence', '')} {detail.get('rationale', '')}",
                    metric_names, hard_cmds,
                )
                dissent.append({
                    "candidate_id": cid,
                    "row": row,
                    "judge_id": cell["judge_id"],
                    "judge_family": cell["judge_family"],
                    "score": float(score),
                    "row_median": float(row_median),
                    "evidence": detail.get("evidence"),
                    "rationale": detail.get("rationale"),
                    "cites_gate": cites,
                })
                if cites is not None and cid not in rerun_requests:
                    rerun_requests[cid] = cites
    # Second pass: a weighted-median row can sit ON an outlying cell
    # (with two peers the lower weighted median is the lower score),
    # which leaves the majority-side cell beyond the threshold while
    # the citing outlier itself measures distance 0. When a candidate
    # already carries a dissent on a row, also check the remaining
    # cells on that row for a gate citation — the citation, not the
    # distance, is what the re-run rule keys on. The pass is PER
    # CANDIDATE (fix pass round 4, spec G9, round-3 contest-core F4):
    # it runs for every dissented candidate that has no re-run request
    # of its own — one candidate's first-pass citation no longer
    # suppresses the citation check for all the others.
    dissented = {(d["candidate_id"], d["row"]) for d in dissent}
    for candidate in candidates:
        cid = _candidate_id(candidate)
        if cid in rerun_requests:
            continue  # this candidate already has its re-run
        if not any(dcid == cid for dcid, _row in dissented):
            continue  # no dissent on this candidate: nothing to recheck
        cells = collected.get(cid, [])
        if not cells:
            continue
        metric_names = _metric_names_for_candidate(cid, gate_results)
        hard_cmds = _hard_cmds(contract, cid, gate_results)
        for row in VALIDITY_ROWS:
            if (cid, row) not in dissented:
                continue
            for cell in cells:
                detail = cell["details"][row]
                cites = _cites_gate(
                    f"{detail.get('evidence', '')} {detail.get('rationale', '')}",
                    metric_names, hard_cmds,
                )
                if cites is not None:
                    rerun_requests.setdefault(cid, cites)
                    break

    # ---- escalation ----------------------------------------------------
    reasons: list = []
    for candidate in candidates:
        cid = _candidate_id(candidate)
        entry = per_candidate[cid]
        cells = collected.get(cid, [])
        if not cells:
            continue  # unscored: nothing to compare (see the aggregation above)
        if entry["mean_confidence"] < LOW_CONFIDENCE_THRESHOLD:
            reasons.append(
                f"candidate {cid} mean confidence {_fmt(entry['mean_confidence'])} "
                f"below {LOW_CONFIDENCE_THRESHOLD}"
            )
        if entry["spread"] > spread_threshold:
            reasons.append(
                f"candidate {cid} peer spread {_fmt(entry['spread'])} "
                f"above threshold {_fmt(float(spread_threshold))}"
            )

    panel_status = (
        "degraded"
        if any(e["panel_status"] == "degraded" for e in per_candidate.values())
        else "full"
    )

    return MatrixOutcome(
        per_candidate=per_candidate,
        dissent=dissent,
        rerun_requests=rerun_requests,
        panel_status=panel_status,
        escalated=bool(reasons),
        escalation_reason="; ".join(reasons) if reasons else None,
        peers_used=peers_used,
        effective_calibrations=effective,
    )


# ---------------------------------------------------------------- calibration

def _expert_entries(experts, store):
    """Normalise the experts argument to [(expert_id, family), ...]."""
    entries: list = []
    seen = set()

    def _add(expert_id, family):
        key = expert_id
        if key in seen:
            return
        seen.add(key)
        entries.append((expert_id, family))

    def _family_from(value, expert_id):
        family = _family_of_entry(value)
        if family is None and expert_id is not None and store is not None:
            getter = getattr(store, "get_expert", None)
            if callable(getter):
                try:
                    record = getter(expert_id)
                except Exception:  # noqa: BLE001 - unknown expert: skip lookup
                    record = None
                family = _family_of_entry(record)
        return family

    if experts is None:
        lister = getattr(store, "list_experts", None) if store is not None else None
        experts = lister() if callable(lister) else []
    if isinstance(experts, dict):
        for expert_id, value in experts.items():
            _add(expert_id, _family_from(value, expert_id))
    elif isinstance(experts, (list, tuple, set)):
        for item in experts:
            if isinstance(item, dict):
                expert_id = item.get("id") or item.get("expert_id")
                _add(expert_id, _family_from(item, expert_id))
            elif isinstance(item, str):
                _add(item, _family_from(None, item))
            else:
                expert_id = getattr(item, "id", None) or getattr(item, "expert_id", None)
                _add(expert_id, _family_from(item, expert_id))
    return entries


def _rows_from_attr(store, name):
    value = getattr(store, name, None)
    if value is None or callable(value):
        return []
    if isinstance(value, dict):
        rows = []
        for item in value.values():
            if isinstance(item, (list, tuple)):
                rows.extend(r for r in item if isinstance(r, dict))
            elif isinstance(item, dict):
                rows.append(item)
        return rows
    if isinstance(value, (list, tuple)):
        return [r for r in value if isinstance(r, dict)]
    return []


def _call_rows(store, names):
    for name in names:
        fn = getattr(store, name, None)
        if callable(fn):
            try:
                result = fn()
            except Exception:  # noqa: BLE001 - enumeration is best-effort
                continue
            if isinstance(result, (list, tuple)):
                return [r for r in result if isinstance(r, dict)]
    return []


def _sqlite_ids(store, sql):
    conn = getattr(store, "_conn", None)
    if conn is None:
        return []
    try:
        rows = conn.execute(sql).fetchall()
    except Exception:  # noqa: BLE001 - not a sqlite store: no fallback
        return []
    ids = []
    for row in rows:
        try:
            value = row[0]
        except Exception:  # noqa: BLE001
            continue
        if value is not None:
            ids.append(value)
    return ids


def _enumerate_candidates(store):
    """Best-effort enumeration of every candidate the store knows.

    Uses, in order: bulk candidate accessors, a ``candidates``
    attribute (the FakeStore pattern), contracts + candidates_for,
    score-row candidate ids + get_candidate, and -- for the real
    SQLite Store, whose public API has no bulk candidate listing -- a
    read-only id query over its connection. Returns {cid: dict}.
    """
    found: dict = {}

    def _add(candidate):
        if not isinstance(candidate, dict):
            return
        cid = _candidate_id(candidate)
        if cid is not None and cid not in found:
            found[cid] = candidate

    for candidate in _call_rows(store, ("all_candidates", "list_candidates",
                                        "candidates_all")):
        _add(candidate)
    for candidate in _rows_from_attr(store, "candidates"):
        _add(candidate)
    for candidate in _rows_from_attr(store, "_candidates"):
        _add(candidate)

    # Score rows (attribute pattern) reveal candidate ids.
    score_rows = _rows_from_attr(store, "scores") + _rows_from_attr(store, "_scores")
    score_rows += _call_rows(store, ("all_scores", "list_scores", "scores_all"))
    score_ids = []
    for row in score_rows:
        cid = row.get("candidate_id")
        if cid is not None and cid not in score_ids:
            score_ids.append(cid)

    if not found and not score_ids:
        for cid in _sqlite_ids(store, "SELECT id FROM candidates"):
            if cid not in score_ids:
                score_ids.append(cid)
        for cid in _sqlite_ids(
                store, "SELECT DISTINCT candidate_id FROM scores"):
            if cid not in score_ids:
                score_ids.append(cid)

    # Contracts -> candidates_for.
    contract_ids = []
    for contract in (_call_rows(store, ("all_contracts", "list_contracts"))
                     + _rows_from_attr(store, "contracts")
                     + _rows_from_attr(store, "_contracts")):
        cid_ref = contract.get("id") or contract.get("contract_id")
        if cid_ref is not None and cid_ref not in contract_ids:
            contract_ids.append(cid_ref)
    raw_ids = getattr(store, "contract_ids", None)
    if isinstance(raw_ids, (list, tuple)):
        for cid_ref in raw_ids:
            if cid_ref is not None and cid_ref not in contract_ids:
                contract_ids.append(cid_ref)
    candidates_for = getattr(store, "candidates_for", None)
    if callable(candidates_for):
        for contract_id in contract_ids:
            try:
                for candidate in candidates_for(contract_id) or []:
                    _add(candidate)
            except Exception:  # noqa: BLE001
                continue

    getter = getattr(store, "get_candidate", None)
    for cid in score_ids:
        if cid in found:
            continue
        if callable(getter):
            try:
                _add(getter(cid))
                continue
            except Exception:  # noqa: BLE001
                pass
        found.setdefault(cid, {"id": cid})
    return found


def _verdicts_by_contract(store, candidates_by_id):
    contract_ids = []
    for candidate in candidates_by_id.values():
        ref = candidate.get("contract_id")
        if ref is not None and ref not in contract_ids:
            contract_ids.append(ref)
    verdicts: dict = {}
    latest = getattr(store, "latest_verdict", None)
    if callable(latest):
        for contract_id in contract_ids:
            try:
                verdict = latest(contract_id)
            except Exception:  # noqa: BLE001
                verdict = None
            if isinstance(verdict, dict):
                verdicts[contract_id] = verdict
    if not verdicts:
        rows = (_rows_from_attr(store, "verdicts")
                + _rows_from_attr(store, "_verdicts")
                + _call_rows(store, ("all_verdicts", "list_verdicts")))
        for row in rows:  # later rows are newer: they overwrite
            ref = row.get("contract_id")
            if ref is not None:
                verdicts[ref] = row
    return verdicts


def _judged_negatives(judged):
    negatives = set()
    if not isinstance(judged, dict):
        return negatives
    maps = [judged]
    for key in ("per_candidate", "perCandidate", "candidates"):
        sub = judged.get(key)
        if isinstance(sub, dict):
            maps.append(sub)
    for mapping in maps:
        for cid, entry in mapping.items():
            if isinstance(entry, dict) and entry.get("pass") is False:
                negatives.add(cid)
            elif entry is False:
                negatives.add(cid)
    return negatives


#: Elimination kinds that ARE decidable negatives (round-5 fix spec
#: H5, round-5 gate-core F5): the gates or the judging killed the
#: candidate on evidence a validity score can be graded against.
#: "bracket_cut" is deliberately absent — a cut passer decided
#: nothing (see _decidable_outcomes).
_ELIMINATED_KIND_NEGATIVES = ("hard", "measured", "gate_error", "judged")


def _candidate_eliminated_kind(candidate):
    """The candidate's elimination kind, resolved canonically
    (round-6 fix spec J2, round-6 gate-core F2).

    The loop's write site (``loop._mark_eliminated``) persists the
    kind INSIDE the candidate's evidence, and the real Store's
    candidate dict carries ``evidence`` as a nested dict — there is
    no top-level ``eliminated_kind`` on a persisted row. Round 5's
    read site looked only at the top level, so on the production
    wiring the H5 branch never fired: a gate_error candidate was
    invisible to calibration instead of grading as a negative.

    Canonical resolution: a top-level ``eliminated_kind``, when
    present, wins (hand-built rows — the round-5 unit-test shape —
    keep working); otherwise the kind is read from the candidate's
    ``evidence``, which may be a dict (the real Store's shape) or a
    JSON string (duck-typed rows that carry the raw column).
    Returns None when neither place names a kind.

    The resolved kind is canonicalized — stripped and lowercased —
    in BOTH positions (round-7 fix spec K9; round-7 attack F6):
    matching used to be exact, so a ``Gate_Error`` or
    ``" gate_error "`` variant matched nothing and the candidate's
    negative silently vanished from calibration.  A value that
    canonicalizes to the empty string names no kind at all, so
    resolution falls through to the next position (an empty
    top-level value no longer shadows a real evidence kind); a
    non-string value passes through untouched, as before.
    """
    def canonical(value):
        if isinstance(value, str):
            return value.strip().lower() or None
        return value

    kind = canonical(candidate.get("eliminated_kind"))
    if kind is not None:
        return kind
    evidence = candidate.get("evidence")
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except ValueError:
            evidence = None
    if isinstance(evidence, dict):
        return canonical(evidence.get("eliminated_kind"))
    return None


def _decidable_outcomes(candidates_by_id, verdicts_by_contract):
    """{candidate_id: True (positive) / False (negative)} for candidates
    with a KNOWN decidable outcome; unlisted candidates are unknown.

    Decidability is absolute, not relative (fix pass round 4, spec G9,
    round-3 contest-core F2): the winner of a decided verdict is
    positive; a candidate the verdict's judged map marks as failed, or
    whose own row carries a gate elimination (hard / measured /
    provenance), is negative. Two classes are EXCLUDED — they decided
    nothing a validity score can be graded against: candidates that
    PASSED the judging but lost the bracket cut to a stronger passer
    (the verdict's eliminated list names them alongside true failures,
    so the list itself is never read as ground truth), and ALL
    candidates of an escalated verdict (that contest produced no
    winner; its judged map grades nobody).

    Round-5 fix spec H5 (round-5 gate-core F5): the loop persists
    the elimination kind (hard | measured | gate_error | judged |
    bracket_cut) and a row carrying it is governed by it: the four
    evidence kinds are negatives — ``gate_error`` included, so a
    candidate the gates killed on re-check after scoring is graded,
    not silently dropped — bracket_cut is excluded, and candidates
    of an escalated verdict stay excluded whole. Rows WITHOUT the
    kind keep the pre-fix ``eliminated_at`` behavior exactly.

    Round-6 fix spec J2 (round-6 gate-core F2): the kind is resolved
    by :func:`_candidate_eliminated_kind` — the loop writes it into
    the candidate's ``evidence`` (the shape the real Store returns),
    NOT at the top level round 5's read site expected, so the H5
    branch was dead on the production wiring until the read side
    learned the write side's shape.
    """
    outcomes: dict = {}
    for contract_id, verdict in verdicts_by_contract.items():
        if not isinstance(verdict, dict):
            continue
        if verdict.get("escalated"):
            continue  # decided nothing: no positives, no negatives
        winner = verdict.get("winner")
        if winner is not None:
            outcomes[winner] = True
        for cid in _judged_negatives(verdict.get("judged")):
            if cid != winner:
                outcomes[cid] = False
    for cid, candidate in candidates_by_id.items():
        kind = _candidate_eliminated_kind(candidate)
        if kind is not None:
            if kind in _ELIMINATED_KIND_NEGATIVES:
                verdict = verdicts_by_contract.get(
                    candidate.get("contract_id"))
                if isinstance(verdict, dict) and verdict.get("escalated"):
                    continue  # escalated verdicts stay excluded whole
                if outcomes.get(cid) is not True:
                    outcomes[cid] = False
            # bracket_cut (or an unknown kind): excluded, not decidable.
            continue
        eliminated_at = candidate.get("eliminated_at")
        if eliminated_at in ("hard", "measured", "provenance"):
            if outcomes.get(cid) is not True:
                outcomes[cid] = False
    return outcomes


def _score_rows_for(store, candidate_id, enumerated_rows):
    scorer = getattr(store, "scores_for", None)
    if callable(scorer):
        try:
            rows = scorer(candidate_id)
        except Exception:  # noqa: BLE001
            rows = None
        if isinstance(rows, (list, tuple)):
            return [r for r in rows if isinstance(r, dict)]
    return [r for r in enumerated_rows if r.get("candidate_id") == candidate_id]


def _cell_mean(row):
    mean = row.get("mean")
    if _is_number(mean):
        return float(mean)
    items = row.get("items")
    if isinstance(items, dict) and items:
        values = []
        for raw in items.values():
            if _is_number(raw):
                values.append(float(raw))
            elif isinstance(raw, dict) and _is_number(raw.get("score")):
                values.append(float(raw["score"]))
        if values:
            return sum(values) / len(values)
    return None


def _row_round(row):
    """The bracket round a score row was persisted under, or None.

    run_matrix tags each cell's per-row detail dicts with the round
    (round-3 contest-core F6); untagged rows — legacy rows, judged-gate
    rows, rows from callers that name no round — return None.  An
    untagged row is its (judge, gate) pair's ROUND 0 (round-5 fix spec
    H5, round-5 gate-core F6): a tagged row for the same pair
    supersedes the pair's untagged rows in compute_calibration, while
    a pair with only untagged rows keeps counting them individually.
    """
    details = row.get("details")
    if isinstance(details, dict):
        for detail in details.values():
            if isinstance(detail, dict) and detail.get("round") is not None:
                return detail.get("round")
    return None


def compute_calibration(store, experts) -> dict:
    """Per-family judge calibration from the store's score history.

    For each scored candidate with a known decidable outcome (verdict
    winner of a non-escalated verdict = positive; judged-losing or
    eliminated with kind hard/measured/gate_error/judged = negative;
    bracket-cut passers and every candidate of an escalated verdict
    excluded — see _decidable_outcomes), a cell in gate "matrix"/"judged"
    AGREES if (score >= 5) == positive. A judge–candidate pair's rows
    are counted once per bracket round, at their latest values (see
    the supersede pass below); untagged rows are the pair's round 0
    and are superseded by any tagged row for the pair (round-5 H5).
    calibration = (agree + 2*0.5) / (n + 2) -- Laplace smoothing toward
    the 0.5 prior. ``active`` is True once n >= 5; before that the
    effective weight elsewhere is the prior (see _effective_calibration).

    ``bias`` is the style-preference proxy quantity, reported but NOT
    applied as a penalty -- see the module docstring's named gap
    (GAP-STYLE-PREFERENCE-PENALTY).

    When the store offers update_expert_calibration it is called back
    per expert with (expert_id, calibration, n).
    """
    expert_entries = _expert_entries(experts, store)
    family_by_expert = {eid: fam for eid, fam in expert_entries if fam is not None}

    candidates_by_id = _enumerate_candidates(store)
    verdicts = _verdicts_by_contract(store, candidates_by_id)
    outcomes = _decidable_outcomes(candidates_by_id, verdicts)

    enumerated_scores = (
        _rows_from_attr(store, "scores") + _rows_from_attr(store, "_scores")
        + _call_rows(store, ("all_scores", "list_scores", "scores_all"))
    )

    # Gather candidate ids to read scores for: enumerated candidates
    # plus any candidate id appearing in enumerable score rows.
    candidate_ids = list(candidates_by_id)
    for row in enumerated_scores:
        cid = row.get("candidate_id")
        if cid is not None and cid not in candidate_ids:
            candidate_ids.append(cid)

    agree: dict = {}
    total: dict = {}
    # (candidate_id -> family -> [cell means]) for the bias quantity.
    means_by_candidate: dict = {}
    families_seen: list = []
    for family in family_by_expert.values():
        if family not in families_seen:
            families_seen.append(family)

    for cid in candidate_ids:
        rows = _score_rows_for(store, cid, enumerated_scores)
        # Supersede (round-3 contest-core F6): a dissent re-run persists
        # a second set of rows for the SAME bracket round; only the
        # latest row of each (judge, gate, round) counts — each
        # judge–candidate pair contributes its latest row per round
        # exactly once, to agreement and to the bias means alike.
        # Round 0 (round-5 fix spec H5, round-5 gate-core F6): untagged
        # rows are the pair's round 0 — when the pair has ANY tagged
        # row, its untagged rows are superseded wholesale (a pre-fix
        # book's re-run rows no longer double-count beside their tagged
        # successors); a pair with only untagged rows keeps counting
        # them individually, exactly as before.
        tagged_pairs = {
            (row.get("judge_id"), row.get("gate"))
            for row in rows
            if _row_round(row) is not None
        }
        latest_by_key: dict = {}
        ordered_keys: list = []
        for position, row in enumerate(rows):
            round_tag = _row_round(row)
            if round_tag is None:
                if (row.get("judge_id"), row.get("gate")) in tagged_pairs:
                    continue  # round 0, superseded by the pair's tagged rows
                key = ("row", position)
            else:
                key = ("round", row.get("judge_id"), row.get("gate"), round_tag)
            if key not in latest_by_key:
                ordered_keys.append(key)
            latest_by_key[key] = row
        for key in ordered_keys:
            row = latest_by_key[key]
            if row.get("gate") not in _CELL_GATES:
                continue
            family = row.get("judge_family")
            if family is None and row.get("judge_id") in family_by_expert:
                family = family_by_expert[row.get("judge_id")]
            if family is None:
                continue
            if family not in families_seen:
                families_seen.append(family)
            mean = _cell_mean(row)
            if mean is None:
                continue
            means_by_candidate.setdefault(cid, {}).setdefault(family, []).append(mean)
            if cid not in outcomes:
                continue  # no decidable outcome: not a calibration cell
            positive = outcomes[cid]
            total[family] = total.get(family, 0) + 1
            if (mean >= 5) == positive:
                agree[family] = agree.get(family, 0) + 1

    # Bias quantity: per family, its per-candidate cell mean minus the
    # panel median for that candidate, averaged over candidates in
    # contests where the family itself had a candidate.
    contest_families: dict = {}
    for candidate in candidates_by_id.values():
        ref = candidate.get("contract_id")
        family = candidate.get("family")
        if ref is not None and family is not None:
            contest_families.setdefault(ref, set()).add(family)
    bias_diffs: dict = {}
    for cid, by_family in means_by_candidate.items():
        candidate = candidates_by_id.get(cid) or {}
        ref = candidate.get("contract_id")
        if ref is None:
            continue
        in_contest = contest_families.get(ref, set())
        per_family_mean = {
            fam: sum(vals) / len(vals) for fam, vals in by_family.items() if vals
        }
        if not per_family_mean:
            continue
        panel_median = statistics.median(per_family_mean.values())
        for family, fam_mean in per_family_mean.items():
            if family in in_contest:
                bias_diffs.setdefault(family, []).append(fam_mean - panel_median)

    result: dict = {}
    for family in families_seen:
        n = int(total.get(family, 0))
        calibration = (agree.get(family, 0) + 2 * CALIBRATION_PRIOR) / (n + 2)
        diffs = bias_diffs.get(family)
        result[family] = {
            "calibration": float(calibration),
            "n": n,
            "active": bool(n >= CALIBRATION_MIN_CELLS),
            "bias": float(sum(diffs) / len(diffs)) if diffs else None,
        }

    updater = getattr(store, "update_expert_calibration", None)
    if callable(updater):
        for expert_id, family in expert_entries:
            if family is None or family not in result:
                continue
            updater(expert_id, result[family]["calibration"], result[family]["n"])
    return result
