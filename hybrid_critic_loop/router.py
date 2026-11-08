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


def apply_lesson_route_change(store, category, route_change, contract_id):
    """Apply a lesson's route change. Returns a human line, or None.

    If ``route_change`` names an expert to demote, demote it via the store,
    promoting ``route_change['promote']`` or — when the demoted expert is
    the current primary — the ``compete_with`` expert, otherwise keeping
    the current primary in place.  A ledger row records the change.
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

    result = None
    try:
        result = store.apply_route_demotion(category, demote, promote, reason)
    except TypeError:
        result = store.apply_route_demotion(
            category, demote=demote, promote=promote, reason=reason
        )

    note = f"route_change demote {demote} ({reason}) contract {contract_id}"
    try:
        store.add_ledger(expert_id=demote, outcome="observed", note=note)
    except TypeError:
        # Real Store.add_ledger has a wide positional signature:
        # (target_id, contract_id, candidate_id, stage, expert_id,
        #  tokens_in, tokens_out, cost_usd, outcome, note=None)
        try:
            store.add_ledger(None, contract_id, None, None, demote,
                             None, None, None, "observed", note)
        except Exception:
            pass
    except Exception:
        pass

    new_route = result if isinstance(result, dict) and result else route_for(store, category)
    new_primary = (new_route or {}).get("primary", promote)
    return (
        f"Route for {category}: demoted {demote}, promoted {promote} "
        f"(primary is now {new_primary}) — {reason} (contract {contract_id})"
    )
