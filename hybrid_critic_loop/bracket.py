"""The Millionaire Matrix step-down bracket.

Owner correction (v0.3): the funnel is a SCORED STEP-DOWN BRACKET,
not a synthesis stage. There are no synthesizer seats and no merge
step. The whole panel generates once; then rounds repeat the full
gate stack (hard, measured, peer validity matrix) over the surviving
field and the field narrows by recorded score until one candidate
remains for the human gate.

Cut size is configurable per contract (owner refinement):

* ``bracket.cut_mode: "fixed"`` (default) with
  ``bracket.cut_per_round`` (default 2): the field shrinks by that
  many candidates per round — 16 -> 14 -> 12 -> ... -> 2 -> final
  scored round -> 1. The gentle ladder is the default because every
  extra round is a full matrix pass: the step-down rate is a cost
  dial, which the ledger's cost-per-validity-point makes visible.
* ``bracket.cut_mode: "half"``: the halving ladder — the top half
  advances, rounding up for odd fields (9 -> 5 -> 3 -> 2 -> 1).

Either way the ladder always lands on a final TWO, and then a final
scored round decides the one: if a cut would jump past 2 it cuts to
exactly 2. The one exception is a round in which the matrix passes
nobody: the anchor removes every candidate before the cut, the round
records the empty field (``passed=0``) and the run escalates or offers
a measured waiver — there is nothing left to cut.

Rules that never bend:

* ANCHOR: a candidate eliminated by a hard or measured gate is out at
  ANY round, regardless of matrix score. Gate rank is unchanged — the
  matrix never overrules hard/measured.
* Cuts are pure functions of recorded scores (matrix validity +
  gate results). Nothing is regenerated between rounds; a candidate's
  artifact never changes (no revision round in the entry).
* Tie at the cut line: fewer tokens wins, then earlier completion
  (the Phase-0 tie rule).
* Every round records round number, field in, field out, and the cut
  line score (the validity score of the last candidate to advance;
  None when nobody was cut) so the step-down is auditable.
"""

from __future__ import annotations

DEFAULT_CUT_PER_ROUND = 2
CUT_MODES = ("fixed", "half")


def bracket_config(contract: dict) -> dict:
    """Normalise the contract's ``bracket`` block.

    ``contracts.normalize_contract`` already validates the shape; this
    helper also tolerates raw dicts (tests, tools) by applying the
    same defaults defensively.
    """
    raw = (contract or {}).get("bracket") or {}
    if not isinstance(raw, dict):
        raw = {}
    mode = raw.get("cut_mode", "fixed")
    if mode not in CUT_MODES:
        mode = "fixed"
    cut = raw.get("cut_per_round", DEFAULT_CUT_PER_ROUND)
    if not isinstance(cut, int) or isinstance(cut, bool) or cut < 1:
        cut = DEFAULT_CUT_PER_ROUND
    return {"cut_mode": mode, "cut_per_round": cut}


def next_field_size(current: int, config: dict) -> int:
    """How many candidates survive the next cut.

    Never below 1, never above ``current - 1`` while more than one
    candidate remains, and never jumps past the final two.
    """
    if current <= 1:
        return current
    if current == 2:
        return 1  # the final scored round
    if config.get("cut_mode") == "half":
        target = (current + 1) // 2  # top half, rounding up
    else:
        target = current - int(config.get("cut_per_round", DEFAULT_CUT_PER_ROUND))
    if target < 2:
        target = 2  # a cut never jumps past the final two
    if target >= current:
        target = current - 1
    return target


def rank_candidates(candidates, validity: dict, durations: dict | None = None) -> list:
    """Order candidates for a cut: validity desc, then fewer tokens,
    then earlier completion (shorter recorded duration).

    ``validity`` maps candidate id -> matrix validity score
    (median_mean). A candidate with no recorded validity sorts last.
    Pure function of the recorded values — no store access.
    """
    durations = durations or {}

    def _tokens(candidate):
        tin, tout = candidate.get("tokens_in"), candidate.get("tokens_out")
        if tin is None and tout is None:
            return float("inf")
        return float((tin or 0) + (tout or 0))

    return sorted(
        candidates,
        key=lambda c: (
            -(validity.get(c["id"]) if validity.get(c["id"]) is not None else -1.0),
            _tokens(c),
            durations.get(c["id"], 0),
        ),
    )


def apply_cut(field, validity: dict, config: dict,
              durations: dict | None = None, field_size: int | None = None) -> dict:
    """Cut ``field`` (candidate dicts: this round's matrix passers) down
    to the next ladder size by recorded validity.

    ``field_size`` is the size of the whole field this round — the passers
    plus the candidates the matrix failed, which the anchor has already
    removed. The ladder steps down from it, so a failer counts as one of
    the round's eliminations and never displaces a passer at the line
    (fix pass Wave 2.8, review M5). Defaults to ``len(field)``.

    Returns {advanced, eliminated, cut_line}: ``cut_line`` is the
    validity score of the last candidate to advance, or None when nobody
    was cut (the field already fits) or nobody advanced.
    """
    ranked = rank_candidates(field, validity, durations)
    whole = field_size if field_size is not None else len(ranked)
    target = next_field_size(whole, config)
    advanced = ranked[:target]
    eliminated = ranked[target:]
    cut_line = None
    if advanced and (eliminated or whole > len(ranked)):
        score = validity.get(advanced[-1]["id"])
        cut_line = float(score) if score is not None else None
    return {"advanced": advanced, "eliminated": eliminated, "cut_line": cut_line}
