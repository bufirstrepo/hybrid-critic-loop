"""Routing table for the Hybrid Critic Loop.

Routes decide, per category, which expert is primary, which one it competes
against, the fallback order, and the preferred judge panel.  Lessons can
demote an expert that lost on a gate; :func:`apply_lesson_route_change`
applies that demotion through the store and records it in the ledger.

Only the store API described in the build brief is used, and route seeding
is defensive: the brief's Store subset exposes ``get_route`` but no explicit
setter, so :func:`ensure_routes` tries the plausible setter names and a
plain ``routes`` dict attribute before giving up silently (a Store whose
``get_route`` already returns defaults needs no seeding at all).
"""

from __future__ import annotations

import copy

#: The owner's target panel (v0.3 owner decision): eight families.
#: The panel composes from the seats whose executors report available;
#: the floor rules (verdict degraded when fewer than 2 distinct peer
#: families score a candidate) live in the matrix engine.
PANEL_FAMILIES = [
    "anthropic", "openai", "xai", "google",
    "reflection", "alibaba", "moonshot", "deepseek",
]

DEFAULT_ROUTES = {
    "code": {
        "primary": "codex",
        "compete_with": "grok",
        "fallback": ["claude", "agy", "qwen-local", "kimi", "deepseek",
                     "beam", "local-solver"],
        # The google seat is `agy` (no executor is called `gemini`; fix pass
        # Wave 2.10, review 1D-17). The matrix seats peers from the live
        # executors anyway; this list serves the legacy judge path.
        "judges": ["agy", "beam", "grok", "qwen-local", "claude", "codex"],
        "rule": "judge.family not in contest.families; panel recomputed on fallback",
        "panel": list(PANEL_FAMILIES),
        "scoring": "peer_matrix; no self-scoring; blind",
    }
}

_SETTER_NAMES = ("set_route", "upsert_route", "ensure_route", "save_route", "put_route")


def category_of(domain: str) -> str:
    """Return the routing category for a domain: its first dotted segment."""
    if not domain:
        return "code"
    first = str(domain).split(".")[0].strip()
    return first or "code"


def _seed_one(store, category, route) -> bool:
    """Try to persist ``route`` for ``category`` on ``store``. Returns success."""
    for name in _SETTER_NAMES:
        method = getattr(store, name, None)
        if not callable(method):
            continue
        try:
            method(category, copy.deepcopy(route))
            return True
        except TypeError:
            pass
        try:
            method(category, **copy.deepcopy(route))
            return True
        except TypeError:
            continue
        except Exception:
            return False
    routes = getattr(store, "routes", None)
    if isinstance(routes, dict):
        routes[category] = copy.deepcopy(route)
        return True
    return False


def ensure_routes(store):
    """Upsert defaults for categories not present in the store."""
    for category, route in DEFAULT_ROUTES.items():
        existing = None
        try:
            existing = store.get_route(category)
        except Exception:
            existing = None
        if existing:
            continue
        _seed_one(store, category, route)


def route_for(store, category) -> dict:
    """Return the route for ``category`` from the store, seeding if absent."""
    if not category:
        category = "code"
    existing = None
    try:
        existing = store.get_route(category)
    except Exception:
        existing = None
    if existing:
        return existing
    # Seed just this category if we have a default for it, else all defaults.
    if category in DEFAULT_ROUTES:
        _seed_one(store, category, DEFAULT_ROUTES[category])
    else:
        ensure_routes(store)
    try:
        existing = store.get_route(category)
    except Exception:
        existing = None
    if existing:
        return existing
    fallback = DEFAULT_ROUTES.get(category) or DEFAULT_ROUTES["code"]
    return copy.deepcopy(fallback)


def _route_positions(route):
    """The fields a demotion can move, as a comparable tuple."""
    if not isinstance(route, dict):
        return None
    return (
        route.get("primary"),
        route.get("compete_with"),
        tuple(route.get("fallback") or ()),
    )


def apply_lesson_route_change(store, category, route_change, contract_id):
    """Apply a lesson's route change. Returns a human line, or None.

    If ``route_change`` names an expert to demote, demote it via the store,
    promoting ``route_change['promote']`` or — when the demoted expert is
    the current primary — the ``compete_with`` expert, otherwise keeping
    the current primary in place.  A ledger row records the change.

    The line reports what the demotion call's RETURN says actually
    changed (fix spec G8; round-3 contest-core F5): the store's
    ``apply_route_demotion`` returns the resulting route plus a
    ``changed`` flag — when nothing moved (the demoted expert holds no
    route position), the line says so and NO ledger row is written.
    Before, most demotions were silent no-ops in the store while this
    line and a ledger row both claimed they happened. Stores whose
    demotion returns no ``changed`` flag (older doubles) are read by
    comparing the route before and after the call.
    """
    if not route_change or not route_change.get("demote"):
        return None
    demote = route_change["demote"]
    reason = route_change.get("reason") or ""
    category = category or route_change.get("category") or "code"

    current = route_for(store, category) or {}
    primary = current.get("primary")
    compete_with = current.get("compete_with")
    promote = route_change.get("promote") or (
        primary if demote != primary else compete_with
    )
    before = _route_positions(current)

    result = None
    try:
        result = store.apply_route_demotion(category, demote, promote, reason)
    except TypeError:
        result = store.apply_route_demotion(
            category, demote=demote, promote=promote, reason=reason
        )

    if isinstance(result, dict) and "changed" in result:
        # The store reported the change itself: speak from it exactly.
        changed = bool(result["changed"])
        promoted_actual = result.get("promoted")
        new_route = result
    else:
        new_route = result if isinstance(result, dict) and result else route_for(store, category)
        changed = _route_positions(new_route) != before
        promoted_actual = promote if changed else None
    new_primary = (new_route or {}).get("primary", primary)

    if not changed:
        return (
            f"Route for {category}: no route change — {demote} holds no "
            f"route position to demote from — {reason} (contract {contract_id})"
        )

    note = f"route_change demote {demote} ({reason}) contract {contract_id}"
    try:
        # Tightening spec T2: router bookkeeping is an internal writer.
        store.add_ledger(expert_id=demote, outcome="observed", note=note,
                         provenance="internal")
    except TypeError:
        # Real Store.add_ledger has a wide positional signature:
        # (target_id, contract_id, candidate_id, stage, expert_id,
        #  tokens_in, tokens_out, cost_usd, outcome, note=None,
        #  provenance=None)
        try:
            store.add_ledger(None, contract_id, None, None, demote,
                             None, None, None, "observed", note,
                             provenance="internal")
        except Exception:
            pass
    except Exception:
        pass

    if promoted_actual:
        return (
            f"Route for {category}: demoted {demote}, promoted {promoted_actual} "
            f"(primary is now {new_primary}) — {reason} (contract {contract_id})"
        )
    return (
        f"Route for {category}: demoted {demote} within the fallback order "
        f"(primary is now {new_primary}) — {reason} (contract {contract_id})"
    )
