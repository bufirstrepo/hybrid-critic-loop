"""The judged gate: cross-family panels scoring blinded candidates.

The protocol, enforced in code rather than by convention:

* No judge may score a candidate from its own model family
  (``judge.family not in contest.families``). select_panel filters by
  it, and run_judged_gate re-verifies it against the live executors
  before any packet is sent -- a hand-built panel cannot bypass it.
* Judges see blinded text only: labels "A"/"B" assigned per judge from
  a seeded shuffle, identity lines and worktree paths stripped, expert
  ids and family names replaced. Judges DO see hard-gate results and
  measured numbers -- evidence, not identities.
* No rationale, no score: a candidate score counts only when every
  rubric item carries a numeric score AND a non-empty evidence string.
* Aggregation is per candidate: median of judge means must reach the
  contract minimum, and no single item score anywhere may fall more
  than 2 below it. Wide disagreement (spread of judge means above the
  threshold) escalates to a human instead of averaging away.
"""

from __future__ import annotations

import hashlib
import random
import re
import statistics
import string
from dataclasses import dataclass, field

from .errors import GateError, PanelError
from .rubrics import RUBRICS, get_rubric

_IDENTITY_LINE_PREFIXES = ("diff --git", "index ", "commit ", "Author:", "Date:")
_WT_PATH_RE = re.compile(r"wt/\S+")
_ABS_PATH_RE = re.compile(r"/(home|Users)/\S+")


# ---------------------------------------------------------------- panel

def seeded_shuffle_rng(seed, judge_id) -> random.Random:
    """The per-judge presentation shuffle's RNG, seeded from
    ``(seed, judge_id)``.

    The seed is a STABLE hash — SHA-256 over the string form — not the
    builtin ``hash()``: Python randomises string hashing per process
    (PYTHONHASHSEED), so ``hash((seed, judge_id))`` gave every process
    a different shuffle for the same recorded seed, and the blind
    label assignment / ``position_shown`` evidence could never be
    reproduced from the seed the loop passes (round-11 gatecore F4).
    Shared by the legacy judged gate (here) and the peer matrix
    (matrix.py imports it) so both shuffles mean the same seed the
    same way.
    """
    digest = hashlib.sha256(f"{seed}:{judge_id}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:4], "big"))


def select_panel(route_judges: list, experts: dict, contest_families: set,
                 count: int, available: set | None = None) -> list:
    """Pick exactly ``count`` judges, none from a contest family.

    Preference order: the route's judges, in route order; then every
    other expert, sorted by id. ``available`` (when given) restricts to
    judges whose executor is currently usable. Raises PanelError naming
    the shortfall when not enough eligible judges exist.
    """
    contest = set(contest_families or set())

    def eligible(judge_id) -> bool:
        expert = experts.get(judge_id)
        if not isinstance(expert, dict):
            return False  # cannot verify the family -> cannot seat the judge
        if expert.get("family") in contest:
            return False
        if available is not None and judge_id not in available:
            return False
        return True

    panel: list = []
    for judge_id in route_judges or []:
        if judge_id not in panel and eligible(judge_id):
            panel.append(judge_id)
        if len(panel) >= count:
            break
    if len(panel) < count:
        for judge_id in sorted(experts):
            if judge_id not in panel and eligible(judge_id):
                panel.append(judge_id)
            if len(panel) >= count:
                break
    if len(panel) < count:
        raise PanelError(
            f"judge panel shortfall: need {count}, only {len(panel)} eligible "
            f"(short by {count - len(panel)}); contest families excluded: "
            f"{sorted(contest)}"
        )
    panel = panel[:count]
    # The family-exclusion rule must be impossible to bypass.
    for judge_id in panel:
        assert experts[judge_id].get("family") not in contest, (
            f"judge {judge_id} family {experts[judge_id].get('family')!r} "
            f"is a contest family"
        )
    return panel


def recompute_panel(route, experts: dict, contest_families: set, count: int,
                    available: set | None = None) -> list:
    """Recompute a panel from a route -- the fallback re-dispatch path.

    When a judged gate escalates (panel fell below strength, or judge
    spread too wide), the re-dispatch calls this to rebuild the panel
    from the route definition with the then-current availability, and
    re-runs run_judged_gate with the fresh panel. It is exactly
    select_panel over the route's judge list; ``route`` may be the
    route dict (its "judges" list is used) or a bare judge-id list.
    """
    if isinstance(route, dict):
        route_judges = route.get("judges", [])
    else:
        route_judges = list(route or [])
    return select_panel(route_judges, experts, contest_families, count, available)


# -------------------------------------------------------------- blinding

def blind_text(text: str, strip_terms: list) -> str:
    """Strip identity from a diff before any judge sees it.

    Removes commit-metadata lines, rewrites worktree paths to a generic
    ``wt/<candidate>``, scrubs absolute home paths, and replaces every
    strip term (expert ids, family names) case-insensitively with
    ``<expert>`` — as a whole word only (letters, digits and ``_`` are word
    characters), so a family named ``local`` leaves ``localhost`` and
    ``a_local_var`` alone; terms shorter than four characters are skipped
    (fix pass Wave 2.10, review 1D-11).
    """
    if not text:
        return ""
    kept = []
    for line in text.splitlines():
        if line.lstrip().startswith(_IDENTITY_LINE_PREFIXES):
            continue
        kept.append(line)
    out = "\n".join(kept)
    out = _WT_PATH_RE.sub("wt/<candidate>", out)
    out = _ABS_PATH_RE.sub("<path>", out)
    for term in strip_terms or []:
        term = str(term) if term else ""
        if len(term) < 4:
            continue
        pattern = r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])"
        out = re.sub(pattern, "<expert>", out, flags=re.IGNORECASE)
    return out


def _rubric_id(rubric: dict) -> str:
    rid = rubric.get("id")
    if rid:
        return rid
    for key, value in RUBRICS.items():
        if value.get("items") == rubric.get("items") and value.get("scale") == rubric.get("scale"):
            return key
    return "unknown"


def build_packet(labelled_diffs: dict, measured_summary: str, rubric: dict) -> str:
    """Assemble the single blinded text a judge scores.

    Header (rubric id, items, scale), then the measured/test summary,
    then one section per label. The caller passes pre-blinded diffs;
    build_packet still runs blind_text (strip terms = []) over the
    diffs and summary so path scrubbing cannot be forgotten. No expert
    ids or families are added anywhere.
    """
    rid = _rubric_id(rubric)
    items = ", ".join(rubric.get("items", []))
    lines = [
        f"RUBRIC: {rid} (version {rubric.get('version', '?')})",
        f"SCALE: 0-{rubric.get('scale', 10)} per item",
        f"ITEMS: {items}",
        "",
        "MEASURED / TEST SUMMARY:",
        blind_text(measured_summary or "", []),
        "",
    ]
    for label, diff in labelled_diffs.items():
        lines.append(f"=== CANDIDATE {label} ===")
        lines.append(blind_text(diff or "", []))
        lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------ judged gate

@dataclass
class JudgedOutcome:
    per_candidate: dict = field(default_factory=dict)
    escalated: bool = False
    escalation_reason: str | None = None
    judges_used: list = field(default_factory=list)


def _candidate_id(candidate: dict):
    cid = candidate.get("id")
    if cid is None:
        cid = candidate.get("candidate_id")
    return cid


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _extract_item(items_raw: dict, entry: dict, item_name: str):
    """Pull (score, evidence) for one rubric item, or (None, None).

    Accepted shapes per item: {"score": n, "evidence": s} (also
    "value"/"rationale" spellings), or a bare numeric score backed by
    evidence carried at entry level -- entry["evidence"] as a per-item
    dict or as a single string covering the entry.
    """
    if not isinstance(items_raw, dict) or item_name not in items_raw:
        return None, None
    raw = items_raw[item_name]
    score = None
    evidence = None
    if isinstance(raw, dict):
        score = raw.get("score", raw.get("value"))
        evidence = raw.get("evidence", raw.get("rationale"))
    elif _is_number(raw):
        score = raw
    if evidence is None:
        entry_evidence = entry.get("evidence") if isinstance(entry, dict) else None
        if isinstance(entry_evidence, dict):
            evidence = entry_evidence.get(item_name)
        elif isinstance(entry_evidence, str):
            evidence = entry_evidence
    return score, evidence


def _validate_candidate_score(entry, rubric: dict):
    """Return (items_dict, mean) for a judge's score of one candidate.

    Every rubric item must be present with a numeric score and a
    non-empty string evidence; otherwise the whole score is discarded
    ("no rationale, no score") and None is returned. Every score must
    also lie within the rubric's scale [0, scale] (fix pass round 4,
    spec G6, round-3 contest-core F1) — mirroring the matrix path's
    validator, so an off-scale score (100 on a 0-10 rubric, or a
    negative) discards the whole score instead of passing or poisoning
    the aggregation. NaN fails the range comparison and is discarded
    the same way.
    """
    if not isinstance(entry, dict):
        return None
    items_raw = entry.get("items")
    if not isinstance(items_raw, dict):
        return None
    scale = rubric.get("scale", 10)
    items = {}
    for item_name in rubric["items"]:
        score, evidence = _extract_item(items_raw, entry, item_name)
        if not _is_number(score):
            return None
        if not (0 <= float(score) <= float(scale)):
            return None
        if not (isinstance(evidence, str) and evidence.strip()):
            return None
        items[item_name] = float(score)
    mean = sum(items.values()) / len(items) if items else 0.0
    return items, mean


def _measured_summary_for_labels(order: list, labels: list, gate_results: dict) -> str:
    """Hard pass/fail + measurement values, labelled generically."""
    lines = []
    for label, candidate in zip(labels, order):
        cid = _candidate_id(candidate)
        result = (gate_results or {}).get(cid)
        if result is None:
            lines.append(f"Candidate {label}: no gate results recorded")
            continue
        hard_txt = "PASS" if result.hard_pass else "FAIL"
        line = f"Candidate {label}: hard gates {hard_txt}"
        readings = []
        for measure in result.measured or []:
            for row in measure.metrics or []:
                verdict = "pass" if row.get("passed") else "fail"
                readings.append(
                    f"{row.get('metric')}={row.get('value')} "
                    f"({row.get('op')} {row.get('threshold')}: {verdict})"
                )
        if readings:
            line += "; measurements: " + ", ".join(readings)
        if result.measured_pass is None:
            line += "; measured gates: none"
        else:
            line += f"; measured gates {'PASS' if result.measured_pass else 'FAIL'}"
        lines.append(line)
    return "\n".join(lines)


def run_judged_gate(store, contract: dict, judged_item: dict, candidates: list,
                    gate_results: dict, diffs: dict, judge_executors: dict,
                    panel: list, seed: int = 0) -> JudgedOutcome:
    """Run the judged gate over the surviving candidates.

    ``panel`` must already satisfy family exclusion (see select_panel),
    but the exclusion is re-verified here against the live executors'
    families and the contest families taken from the candidates
    themselves: any violation raises PanelError before judging starts.
    """
    rubric_id = judged_item["rubric"]
    rubric = get_rubric(rubric_id)
    min_score = judged_item["min"]
    judges_needed = int(judged_item.get("judges", 3))
    spread_threshold = float(judged_item.get("spread_threshold", 2.0))

    contest_families = {
        c.get("family") for c in candidates if isinstance(c, dict) and c.get("family")
    }
    # Re-verify family exclusion against the live executors.
    for judge_id in panel:
        executor = (judge_executors or {}).get(judge_id)
        if executor is None:
            raise PanelError(f"panel judge {judge_id!r} has no executor to verify")
        family = getattr(executor, "family", None)
        if family in contest_families:
            raise PanelError(
                f"judge {judge_id!r} family {family!r} is a contest family; "
                f"a judge may never score its own family"
            )

    # Strip terms: every expert id and family in the contest, plus the
    # seated judges' ids and families -- nothing identifying reaches a
    # judge, including the judges' own identities.
    strip_terms: list = []
    for candidate in candidates:
        for key in ("expert_id", "expert", "author_id", "author", "family"):
            value = candidate.get(key) if isinstance(candidate, dict) else None
            if value and str(value) not in strip_terms:
                strip_terms.append(str(value))
    for judge_id in panel:
        if judge_id not in strip_terms:
            strip_terms.append(str(judge_id))
        family = getattr(judge_executors[judge_id], "family", None)
        if family and str(family) not in strip_terms:
            strip_terms.append(str(family))

    packet_rubric = dict(rubric)
    packet_rubric["id"] = rubric_id
    labels_pool = list(string.ascii_uppercase)

    # candidate_id -> {"judge_means": [...], "item_scores": [...]}
    collected: dict = {_candidate_id(c): {"judge_means": [], "item_scores": []}
                        for c in candidates}
    valid_judges: list = []

    for judge_id in panel:
        executor = judge_executors[judge_id]
        judge_family = getattr(executor, "family", None)
        rng = seeded_shuffle_rng(seed, judge_id)
        order = list(candidates)
        rng.shuffle(order)
        labels = labels_pool[: len(order)]
        labelled_diffs = {}
        for label, candidate in zip(labels, order):
            cid = _candidate_id(candidate)
            labelled_diffs[label] = blind_text((diffs or {}).get(cid, ""), strip_terms)
        summary = _measured_summary_for_labels(order, labels, gate_results)
        packet = build_packet(labelled_diffs, summary, packet_rubric)
        try:
            raw = executor.judge(packet, rubric)
        except Exception:  # noqa: BLE001 - a failing judge yields no scores
            raw = None
        if not isinstance(raw, dict):
            continue
        judge_scored_any = False
        for position, (label, candidate) in enumerate(zip(labels, order), start=1):
            validated = _validate_candidate_score(raw.get(label), rubric)
            if validated is None:
                continue  # no rationale, no score -- discarded
            items, mean = validated
            cid = _candidate_id(candidate)
            store.add_score(
                cid, "judged", rubric_id, judge_id, judge_family,
                True, position, items, mean,
            )
            collected[cid]["judge_means"].append(mean)
            collected[cid]["item_scores"].extend(items.values())
            judge_scored_any = True
        if judge_scored_any:
            valid_judges.append(judge_id)

    per_candidate: dict = {}
    for candidate in candidates:
        cid = _candidate_id(candidate)
        data = collected.get(cid, {"judge_means": [], "item_scores": []})
        judge_means = data["judge_means"]
        if judge_means:
            median_mean = float(statistics.median(judge_means))
            spread = float(max(judge_means) - min(judge_means))
        else:
            median_mean = 0.0
            spread = 0.0
        min_item = float(min(data["item_scores"])) if data["item_scores"] else 0.0
        passed = bool(judge_means) and median_mean >= min_score and min_item >= min_score - 2
        per_candidate[cid] = {
            "median_mean": median_mean,
            "min_item": min_item,
            "spread": spread,
            "pass": passed,
            "judge_means": list(judge_means),
        }

    if len(valid_judges) < judges_needed:
        return JudgedOutcome(
            per_candidate=per_candidate,
            escalated=True,
            escalation_reason=(
                f"judge panel fell below {judges_needed} after validation"
            ),
            judges_used=list(valid_judges),
        )

    worst_spread = max(
        (entry["spread"] for entry in per_candidate.values()), default=0.0
    )
    if worst_spread > spread_threshold:
        return JudgedOutcome(
            per_candidate=per_candidate,
            escalated=True,
            escalation_reason=(
                f"judge spread {worst_spread} above threshold {spread_threshold}"
            ),
            judges_used=list(valid_judges),
        )

    return JudgedOutcome(
        per_candidate=per_candidate,
        escalated=False,
        escalation_reason=None,
        judges_used=list(valid_judges),
    )
