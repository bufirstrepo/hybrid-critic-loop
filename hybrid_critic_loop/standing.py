"""Standing: the client book — lifetime measured spend, as tiers.

A store's standing is its *lifetime* record, the way a boutique's client
book is lifetime spend, not one visit: every token and every measured
dollar the ledger has ever recorded, summed. Tokens never convert to
dollars, and where a provider reported no cost the spend reads
"not reported" — the same discipline the ledger report and CPVP already
keep (reports print only database rows; nothing is estimated).

Aggregation is the store's own (:meth:`Store.ledger_summary` for dispatch
totals, the experts table for validity fields); this module only tiers
and formats what those rows say.
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


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _sum_or_none(values):
    present = [v for v in values if _is_number(v)]
    if not present:
        return None
    return sum(present)


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
    $0.01 floor.
    """
    current = tier_for(spend)
    if current == "MILLIONAIRE":
        return None
    base = float(spend) if _is_number(spend) else 0.0
    floors = {name: floor for name, floor in TIERS if floor is not None}
    names = [name for name, _floor in TIERS]
    nxt = names[names.index(current) - 1]
    return nxt, max(floors[nxt] - base, 0.0)


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

    Returns totals (tokens in/out and combined, measured cost — each None
    when nothing was ever reported), the headline tier (from the lifetime
    measured cost), and one row per dispatched expert: the ledger
    summary's own fields plus that expert's tier, taken from the expert's
    measured validity cost where present.
    """
    try:
        summary = store.ledger_summary()
    except TypeError:
        summary = store.ledger_summary(None)
    summary = summary or []
    tokens_in = _sum_or_none([row.get("tokens_in") for row in summary])
    tokens_out = _sum_or_none([row.get("tokens_out") for row in summary])
    if tokens_in is None and tokens_out is None:
        tokens = None
    else:
        tokens = (tokens_in or 0) + (tokens_out or 0)
    cost = _sum_or_none([row.get("cost_usd") for row in summary])
    experts = []
    for row in summary:
        expert_row = dict(row)
        expert_row["tier"] = tier_for(row.get("validity_cost_usd"))
        experts.append(expert_row)
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens": tokens,
        "cost_usd": cost,
        "tier": tier_for(cost),
        "experts": experts,
    }


def has_standing(store) -> bool:
    """True when the store has any dispatch on record to take standing from."""
    try:
        return bool(compute_standing(store)["experts"])
    except Exception:
        return False


def format_standing_line(standing) -> str:
    """The one-line standing, from a :func:`compute_standing` result."""
    spend = standing.get("cost_usd")
    if _is_number(spend):
        spend_text = f"{format_money(spend)} lifetime measured spend"
    else:
        spend_text = "lifetime measured spend not reported"
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


def contract_totals(store, contract_id) -> dict:
    """Tokens and cost recorded in the ledger for one contract.

    Each total is None when no row for the contract reported it — a run
    whose providers reported nothing has nothing to stamp.
    """
    rows = store.ledger_rows()
    mine = [
        row for row in rows or []
        if isinstance(row, dict) and row.get("contract_id") == contract_id
    ]
    tokens_in = _sum_or_none([row.get("tokens_in") for row in mine])
    tokens_out = _sum_or_none([row.get("tokens_out") for row in mine])
    if tokens_in is None and tokens_out is None:
        tokens = None
    else:
        tokens = (tokens_in or 0) + (tokens_out or 0)
    return {
        "tokens": tokens,
        "cost_usd": _sum_or_none([row.get("cost_usd") for row in mine]),
    }


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
        lines.append(
            f"next tier: {name} — {format_money(additional)} "
            "more measured spend"
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
        line += f" tier={row.get('tier')}"
        lines.append(line)
    return "\n".join(lines)
