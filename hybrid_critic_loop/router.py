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


def _gate_checked_promote(route_change, demote, current, passed):
    """The promotion candidate under the owner's route rule.

    Owner ruling 2026-11-09 (Item 1 = A): never promote a seat that
    failed the deciding gate. An explicitly named promote is honoured
    only when it passed; otherwise the first gate-passing seat in
    route order after the demoted seat's slot is chosen. Returns None
    when no gate-passing seat can take a top slot.
    """
    explicit = route_change.get("promote")
    if explicit and explicit != demote and explicit in passed:
        return explicit
    primary = current.get("primary")
    compete_with = current.get("compete_with")
    fallback = list(current.get("fallback") or [])
    if demote == primary:
        pool = [compete_with, *fallback]
    elif demote == compete_with:
        pool = list(fallback)
    else:
        pool = []
    for seat in pool:
        if seat is not None and seat != demote and seat in passed:
            return seat
    return None


def apply_lesson_route_change(store, category, route_change, contract_id,
                              gate_passed=None):
    """Apply a lesson's route change. Returns a human line, or None.

    If ``route_change`` names an expert to demote, demote it via the store,
    promoting ``route_change['promote']`` or — when the demoted expert is
    the current primary — the ``compete_with`` expert.  A ledger row
    records the change.

    Owner ruling 2026-11-09 (Item 1 = A; FIX-SPEC-DECISIONS D1):
    "Demote = move the loser behind every seat that passed the
    deciding gate; never promote a seat that failed it." When the
    caller supplies ``gate_passed`` (the experts whose candidates
    passed the deciding gate), the store rebuilds the route order
    around the passed set — gate-passing seats first, in route
    order, then the loser, then the rest — so the loser lands
    behind every gate-passing seat. An explicitly named promote
    does NOT steer that rebuild, not even when the named seat
    itself passed: the seat that takes a top slot is the first
    gate-passing seat in route order (round-9 correction — the
    earlier wording of this docstring claimed the named promote
    was honoured in passed mode; the store never read it there).
    A named promote is honoured only on the legacy path below,
    subject to the store's ``primary == compete_with`` guard. If no
    gate-passing seat exists to promote, the route cannot move: the
    demotion is still recorded — a ledger row and a line that say
    plainly that no gate-passing seat was available — instead of an
    unqualified promotion. Callers that supply no gate results keep
    the pre-ruling promotion default (the store's own
    ``primary == compete_with`` guard still applies).

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
    passed = set(gate_passed) if gate_passed is not None else None
    if passed is not None:
        promote = _gate_checked_promote(route_change, demote, current, passed)
    else:
        # Pre-ruling default, minus the defect-2 trap: the primary is
        # never named as the promote for another seat's demotion (the
        # store used to seat it as compete_with alongside itself).
        promote = route_change.get("promote") or (
            compete_with if demote == primary else None
        )
    before = _route_positions(current)

    result = None
    if passed is not None:
        try:
            result = store.apply_route_demotion(
                category, demote, promote, reason, passed_experts=passed
            )
        except TypeError:
            result = None
    if result is None:
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
        displaced_actual = result.get("displaced")
        branch = result.get("branch")
        new_route = result
    else:
        new_route = result if isinstance(result, dict) and result else route_for(store, category)
        changed = _route_positions(new_route) != before
        promoted_actual = promote if changed else None
        displaced_actual = None
        branch = None
    new_primary = (new_route or {}).get("primary", primary)
    new_compete = (new_route or {}).get("compete_with", compete_with)

    if not changed:
        if passed is not None and branch in (
                "primary", "compete_with", "fallback"):
            # The demoted seat holds a position, but no gate-passing
            # seat exists to move it behind (owner ruling, Item 1 =
            # A): record the demotion honestly — no unqualified
            # promotion, no claimed reshuffle. Round-9 fix M4: the
            # fallback branch belongs here too — a fallback demotee
            # with zero passers DOES hold a route position, and the
            # old primary/compete-only condition let it fall through
            # to the false "holds no route position" line with no
            # ledger row at all.
            note = (
                f"route_change demote {demote} ({reason}) contract "
                f"{contract_id}: no seat that passed the deciding gate "
                f"to promote; route unchanged"
            )
            _record_route_ledger(store, demote, contract_id, note)
            return (
                f"Route for {category}: demoted {demote} — no seat that "
                f"passed the deciding gate to promote; route unchanged "
                f"— {reason} (contract {contract_id})"
            )
        if branch in ("primary", "compete_with", "fallback"):
            # The demoted seat holds a route position; the demotion
            # simply moved nothing (a primary with no successor
            # named, a fallback member already last). Say that —
            # not the position-less line below (round-9 fix M4).
            return (
                f"Route for {category}: no route change — {demote} holds "
                f"a route position and the demotion moved nothing "
                f"— {reason} (contract {contract_id})"
            )
        return (
            f"Route for {category}: no route change — {demote} holds no "
            f"route position to demote from — {reason} (contract {contract_id})"
        )

    note = f"route_change demote {demote} ({reason}) contract {contract_id}"
    if displaced_actual:
        # Round-9 fix M4: a seat displaced to the fallback by the
        # promotion is part of what happened; the record says so.
        note += f"; {displaced_actual} displaced to the fallback order"
    _record_route_ledger(store, demote, contract_id, note)

    if promoted_actual:
        line = (
            f"Route for {category}: demoted {demote}, promoted {promoted_actual} "
            f"(primary is now {new_primary})"
        )
    elif demote == new_compete:
        # The demotee landed on the compete seat (round-9 fix M4;
        # lane-4 F4 — the old template called every unpromoted
        # demotion a fallback move, even when a passed-mode rebuild
        # had placed the demotee ON the compete seat).
        line = (
            f"Route for {category}: demoted {demote} to the compete seat "
            f"(primary is now {new_primary})"
        )
    else:
        line = (
            f"Route for {category}: demoted {demote} within the fallback order "
            f"(primary is now {new_primary})"
        )
    if displaced_actual:
        line += f"; {displaced_actual} moved to the fallback order"
    return f"{line} — {reason} (contract {contract_id})"


def _record_route_ledger(store, demote, contract_id, note):
    """Write the router's internal ledger row for a route change."""
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
