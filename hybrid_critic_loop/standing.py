"""Standing: the client book — lifetime measured spend, as tiers.

A store's standing is its *lifetime* record, the way a boutique's client
book is lifetime spend, not one visit: every token and every measured
dollar the ledger has ever recorded, summed. Tokens never convert to
dollars, and where a provider reported no cost the spend reads
"not reported" — the same discipline the ledger report and CPVP already
keep (reports print only database rows; nothing is estimated).

**The one definition of a counted row (tightening spec T1).** Every
spend figure in the plugin — this module's standing line and client
book, the verdict stamp, and :mod:`hybrid_critic_loop.chain`'s
certificates — comes from a single aggregation, :func:`ledger_totals`,
shared by both modules, so the surfaces can never disagree again
(security F5, correctness F6, claims #2). A ledger row is *counted*
when:

- its outcome is a generation outcome (a dispatch: ``completed`` /
  ``refused`` / ``timeout`` / ``unavailable`` / ``error``), and
- its provenance is counted (tightening spec T2): ``"executor-reported"``,
  a candidate provenance string the dispatch path passed, or
  ``"legacy-import"`` (rows written before provenance existed — a row
  dict with no provenance at all reads as ``"legacy-import"`` too).
  NOT counted: ``"internal"`` (the plugin's own bookkeeping rows) and
  ``"unattributed"`` (a writer that named no source). A front-door
  ledger write carrying invented numbers under an internal or
  unattributed provenance therefore raises no tier (security F2).

Sums are reported-only: a field no provider reported stays out of the
sum, and a total with no reported values at all is None ("not
reported"), never an estimate. Aggregated spend is rounded to cents
before any tier or gap math runs on it (tightening spec T8;
correctness F4). Rows that carry a cost the totals do not count are
never dropped silently: :func:`compute_standing` reports them as
excluded totals (``excluded_cost_usd`` / ``excluded_rows``).
"""

from __future__ import annotations

# The ladder, highest first: (tier name, lifetime measured spend floor).
# WINDOW SHOPPER is the floor tier — no measured spend at all (or a
# measured total under the CLIENT floor).
TIERS = (
    ("MILLIONAIRE", 1_000_000.0),
    ("BALLER", 100_000.0),
    ("PLAYER", 10_000.0),
    ("HIGH ROLLER", 1_000.0),
    ("CLIENT", 0.01),
    ("WINDOW SHOPPER", None),
)

#: Outcomes that make a ledger row a dispatch (mirrors
#: ``state._GENERATION_OUTCOMES``; the queue's ``escalated`` /
#: ``observed`` notes are bookkeeping, not dispatches).
GENERATION_OUTCOMES = (
    "completed", "refused", "timeout", "unavailable", "error")

#: Provenances the totals never count (tightening spec T2).
_NON_COUNTED_PROVENANCES = frozenset({"internal", "unattributed"})

#: The provenance a row with none recorded reads as: rows written
#: before the provenance column existed are imports of the old book
#: and count (the state migration labels them exactly this).
_MISSING_PROVENANCE = "legacy-import"


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _sum_or_none(values):
    present = [v for v in values if _is_number(v)]
    if not present:
        return None
    return sum(present)


def _round_cents(value):
    """Round an aggregated spend to cents (tightening spec T8)."""
    if value is None:
        return None
    return round(float(value), 2)


def _row_provenance(row):
    prov = row.get("provenance")
    if isinstance(prov, str) and prov:
        return prov
    return _MISSING_PROVENANCE


def _is_counted_row(row):
    """The counted-row test from this module's docstring (spec T1/T2)."""
    return (
        row.get("outcome") in GENERATION_OUTCOMES
        and _row_provenance(row) not in _NON_COUNTED_PROVENANCES
    )


def ledger_totals(store, contract_id=None, upto_id=None) -> dict:
    """The one aggregation: counted-row totals over a store's ledger.

    With ``contract_id``, only that contract's rows are in scope; with
    ``upto_id``, only rows with ``id <= upto_id`` (a certificate's
    high-water prefix). Returns reported-only sums — each of
    ``tokens_in`` / ``tokens_out`` / ``tokens`` / ``cost_usd`` is None
    when no counted row in scope reported it — with ``cost_usd``
    rounded to cents, plus the coverage counts ``cost_rows`` (counted
    rows that reported a cost) and ``cost_missing`` (counted rows that
    reported none), plus the excluded totals: ``excluded_cost_usd`` /
    ``excluded_rows`` over in-scope rows that carry a cost the totals
    do not count (non-generation outcomes, or internal / unattributed
    provenance), so excluded money is always named, never vanished.
    """
    try:
        rows = store.ledger_rows()
    except TypeError:
        rows = store.ledger_rows(None)
    tin_values, tout_values, cost_values = [], [], []
    cost_rows = 0
    cost_missing = 0
    excluded_values = []
    excluded_rows = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if contract_id is not None and row.get("contract_id") != contract_id:
            continue
        if upto_id is not None:
            rid = row.get("id")
            if not _is_number(rid) or rid > upto_id:
                continue
        if _is_counted_row(row):
            if _is_number(row.get("tokens_in")):
                tin_values.append(row["tokens_in"])
            if _is_number(row.get("tokens_out")):
                tout_values.append(row["tokens_out"])
            if _is_number(row.get("cost_usd")):
                cost_values.append(row["cost_usd"])
                cost_rows += 1
            else:
                cost_missing += 1
        elif _is_number(row.get("cost_usd")):
            excluded_values.append(row["cost_usd"])
            excluded_rows += 1
    tokens_in = _sum_or_none(tin_values)
    tokens_out = _sum_or_none(tout_values)
    if tokens_in is None and tokens_out is None:
        tokens = None
    else:
        tokens = (tokens_in or 0) + (tokens_out or 0)
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens": tokens,
        "cost_usd": _round_cents(_sum_or_none(cost_values)),
        "cost_rows": cost_rows,
        "cost_missing": cost_missing,
        "excluded_cost_usd": _round_cents(_sum_or_none(excluded_values)),
        "excluded_rows": excluded_rows,
    }


def tier_for(spend) -> str:
    """The tier for a lifetime measured spend (None = nothing measured)."""
    if not _is_number(spend):
        return "WINDOW SHOPPER"
    for name, floor in TIERS:
        if floor is not None and spend >= floor:
            return name
    return "WINDOW SHOPPER"


def next_tier(spend):
    """(next tier name, additional measured spend required) or None.

    None at MILLIONAIRE — there is no room above it. An unmeasured spend
    counts as zero measured so far, so the next tier is CLIENT at its
    $0.01 floor. The gap is computed on the cent-rounded spend
    (tightening spec T8).
    """
    current = tier_for(spend)
    if current == "MILLIONAIRE":
        return None
    base = _round_cents(float(spend)) if _is_number(spend) else 0.0
    floors = {name: floor for name, floor in TIERS if floor is not None}
    names = [name for name, _floor in TIERS]
    nxt = names[names.index(current) - 1]
    return nxt, max(_round_cents(floors[nxt] - base), 0.0)


def format_money(value) -> str:
    if not _is_number(value):
        return "not reported"
    return f"${float(value):,.2f}"


def format_tokens(value) -> str:
    if not _is_number(value):
        return "not reported"
    if float(value) == int(float(value)):
        return f"{int(float(value)):,}"
    return f"{float(value):,.2f}"


def compute_standing(store) -> dict:
    """Lifetime standing from the store's recorded rows.

    Headline totals come from the one aggregation (:func:`ledger_totals`):
    tokens in/out and combined, measured cost (cent-rounded) — each None
    when nothing counted was ever reported — the headline tier from that
    cost, the cost coverage counts (``cost_rows`` / ``cost_missing``),
    and the excluded totals for cost-carrying rows the aggregation does
    not count. Plus one row per dispatched expert: the ledger summary's
    own fields plus that expert's validity tier, taken from the expert's
    measured validity cost where present.
    """
    totals = ledger_totals(store)
    try:
        summary = store.ledger_summary()
    except TypeError:
        summary = store.ledger_summary(None)
    summary = summary or []
    experts = []
    for row in summary:
        expert_row = dict(row)
        validity_tier = tier_for(row.get("validity_cost_usd"))
        expert_row["tier"] = validity_tier
        expert_row["validity_tier"] = validity_tier
        experts.append(expert_row)
    return {
        "tokens_in": totals["tokens_in"],
        "tokens_out": totals["tokens_out"],
        "tokens": totals["tokens"],
        "cost_usd": totals["cost_usd"],
        "tier": tier_for(totals["cost_usd"]),
        "cost_rows": totals["cost_rows"],
        "cost_missing": totals["cost_missing"],
        "excluded_cost_usd": totals["excluded_cost_usd"],
        "excluded_rows": totals["excluded_rows"],
        "experts": experts,
    }


def has_standing(store) -> bool:
    """True when the store has any dispatch on record to take standing from."""
    try:
        return bool(compute_standing(store)["experts"])
    except Exception:
        return False


def format_standing_line(standing) -> str:
    """The one-line standing, from a :func:`compute_standing` result.

    Partial cost coverage is rendered, never hidden (tightening spec
    T8; correctness F3): when some counted dispatches reported no cost,
    the spend carries "(N of M dispatches reported cost)".
    """
    spend = standing.get("cost_usd")
    cost_rows = standing.get("cost_rows") or 0
    cost_missing = standing.get("cost_missing") or 0
    coverage = ""
    if cost_missing > 0:
        coverage = (
            f" ({cost_rows} of {cost_rows + cost_missing} "
            "dispatches reported cost)"
        )
    if _is_number(spend):
        spend_text = f"{format_money(spend)} lifetime measured spend{coverage}"
    else:
        spend_text = f"lifetime measured spend not reported{coverage}"
    tokens = standing.get("tokens")
    if _is_number(tokens):
        tokens_text = f"{format_tokens(tokens)} tokens"
    else:
        tokens_text = "tokens not reported"
    return (
        f"Millionaire Matrix standing: {standing.get('tier')} — "
        f"{spend_text} · {tokens_text}"
    )


def standing_line(store):
    """The one-line standing for a store (see :func:`format_standing_line`)."""
    return format_standing_line(compute_standing(store))


def contract_totals(store, contract_id, upto_id=None) -> dict:
    """Tokens and cost recorded in the ledger for one contract.

    Delegates to the one aggregation (:func:`ledger_totals`), so a
    run's totals here are the certificate's totals in
    :mod:`hybrid_critic_loop.chain` by construction. Each total is None
    when no counted row for the contract reported it — a run whose
    providers reported nothing has nothing to stamp.
    """
    totals = ledger_totals(store, contract_id=contract_id, upto_id=upto_id)
    return {"tokens": totals["tokens"], "cost_usd": totals["cost_usd"]}


def verdict_standing_line(store, contract_id):
    """The standing line a verdict report carries for one contract.

    The store's current tier, plus this run's own recorded contribution
    (tokens and cost on this contract's ledger rows, or "not reported").
    Returns None when the store cannot produce standing at all (a
    duck-typed report fake), so reports stay silent rather than guess.
    """
    try:
        standing = compute_standing(store)
        totals = contract_totals(store, contract_id)
    except Exception:
        return None
    tokens, cost = totals.get("tokens"), totals.get("cost_usd")
    if not _is_number(tokens) and not _is_number(cost):
        contribution = "this run: not reported"
    else:
        parts = []
        if _is_number(tokens):
            parts.append(f"{format_tokens(tokens)} tokens")
        if _is_number(cost):
            parts.append(format_money(cost))
        contribution = "this run: " + " · ".join(parts)
    return f"standing: {standing.get('tier')} — {contribution}"


def standing_report(store) -> str:
    """The full client book for the local store, as text."""
    from .reports import (
        _calibration_text,
        _cpvp_text,
        _value_or_not_reported,
        _win_rate_text,
    )

    standing = compute_standing(store)
    lines = ["standing — the client book:", format_standing_line(standing)]
    upcoming = next_tier(standing.get("cost_usd"))
    if upcoming is not None:
        name, additional = upcoming
        if (standing.get("cost_missing") or 0) > 0:
            # Partial coverage: the gap is a floor, not an exact figure
            # (tightening spec T8; correctness F3).
            lines.append(
                f"next tier: {name} — at least "
                f"{format_money(additional)} more measured spend"
            )
        else:
            lines.append(
                f"next tier: {name} — {format_money(additional)} "
                "more measured spend"
            )
    excluded_cost = standing.get("excluded_cost_usd")
    excluded_rows = standing.get("excluded_rows") or 0
    if excluded_rows and _is_number(excluded_cost):
        # Money the book carries but the totals do not count is named,
        # never dropped silently (tightening spec T2; security F2).
        lines.append(
            f"{format_money(excluded_cost)} across {excluded_rows} rows "
            "not counted — unattributed/internal writers"
        )
    experts = standing["experts"]
    if not experts:
        lines.append("experts: not run")
        return "\n".join(lines)
    lines.append("experts:")
    for row in experts:
        expert = row.get("expert_id", row.get("expert", "?"))
        line = (
            f"{expert}: dispatches={row.get('dispatches', 0)} "
            f"completed={row.get('completed', 0)} "
            f"refused={row.get('refused', 0)} "
            f"timeout={row.get('timeout', row.get('timeouts', row.get('timed_out', 0)))} "
            f"unavailable={row.get('unavailable', 0)} "
            f"error={row.get('error', 0)} "
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
        # validity_tier, not tier: it is computed from validity cost, a
        # different money stream than the dispatch cost printed beside
        # it (tightening spec T8; correctness F13).
        line += f" validity_tier={row.get('validity_tier', row.get('tier'))}"
        lines.append(line)
    return "\n".join(lines)
