"""Standing: the client book — lifetime measured spend, as tiers.

A store's standing is its *lifetime* record, the way a boutique's client
book is lifetime spend, not one visit: every token and every measured
dollar the ledger has ever recorded, summed. Tokens never convert to
dollars, and where a provider reported no cost the spend reads
"not reported" — the same discipline the ledger report and CPVP already
keep (reports print only database rows; nothing is estimated).

**The one definition of a counted row (tightening spec T1, hardened to
counted-row rule v2 by the round-4 fix spec, G8).** Every spend figure
in the plugin — this module's standing line and client book, the
verdict stamp, and :mod:`hybrid_critic_loop.chain`'s certificates —
comes from a single aggregation, :func:`ledger_totals`, shared by both
modules, so the surfaces can never disagree again (security F5,
correctness F6, claims #2). A ledger row is *counted* when:

- its outcome is a generation outcome (a dispatch: ``completed`` /
  ``refused`` / ``timeout`` / ``unavailable`` / ``error``), and
- its provenance is counted: not ``"internal"`` (the plugin's own
  bookkeeping rows), not ``"unattributed"`` (a writer that named no
  source), and not ``""`` — an empty label claims nothing, exactly
  like passing no label at all (round-3 attack F1: the tightening-era
  test was a denylist, so any invented string — or the empty string,
  which read as missing — counted), and
- it is *referentially backed* (round-3 attack F1/F2): either its
  provenance is ``"legacy-import"`` (rows written before provenance
  existed — a row dict with no provenance at all reads as
  ``"legacy-import"`` too) AND its seal came from a migration route
  — ``sealed_via`` is ``"backfill"`` (pre-chain books) or
  ``"upgrade"`` (the canonical-form cascade) — or its
  ``candidate_id`` names an existing candidate **on the row's own
  contract** whose stored provenance equals the row's provenance,
  or its provenance is ``"executor-reported"`` and such a candidate
  exists. The seal-route condition is round-6 fix spec J4
  (round-6 attack F2): the open-time migration labels ANY
  NULL-provenance row ``"legacy-import"`` and the sealing pass
  seals an unsealed suffix ``"repair"``, so keyed on the label
  alone the exemption laundered a raw post-hoc append into counted
  money with no hash math at all; a ``"repair"``-sealed
  legacy-import row now counts only via the ordinary referential
  leg, like any other label. A counted
  label naming a candidate that was never recorded — or one recorded
  under a different contract (round-5 attack F4) — is a claim the
  store's own graph does not back, and it does not count. Where the
  store cannot name its candidates at all (degraded stores), the leg
  passes unevaluated and the totals carry
  ``referential_unchecked=True``; the standing line/report then
  append "· backing unverified" rather than silently trusting
  labels (round-5 upgrade F1).

Rows the totals do not count are never dropped silently:
:func:`compute_standing` reports them as excluded totals split by
cause — provenance-excluded, outcome-excluded, and
referential-excluded — each with its amount and row count.

**Chain gate (round-4 fix spec G8; round-3 attack F2).** The standing
surfaces (:func:`standing_line`, :func:`standing_report`, and through
them the session hook's line) check the ledger's hash chain first: on
a broken chain they render "standing unavailable — ledger chain
broken at row N" instead of a tier. A book that cannot prove its own
rows does not get to state a lifetime spend. (Certificates verify
their own prefix in chain.py; this gate is the standing surfaces'
half of the same discipline.) Round 5 (fix spec H2/H3; attack F2,
upgrade F1) extends the same gate to the verdict report (reports.py —
the surface that carries the stamp), maps a ``(False, None)`` result
— the hashless-rows break of a degraded book — to the unavailable
line with the row shown as "—", and splits the check by surface:
operator surfaces pay the full walk, while the hook's line uses the
cached gate (``chain.verify_chain_cached``), which re-verifies only
rows past the last verified head.

Sums are reported-only: a field no provider reported stays out of the
sum, and a total with no reported values at all is None ("not
reported"), never an estimate. Aggregated spend is rounded to cents
before any tier or gap math runs on it (tightening spec T8;
correctness F4).

Aggregation runs in SQL against the store's connection when the store
exposes one (the real Store does): the counted-row rule, referential
leg included, is a conditional-sum query joined to the candidates
table, so the standing / certificate / hook paths never materialize
the whole book as dicts (round-4 fix spec G3) — and the expert
summary the surfaces merge in is aggregated in SQL too, with
``Store.ledger_summary``'s exact return shape (round-5 fix spec H3).
Stores without a connection (duck-typed fakes) take the same rules in
Python over ``ledger_rows()``; a store that can neither be queried
nor name its candidates through ``get_candidate`` cannot be
referentially checked, and there the referential leg passes — the
seam is named here rather than hidden (and disclosed on the line
itself, above), and the real Store never takes it.
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

#: Provenances the totals never count (tightening spec T2, hardened
#: by counted-row rule v2): ``"internal"`` is the plugin's own
#: bookkeeping, ``"unattributed"`` a writer that named no source, and
#: ``""`` a label that claims nothing (round-3 attack F1).
_NON_COUNTED_PROVENANCES = frozenset({"internal", "unattributed", ""})

#: The provenance a row with none recorded reads as: rows written
#: before the provenance column existed are imports of the old book
#: and count (the state migration labels them exactly this).
_MISSING_PROVENANCE = "legacy-import"

#: The seal routes that mark a row as genuinely migration-era
#: (round-6 fix spec J4): "backfill" (the pre-chain sealing pass)
#: and "upgrade" (the canonical-form cascade). The legacy-import
#: exemption requires one of these — a "repair"-sealed
#: legacy-import row is a post-hoc append the open-time machinery
#: labelled and sealed, not a row the migration carried over, and
#: it counts only via the ordinary referential leg.
_MIGRATION_SEALS = frozenset({"backfill", "upgrade"})

#: The provenance the dispatch path stamps when the candidate carries
#: none of its own (loop.py): counted when the named candidate exists.
_EXECUTOR_REPORTED = "executor-reported"

#: Exclusion causes, in the order the counted-row rule tests them.
_CAUSE_OUTCOME = "outcome"
_CAUSE_PROVENANCE = "provenance"
_CAUSE_REFERENTIAL = "referential"
_CAUSES = (_CAUSE_PROVENANCE, _CAUSE_OUTCOME, _CAUSE_REFERENTIAL)

#: What the standing surfaces render instead of a tier when the
#: ledger's hash chain does not verify (round-4 fix spec G8).
_CHAIN_UNAVAILABLE = (
    "Millionaire Matrix standing unavailable — "
    "ledger chain broken at row {row}"
)

#: The unavailable line's older-form variant (round-7 fix spec K9;
#: round-7 upgrade-walk F1): the first bad row's stored hash DOES
#: recompute — under a historical canonical form. That book is not
#: corrupt; it is sealed under an older form the open-time probe
#: did not see (its spot checks cover the head and tail rows, so a
#: historical row at an interior position strands un-upgraded and
#: was reported as "broken"). The line names the state the row is
#: actually in, distinct from a row matching no recognised form.
_CHAIN_OLDER_FORM = (
    "Millionaire Matrix standing unavailable — "
    "ledger sealed under an older form at row {row}"
)

#: The ledger-derived fields of a per-expert row — the figures the
#: unified totals own (round-4 fix spec G8, contest F8). Everything
#: else on an expert row (wins, losses, validity, calibration) is the
#: experts table's own record and still comes from ledger_summary.
_DISPATCH_FIELDS = (
    "dispatches", "completed", "refused", "timeout", "unavailable",
    "error", "tokens_in", "tokens_out", "cost_usd",
)

_EMPTY_DISPATCH = {
    "dispatches": 0, "completed": 0, "refused": 0, "timeout": 0,
    "unavailable": 0, "error": 0, "tokens_in": None, "tokens_out": None,
    "cost_usd": None,
}


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
    """A row's provenance label.

    No label recorded at all (missing key, or None) reads as
    ``"legacy-import"``: the row predates attribution. An explicit
    empty string is a label that claims nothing and stays ``""`` —
    it is NOT read as missing (round-3 attack F1). A non-string label
    is not a label anyone can be counted under either, and also
    reads as ``""``.
    """
    prov = row.get("provenance")
    if prov is None:
        return _MISSING_PROVENANCE
    if isinstance(prov, str):
        return prov
    return ""


def _candidate_resolver(store):
    """Resolve candidate existence + stored provenance for the totals.

    Returns a callable ``candidate_id -> (exists, stored_provenance,
    contract_id)``, or None when the store can neither be queried in
    SQL nor name its candidates through ``get_candidate`` (see the
    module docstring for that seam). The SQL form reads the candidates
    table in one query; the ``get_candidate`` form caches per
    candidate id. The contract id rides along because the counted
    rule's referential leg requires the candidate to belong to the
    row's own contract (round-5 fix spec H2; round-5 attack F4: a
    candidate on contract A used to back rows written under contract
    B).
    """
    conn = getattr(store, "_conn", None)
    if conn is not None:
        try:
            pairs = conn.execute(
                "SELECT id, provenance, contract_id FROM candidates"
            ).fetchall()
        except Exception:
            pairs = None
        if pairs is not None:
            table = {row[0]: (row[1], row[2]) for row in pairs}

            def resolve_sql(cid):
                stored, contract_id = table.get(cid, (None, None))
                return (cid in table, stored, contract_id)

            return resolve_sql
    get_candidate = getattr(store, "get_candidate", None)
    if callable(get_candidate):
        cache = {}

        def resolve(cid):
            if cid not in cache:
                try:
                    candidate = get_candidate(cid)
                except Exception:
                    candidate = None
                if isinstance(candidate, dict):
                    cache[cid] = (
                        True, candidate.get("provenance"),
                        candidate.get("contract_id"))
                else:
                    cache[cid] = (False, None, None)
            return cache[cid]

        return resolve
    return None


def _classify_row(row, resolve):
    """The counted-row rule v2 for one row dict.

    Returns ``"counted"`` or the cause that excludes the row:
    ``"outcome"`` (not a dispatch), ``"provenance"`` (a label the
    totals never count), or ``"referential"`` (a counted label the
    store's candidate graph does not back).
    """
    if row.get("outcome") not in GENERATION_OUTCOMES:
        return _CAUSE_OUTCOME
    prov = _row_provenance(row)
    if prov in _NON_COUNTED_PROVENANCES:
        return _CAUSE_PROVENANCE
    if prov == _MISSING_PROVENANCE and (
            row.get("sealed_via") in _MIGRATION_SEALS):
        # The legacy exemption (round-6 fix spec J4; round-6 attack
        # F2): only a migration route's seal exempts a legacy-import
        # row from the referential leg below. Keyed on the label
        # alone it also exempted the raw NULL-provenance append the
        # open-time migration labels and the suffix pass seals
        # "repair" — indistinguishable in the totals from a genuine
        # pre-provenance row, with no hash math required of the
        # writer. A repair-sealed (or unsealed) legacy-import row
        # falls through to the ordinary leg.
        return "counted"
    if resolve is None:
        # The store cannot name its candidates at all, so the
        # referential leg cannot be checked (module docstring);
        # ledger_totals flags the result referential_unchecked.
        return "counted"
    exists, stored, cand_contract = resolve(row.get("candidate_id"))
    if exists and cand_contract == row.get("contract_id") and (
            stored == prov or prov == _EXECUTOR_REPORTED):
        return "counted"
    return _CAUSE_REFERENTIAL


# ---------------------------------------------------------------------------
# The one aggregation
# ---------------------------------------------------------------------------

#: The counted-row rule v2 as a SQL CASE over a ledger row ``l`` joined
#: to its candidate ``c`` — the same classification :func:`_classify_row`
#: performs in Python, so the SQL and dict paths cannot drift.
_CLASSIFY_SQL = """
CASE
  WHEN l.outcome NOT IN ({outcomes}) THEN 'outcome'
  WHEN COALESCE(l.provenance, 'legacy-import')
       IN ('internal', 'unattributed', '') THEN 'provenance'
  WHEN COALESCE(l.provenance, 'legacy-import') = 'legacy-import'
       AND l.sealed_via IN ('backfill', 'upgrade')
    THEN 'counted'
  WHEN c.id IS NOT NULL
       AND c.contract_id = l.contract_id
       AND (c.provenance = COALESCE(l.provenance, 'legacy-import')
            OR COALESCE(l.provenance, 'legacy-import')
               = 'executor-reported')
    THEN 'counted'
  ELSE 'referential'
END
""".format(
    outcomes=", ".join(f"'{name}'" for name in GENERATION_OUTCOMES))


def _empty_class():
    return {"n": 0, "tin": None, "tout": None, "cost": None, "cost_n": 0}


def _assemble_totals(by_class):
    """Shape per-class aggregates into the ledger_totals result dict."""
    counted = by_class.get("counted") or _empty_class()
    tokens_in, tokens_out = counted["tin"], counted["tout"]
    if tokens_in is None and tokens_out is None:
        tokens = None
    else:
        tokens = (tokens_in or 0) + (tokens_out or 0)
    result = {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens": tokens,
        "cost_usd": _round_cents(counted["cost"]),
        "cost_rows": counted["cost_n"],
        "cost_missing": counted["n"] - counted["cost_n"],
    }
    excluded_cost = None
    excluded_rows = 0
    for cause in _CAUSES:
        entry = by_class.get(cause) or _empty_class()
        result[f"excluded_{cause}_cost_usd"] = _round_cents(entry["cost"])
        result[f"excluded_{cause}_rows"] = entry["cost_n"]
        if entry["cost"] is not None:
            excluded_cost = (excluded_cost or 0) + entry["cost"]
        excluded_rows += entry["cost_n"]
    result["excluded_cost_usd"] = _round_cents(excluded_cost)
    result["excluded_rows"] = excluded_rows
    return result


def _totals_sql(conn, contract_id, upto_id):
    """ledger_totals over the store's connection, or None on failure."""
    where = ""
    params = []
    if contract_id is not None:
        where += " AND l.contract_id = ?"
        params.append(contract_id)
    if upto_id is not None:
        where += " AND l.id <= ?"
        params.append(upto_id)
    sql = (
        "SELECT cls, COUNT(*) AS n, SUM(tokens_in) AS tin,"
        " SUM(tokens_out) AS tout, SUM(cost_usd) AS cost,"
        " COUNT(cost_usd) AS cost_n"
        f" FROM (SELECT {_CLASSIFY_SQL} AS cls, l.tokens_in AS tokens_in,"
        " l.tokens_out AS tokens_out, l.cost_usd AS cost_usd"
        " FROM ledger l"
        " LEFT JOIN candidates c ON c.id = l.candidate_id"
        f" WHERE 1=1{where}) GROUP BY cls"
    )
    try:
        fetched = conn.execute(sql, params).fetchall()
    except Exception:
        return None
    by_class = {}
    for row in fetched:
        by_class[row[0]] = {
            "n": row[1], "tin": row[2], "tout": row[3],
            "cost": row[4], "cost_n": row[5],
        }
    return _assemble_totals(by_class)


def _ledger_rows(store):
    try:
        rows = store.ledger_rows()
    except TypeError:
        rows = store.ledger_rows(None)
    return rows or []


def _totals_python(store, contract_id, upto_id):
    resolve = _candidate_resolver(store)
    classes = {}
    for row in _ledger_rows(store):
        if not isinstance(row, dict):
            continue
        if contract_id is not None and row.get("contract_id") != contract_id:
            continue
        if upto_id is not None:
            rid = row.get("id")
            if not _is_number(rid) or rid > upto_id:
                continue
        cls = _classify_row(row, resolve)
        entry = classes.setdefault(
            cls, {"tin": [], "tout": [], "cost": [], "n": 0, "cost_n": 0})
        entry["n"] += 1
        if cls == "counted":
            if _is_number(row.get("tokens_in")):
                entry["tin"].append(row["tokens_in"])
            if _is_number(row.get("tokens_out")):
                entry["tout"].append(row["tokens_out"])
        if _is_number(row.get("cost_usd")):
            entry["cost"].append(row["cost_usd"])
            entry["cost_n"] += 1
    by_class = {}
    for cls, entry in classes.items():
        by_class[cls] = {
            "n": entry["n"],
            "tin": _sum_or_none(entry["tin"]),
            "tout": _sum_or_none(entry["tout"]),
            "cost": _sum_or_none(entry["cost"]),
            "cost_n": entry["cost_n"],
        }
    result = _assemble_totals(by_class)
    # Degraded-mode honesty (round-5 fix spec H2; round-5 upgrade
    # F1): when the store cannot name its candidates at all, the
    # referential leg passed unchecked — the totals say so, and the
    # standing surfaces disclose it ("· backing unverified") instead
    # of silently trusting labels. The SQL path always evaluates the
    # leg, so it never sets this.
    result["referential_unchecked"] = resolve is None
    return result


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
    do not count, split by cause as ``excluded_provenance_*``,
    ``excluded_outcome_*`` and ``excluded_referential_*`` (amount and
    cost-carrying row count each), so excluded money is always named
    with its reason, never vanished. ``referential_unchecked`` is
    True only when the store could not name its candidates at all,
    so the referential leg passed unevaluated (degraded stores; the
    real Store always evaluates it).
    """
    conn = getattr(store, "_conn", None)
    if conn is not None:
        result = _totals_sql(conn, contract_id, upto_id)
        if result is not None:
            result["referential_unchecked"] = False
            return result
    return _totals_python(store, contract_id, upto_id)


# ---------------------------------------------------------------------------
# Per-expert figures from the same rule
# ---------------------------------------------------------------------------

def _expert_totals_sql(conn):
    sql = (
        "SELECT expert_id, COUNT(*) AS dispatches,"
        " SUM(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN outcome = 'refused' THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN outcome = 'timeout' THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN outcome = 'unavailable' THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN outcome = 'error' THEN 1 ELSE 0 END),"
        " SUM(tokens_in), SUM(tokens_out), SUM(cost_usd)"
        f" FROM (SELECT {_CLASSIFY_SQL} AS cls, l.id AS id,"
        " l.expert_id AS expert_id, l.outcome AS outcome,"
        " l.tokens_in AS tokens_in, l.tokens_out AS tokens_out,"
        " l.cost_usd AS cost_usd FROM ledger l"
        " LEFT JOIN candidates c ON c.id = l.candidate_id)"
        " WHERE cls = 'counted'"
        " GROUP BY expert_id ORDER BY MIN(id)"
    )
    try:
        fetched = conn.execute(sql).fetchall()
    except Exception:
        return None
    return [
        {
            "expert_id": row[0], "dispatches": row[1],
            "completed": row[2], "refused": row[3], "timeout": row[4],
            "unavailable": row[5], "error": row[6],
            "tokens_in": row[7], "tokens_out": row[8], "cost_usd": row[9],
        }
        for row in fetched
    ]


def _expert_totals_python(store):
    resolve = _candidate_resolver(store)
    if resolve is None:
        return None
    order = []
    buckets = {}
    for row in _ledger_rows(store):
        if not isinstance(row, dict):
            continue
        if _classify_row(row, resolve) != "counted":
            continue
        eid = row.get("expert_id")
        if eid not in buckets:
            order.append(eid)
            buckets[eid] = {
                "expert_id": eid, "dispatches": 0, "completed": 0,
                "refused": 0, "timeout": 0, "unavailable": 0, "error": 0,
                "_tin": [], "_tout": [], "_cost": [],
            }
        entry = buckets[eid]
        entry["dispatches"] += 1
        entry[row["outcome"]] += 1
        entry["_tin"].append(row.get("tokens_in"))
        entry["_tout"].append(row.get("tokens_out"))
        entry["_cost"].append(row.get("cost_usd"))
    result = []
    for eid in order:
        entry = buckets[eid]
        result.append({
            "expert_id": eid,
            "dispatches": entry["dispatches"],
            "completed": entry["completed"],
            "refused": entry["refused"],
            "timeout": entry["timeout"],
            "unavailable": entry["unavailable"],
            "error": entry["error"],
            "tokens_in": _sum_or_none(entry["_tin"]),
            "tokens_out": _sum_or_none(entry["_tout"]),
            "cost_usd": _sum_or_none(entry["_cost"]),
        })
    return result


def expert_totals(store):
    """Per-expert dispatch figures from the one aggregation.

    One dict per expert with at least one *counted* row: dispatch and
    per-outcome counts plus reported-only token/cost sums — the same
    figures :func:`ledger_totals` produces for the whole book, grouped
    by expert (round-4 fix spec G8, contest F8: the per-expert numbers
    used to come from the provenance-blind ``ledger_summary``, so the
    reports and the standing disagreed about the same book). Returns
    None when the store cannot support the unified figures at all (no
    connection, no ``get_candidate``) — callers then fall back to the
    summary's own rows rather than inventing zeros.
    """
    conn = getattr(store, "_conn", None)
    if conn is not None:
        result = _expert_totals_sql(conn)
        if result is not None:
            return result
    return _expert_totals_python(store)


def _cpvp(points, cost):
    """cost-per-validity-point, mirroring state._cost_per_validity_point."""
    if cost is not None and points not in (None, 0):
        return cost / points
    return None


def _summary_sql(conn):
    """``Store.ledger_summary``'s return shape, aggregated in SQL.

    The summary the standing/report surfaces consume (round-5 fix
    spec H3; round-5 performance: the store's own ledger_summary
    materializes the whole book in Python, and compute_standing paid
    that on every call). Per expert with at least one generation
    row, in first-dispatch order: dispatch and per-outcome counts
    over ALL generation rows (the summary is provenance-blind by
    design — the counted figures replace them downstream in
    :func:`expert_rows`), reported-only token/cost sums (SQL SUM
    skips NULLs and yields None when nothing was reported, exactly
    the Python rule), and the experts table's own fields (wins,
    losses, validity, cpvp) merged in. Returns None on any SQL
    failure — an older schema without the validity columns, say —
    and the caller falls back to the store's own method.
    """
    outcomes = ", ".join(f"'{name}'" for name in GENERATION_OUTCOMES)
    try:
        agg = conn.execute(
            "SELECT expert_id, COUNT(*),"
            " SUM(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN outcome = 'refused' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN outcome = 'timeout' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN outcome = 'unavailable' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN outcome = 'error' THEN 1 ELSE 0 END),"
            " SUM(tokens_in), SUM(tokens_out), SUM(cost_usd)"
            f" FROM ledger WHERE outcome IN ({outcomes})"
            " GROUP BY expert_id ORDER BY MIN(id)"
        ).fetchall()
        experts = {}
        for row in conn.execute(
                "SELECT id, wins, losses, validity_points,"
                " validity_cost_usd FROM experts").fetchall():
            experts[row[0]] = row[1:]
    except Exception:
        return None
    summary = []
    for row in agg:
        wins, losses, v_points, v_cost = experts.get(
            row[0], (0, 0, None, None))
        total = wins + losses
        summary.append({
            "expert_id": row[0],
            "dispatches": row[1],
            "completed": row[2],
            "refused": row[3],
            "timeout": row[4],
            "unavailable": row[5],
            "error": row[6],
            "tokens_in": row[7],
            "tokens_out": row[8],
            "cost_usd": row[9],
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / total) if total else None,
            "validity_points": v_points,
            "validity_cost_usd": v_cost,
            "cost_per_validity_point": _cpvp(v_points, v_cost),
        })
    return summary


def _summary_rows(store):
    # SQL first when the store exposes a connection (round-5 fix
    # spec H3): compute_standing must not materialize the whole book
    # on a real Store. Duck stores keep the store's own rows path.
    conn = getattr(store, "_conn", None)
    if conn is not None:
        result = _summary_sql(conn)
        if result is not None:
            return result
    try:
        summary = store.ledger_summary()
    except TypeError:
        summary = store.ledger_summary(None)
    return summary or []


def expert_rows(store, summary=None):
    """Per-expert report rows: the summary's rows, unified figures.

    The summary (``store.ledger_summary``) still owns the experts
    table's own fields — wins, losses, win rate, validity, calibration.
    The ledger-derived dispatch figures on each row are replaced by
    :func:`expert_totals`' counted-row figures, so every surface
    prints one book's numbers (round-4 fix spec G8). An expert the
    summary knows but who has no counted rows reads zero dispatches —
    its recorded rows are all in the excluded totals, where the
    standing report names them. When the store cannot support the
    unified figures (:func:`expert_totals` returns None), the
    summary's rows pass through unchanged.
    """
    if summary is None:
        summary = _summary_rows(store)
    rows = [dict(row) for row in summary]
    unified = expert_totals(store)
    if unified is None:
        return rows
    by_id = {entry["expert_id"]: entry for entry in unified}
    seen = set()
    for row in rows:
        eid = row.get("expert_id")
        entry = by_id.get(eid)
        if entry is not None:
            seen.add(eid)
            for key in _DISPATCH_FIELDS:
                row[key] = entry[key]
        else:
            for key in _DISPATCH_FIELDS:
                row[key] = _EMPTY_DISPATCH[key]
    for entry in unified:
        if entry["expert_id"] not in seen:
            rows.append({
                "wins": 0, "losses": 0, "win_rate": None,
                "validity_points": None, "validity_cost_usd": None,
                "cost_per_validity_point": None,
                **entry,
            })
    return rows


# ---------------------------------------------------------------------------
# Standing surfaces
# ---------------------------------------------------------------------------

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
    not count, in aggregate and split by cause. Plus one row per
    dispatched expert: the ledger summary's own fields with the
    dispatch figures replaced by the unified counted-row figures
    (:func:`expert_rows`), plus that expert's validity tier, taken from
    the expert's measured validity cost where present.
    """
    totals = ledger_totals(store)
    experts = []
    for row in expert_rows(store):
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
        "excluded_provenance_cost_usd": totals[
            "excluded_provenance_cost_usd"],
        "excluded_provenance_rows": totals["excluded_provenance_rows"],
        "excluded_outcome_cost_usd": totals["excluded_outcome_cost_usd"],
        "excluded_outcome_rows": totals["excluded_outcome_rows"],
        "excluded_referential_cost_usd": totals[
            "excluded_referential_cost_usd"],
        "excluded_referential_rows": totals[
            "excluded_referential_rows"],
        "referential_unchecked": bool(
            totals.get("referential_unchecked")),
        "experts": experts,
    }


def has_standing(store) -> bool:
    """True when the store has any dispatch on record to take standing from.

    An existence probe, not a standing computation (round-4 fix spec
    G3): one indexed query on the store's connection where it has one,
    so the session hook can ask on every first turn without paying for
    the full book. Any generation-outcome row is a dispatch on record,
    counted or not — the same set the experts list is built from.
    """
    conn = getattr(store, "_conn", None)
    if conn is not None:
        try:
            placeholders = ", ".join("?" for _ in GENERATION_OUTCOMES)
            row = conn.execute(
                "SELECT 1 FROM ledger WHERE outcome IN"
                f" ({placeholders}) LIMIT 1",
                GENERATION_OUTCOMES,
            ).fetchone()
            return row is not None
        except Exception:
            pass
    # Fallback leg (round-5 fix spec H2; round-5 upgrade F1): a
    # store with no connection — the degraded fallback store — answers
    # existence over its own rows: any generation-outcome row is a
    # dispatch on record. The previous leg went through expert_rows /
    # expert_totals, which return None exactly for such stores, so
    # the probe answered False over a store WITH dispatches and the
    # session hook silently dropped the standing line.
    try:
        for row in _ledger_rows(store):
            if isinstance(row, dict) and (
                    row.get("outcome") in GENERATION_OUTCOMES):
                return True
    except Exception:
        pass
    return False


def _chain_check(store, cached=False):
    """(status, bad_row) for the store's ledger chain.

    ``status`` is ``"broken"`` only on a definitive failure from
    chain.py — including ``(False, None)``, the hashless-rows break
    a degraded store produces (round-5 fix spec H2; round-5 upgrade
    F1): a book that cannot verify at all gates exactly like any
    other break, with the row rendered as "—". ``"unknown"`` when
    the check itself cannot run (the store's rows cannot be walked
    at all); surfaces never gate on uncertainty. With
    ``cached=True`` the hook's cheaper gate stands in for the full
    walk — ``chain.verify_chain_cached`` (round-5 pinned interface:
    verify only rows past the last verified head, falling back to
    the full walk when no cache holds); operator surfaces always
    pass ``cached=False`` and pay the full walk. Lazy import:
    chain.py imports this module.
    """
    try:
        from . import chain as _chain

        verify = _chain.verify_chain
        if cached:
            verify = getattr(_chain, "verify_chain_cached", None) or verify
        ok, bad = verify(store)
    except Exception:
        return "unknown", None
    return ("ok", None) if ok else ("broken", bad)


def _ledger_row_by_id(store, row_id):
    """One ledger row as a dict, or None — SQL through the store's
    connection when it exposes one, else the rows walk (the same
    source discipline as chain.py's head snapshot)."""
    conn = getattr(store, "_conn", None)
    if conn is not None:
        try:
            raw = conn.execute(
                "SELECT * FROM ledger WHERE id=?", (row_id,)).fetchone()
            if raw is None:
                return None
            return {key: raw[key] for key in raw.keys()}
        except Exception:  # noqa: BLE001 - fall back to the rows walk
            pass
    try:
        for row in _ledger_rows(store):
            if isinstance(row, dict) and row.get("id") == row_id:
                return row
    except Exception:  # noqa: BLE001 - no source at all
        pass
    return None


def _bad_row_older_form(store, bad):
    """True when the walk's first bad row is sealed under a
    HISTORICAL canonical form (round-7 fix spec K9): its stored
    ``row_hash`` recomputes under a recognised historical variant
    (``pre_sealed_via`` or ``legacy`` — :func:`chain.row_hash_variants`)
    but NOT under the current form. A row whose stored hash matches
    the current form recomputes fine and failed the walk on its
    link, and a row matching no variant is tampered or unsealed —
    both are genuinely "broken", never "older form". Any failure to
    determine the form answers False: wording must never break the
    line it decorates."""
    if bad is None or store is None:
        return False
    row = _ledger_row_by_id(store, bad)
    if not isinstance(row, dict):
        return False
    stored = row.get("row_hash")
    if not stored:
        return False
    try:
        from . import chain as _chain

        variants = _chain.row_hash_variants(row)
    except Exception:  # noqa: BLE001 - undeterminable is "broken"
        return False
    if stored == variants.get("current"):
        return False
    return stored in (
        variants.get("pre_sealed_via"), variants.get("legacy"))


def _unavailable_line(bad, store=None):
    """The unavailable line for a broken chain (bad row id or None).

    With the store in hand, a first bad row sealed under a
    historical canonical form renders the older-form line instead
    (round-7 fix spec K9; round-7 upgrade-walk F1) — the state is
    named, not misdiagnosed as corruption. Callers without a store
    (reports.py's imports of this helper) keep the plain broken
    wording exactly as before.
    """
    if _bad_row_older_form(store, bad):
        return _CHAIN_OLDER_FORM.format(row=bad)
    return _CHAIN_UNAVAILABLE.format(row=bad if bad is not None else "—")


def chain_state(store):
    """Public chain state for reports: (status, bad_row), full walk.

    ``status`` is ``"ok"`` / ``"broken"`` / ``"unknown"`` — see
    :func:`_chain_check`. reports.ledger_report prints its
    chain-state line from this (round-5 fix spec H2).
    """
    return _chain_check(store)


def chain_unavailable_line(store):
    """The unavailable line when the chain definitively fails, else None.

    The verdict report's gate (round-5 fix spec H2; round-5 attack
    F2): full walk, exactly like the standing surfaces.
    """
    status, bad = _chain_check(store)
    return _unavailable_line(bad, store) if status == "broken" else None


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
    line = (
        f"Millionaire Matrix standing: {standing.get('tier')} — "
        f"{spend_text} · {tokens_text}"
    )
    if standing.get("referential_unchecked"):
        # Degraded-mode honesty (round-5 fix spec H2; round-5 upgrade
        # F1): the referential leg could not be evaluated for this
        # store, so the tier is label-trusted — disclosed on the line
        # itself, never silently. The real Store always evaluates it.
        line += " · backing unverified"
    return line


def _headline_standing(store):
    """The standing line's figures only: tier + lifetime totals.

    Round-6 fix spec J6 (round-6 performance F1): the warm hook was
    still ~1.3 s at 200k rows AFTER the cached chain check left it,
    and the residue was :func:`compute_standing`'s per-expert
    machinery — the summary aggregation and the expert totals, two
    more whole-book GROUP BYs — computed for a one-line standing
    that prints none of it. The line needs only the headline
    totals, so it takes them straight from the one aggregation;
    the keys are exactly the ones :func:`format_standing_line`
    reads. :func:`standing_report` keeps the full computation.
    """
    totals = ledger_totals(store)
    return {
        "tier": tier_for(totals["cost_usd"]),
        "cost_usd": totals["cost_usd"],
        "tokens": totals["tokens"],
        "cost_rows": totals["cost_rows"],
        "cost_missing": totals["cost_missing"],
        "referential_unchecked": bool(
            totals.get("referential_unchecked")),
    }


def standing_line(store, cached=False):
    """The one-line standing for a store (see :func:`format_standing_line`).

    Chain-gated (round-4 fix spec G8): a store whose ledger chain does
    not verify renders the unavailable line instead of a tier. The
    gate is the full walk, except with ``cached=True`` — the session
    hook's line (round-5 fix spec H3, pinned cache split): the cached
    gate re-verifies only rows past the last verified head instead of
    re-walking the whole book on every session's first turn. Operator
    surfaces never pass cached=True.

    The figures come from the light totals path
    (:func:`_headline_standing`; round-6 fix spec J6) — the line
    never computes the per-expert aggregations it does not print.
    """
    status, bad = _chain_check(store, cached=cached)
    if status == "broken":
        return _unavailable_line(bad, store)
    return format_standing_line(_headline_standing(store))


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


#: (standing keys for amount, keys for row count, report label) — the
#: excluded totals, one line per cause (round-4 fix spec G8).
_EXCLUDED_CAUSES = (
    ("excluded_provenance_cost_usd", "excluded_provenance_rows",
     "unattributed/internal writers"),
    ("excluded_outcome_cost_usd", "excluded_outcome_rows",
     "non-dispatch outcomes"),
    ("excluded_referential_cost_usd", "excluded_referential_rows",
     "referential-excluded (no matching candidate on record)"),
)


def standing_report(store) -> str:
    """The full client book for the local store, as text."""
    from .reports import (
        CPVP_DEFINITION,
        _calibration_text,
        _cpvp_text,
        _value_or_not_reported,
        _win_rate_text,
    )

    lines = ["standing — the client book:"]
    status, bad = _chain_check(store)
    if status == "broken":
        # Chain-gated (round-4 fix spec G8): no tier, no totals, no
        # expert figures from a book whose rows do not verify.
        lines.append(_unavailable_line(bad, store))
        return "\n".join(lines)
    standing = compute_standing(store)
    lines.append(format_standing_line(standing))
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
    for cost_key, rows_key, label in _EXCLUDED_CAUSES:
        # Money the book carries but the totals do not count is named
        # with its cause, never dropped silently (tightening spec T2;
        # round-3 attack F1/F2; round-4 fix spec G8).
        excluded_cost = standing.get(cost_key)
        excluded_rows = standing.get(rows_key) or 0
        if excluded_rows and _is_number(excluded_cost):
            noun = "row" if excluded_rows == 1 else "rows"
            lines.append(
                f"{format_money(excluded_cost)} across {excluded_rows} "
                f"{noun} not counted — {label}"
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
    lines.append(CPVP_DEFINITION)
    return "\n".join(lines)
