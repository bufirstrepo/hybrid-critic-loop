"""Hash-chained ledger and per-run certificates (Unit B, anti-fake core).

The ledger is the plugin's book: every dispatch, token count and measured
dollar lives there. This module seals it and signs what it says.

**The chain.** Every ledger row carries ``prev_hash`` / ``row_hash``:
``row_hash = sha256(prev_hash + canonical(row))`` where ``canonical`` is a
stable JSON serialization of the row's own fields and the first row's
``prev_hash`` is :data:`GENESIS_HASH`. Rewriting any stored row — inflating
a token count, inventing spend — breaks every recomputation downstream of
it, and :func:`verify_chain` names the first row that no longer seals.
With ``upto_id``, verification covers only the chain prefix up to that
row — the high-water mark a certificate seals (tightening spec T4).

**Full walk vs cached check — the split, stated plainly.** Two
verification strengths exist, and they are NOT interchangeable.
:func:`verify_chain` is the full walk: every row in scope is
recomputed from genesis, and a success refreshes the store's
chain-verified head cache (``get_chain_verified_head`` /
``set_chain_verified_head``). Operator surfaces — the standing
report, the verdict report, the CLI — use the full walk.
:func:`verify_chain_cached` is the hook-path gate check: it trusts
that the prefix up to the cached head was verified when the cache
was written, spot-checks that the head row still recomputes and
links, and verifies only the rows AFTER the head. A tamper inside
the cached prefix that leaves the head row untouched is therefore
invisible to the cached check — by design, and only the full walk
catches it. The session hook pays for the cached check on every
first turn; anything an operator acts on pays for the walk
(round-5 fix spec, pinned interfaces; round-5 performance HIGH).
Round 6 (fix spec J3) closes the invalidation half of the split: a
FAILED full walk deletes the cache, so once any operator surface
has proven the book broken, the cached check stops passing it —
cached surfaces fall back to the full walk until a success
re-seeds the cache.

**The certificate.** A run that reached a verdict with a winner is issued
a certificate: the ``MM CERTIFIED`` stamp plus a signed token
``mm1.<base64url(payload)>.<base64url(sig)>``. The payload names the
contract, the verdict, the run's totals *as recorded in the ledger* at
issuance, the operator's tier at issue time, the run's cost coverage
(``cost_rows`` / ``cost_missing``), the verdict's gate outcome
(``measured``, and ``waived`` when the human gate waived a measured
failure through), the id of the store key that signed it (``kid``),
and the chain head at the run's high-water mark
(``ledger_upto`` — the contract's last ledger row at issuance, so later
bookkeeping rows never invalidate the seal). ``sig`` is HMAC-SHA256 over
the payload with the store's LOCAL key. All totals come from the one
aggregation in :mod:`hybrid_critic_loop.standing`, so a certificate can
never disagree with the standing line or the client book.

**What the local signature does and does not prove — read this before
believing a certificate.** The local key (``cert_key`` in the plugin data
directory, generated once, file perms 600) proves only that the certificate
was issued by *this store* and has not been altered since: anyone holding
the store's files holds the key, so the signature is a store-integrity
seal, not a public proof. Making a *public* claim checkable — the hosted
verification service, live receipts streamed as runs finish, and audit of
provider records for the top tiers — is the designed next step and is NOT
built here. No code or document in this build may claim more than the
paragraph above.

Stdlib only. No network. No telemetry.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import tempfile
import time
from pathlib import Path

from .standing import TIERS as _STANDING_TIERS
from .standing import ledger_totals as _ledger_totals
from .standing import tier_for

__all__ = [
    "GENESIS_HASH",
    "LEDGER_FIELDS",
    "LEDGER_FIELDS_PRE_SEALED_VIA",
    "LEDGER_FIELDS_LEGACY",
    "CertificateKeyError",
    "canonical_row",
    "canonical_row_pre_sealed_via",
    "canonical_row_legacy",
    "row_hash",
    "row_hash_pre_sealed_via",
    "row_hash_legacy",
    "row_hash_variants",
    "verify_chain",
    "verify_chain_cached",
    "cert_key",
    "key_id",
    "run_totals",
    "lifetime_spend",
    "stamp_text",
    "issue_certificate",
    "verify_certificate",
]

#: The prev_hash of the first ledger row in any store.
GENESIS_HASH = "0" * 64

#: The ledger row's own fields, in canonical order. The two hash columns
#: themselves are never part of the serialization (a row cannot seal
#: its own seal). ``provenance`` joined the form in the tightening pass
#: (T12): a row's attribution is sealed like every other field, so
#: flipping it breaks the chain at that row. ``sealed_via`` joined in
#: the round-4 fix pass (G2; round-3 attack F3): the mark that says HOW
#: a row was sealed is itself sealed, so relabelling a backfilled or
#: repaired row as an ordinary append breaks the chain at that row.
LEDGER_FIELDS = (
    "id",
    "target_id",
    "contract_id",
    "candidate_id",
    "stage",
    "expert_id",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "outcome",
    "note",
    "created_at",
    "provenance",
    "sealed_via",
)

#: The tightening-era canonical form — LEDGER_FIELDS with
#: ``provenance`` but without ``sealed_via``. Recognition only: the
#: open-time sealing pass treats a stored hash matching this form as a
#: recognised seal and re-seals the row under the current form (see
#: :func:`row_hash_variants`). New seals never use this form.
LEDGER_FIELDS_PRE_SEALED_VIA = tuple(
    field for field in LEDGER_FIELDS if field != "sealed_via"
)

#: The pre-tightening canonical form — no ``provenance``, no
#: ``sealed_via``. Kept for exactly one purpose: the open-time sealing
#: pass in state.py recognises a row sealed under an old form and
#: re-seals it under the current form (the canonical-form cascade,
#: marked ``sealed_via="upgrade"``), so a store sealed before a form
#: change keeps its chain. Its certificates do NOT survive the change:
#: re-sealing rewrites the chain head a certificate seals, so a
#: certificate issued before a canonical-form change stops verifying
#: and is re-issued from the store (round-4 fix spec G10d). New seals
#: never use an old form.
LEDGER_FIELDS_LEGACY = tuple(
    field for field in LEDGER_FIELDS
    if field not in ("provenance", "sealed_via")
)

_TOKEN_PREFIX = "mm1"

#: Tier names by rank (index 0 is the highest tier) — from standing's
#: ladder, the only copy (tightening spec T10; claims #10).
_TIER_RANK = {
    name: rank for rank, (name, _floor) in enumerate(_STANDING_TIERS)
}


class CertificateKeyError(Exception):
    """A file-backed store's certificate key cannot be read or persisted.

    Raised instead of falling back to an ephemeral key: a certificate
    signed under a key that will not be there at verification time
    verifies nowhere else and reads as forgery — worse than no
    certificate at all (tightening spec T5; correctness F8, host F1).
    """


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------

def _canonical(row, fields) -> str:
    body = {field: row.get(field) for field in fields}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def canonical_row(row) -> str:
    """Stable serialization of a ledger row's own fields.

    Fixed field set, sorted keys, compact separators — the same dict
    always serializes to the same string, so a hash computed at append
    time recomputes identically at verify time.
    """
    return _canonical(row, LEDGER_FIELDS)


def canonical_row_pre_sealed_via(row) -> str:
    """The tightening-era serialization (no ``sealed_via``).

    Recognition only — see :data:`LEDGER_FIELDS_PRE_SEALED_VIA`.
    """
    return _canonical(row, LEDGER_FIELDS_PRE_SEALED_VIA)


def canonical_row_legacy(row) -> str:
    """The pre-T12 canonical serialization (no ``provenance``).

    Recognition only — see :data:`LEDGER_FIELDS_LEGACY`.
    """
    return _canonical(row, LEDGER_FIELDS_LEGACY)


def _digest(prev_hash, canonical: str) -> str:
    data = (str(prev_hash) + canonical).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def row_hash(prev_hash, row) -> str:
    """``sha256(prev_hash + canonical(row))`` as a hex digest."""
    return _digest(prev_hash, canonical_row(row))


def row_hash_pre_sealed_via(prev_hash, row) -> str:
    """``row_hash`` under the tightening-era form (recognition only)."""
    return _digest(prev_hash, canonical_row_pre_sealed_via(row))


def row_hash_legacy(prev_hash, row) -> str:
    """``row_hash`` under the pre-T12 canonical form (recognition only)."""
    return _digest(prev_hash, canonical_row_legacy(row))


def row_hash_variants(row) -> dict:
    """The row's digest under every recognised canonical form.

    Computed over the row's OWN stored ``prev_hash`` link — the link
    the open-time sealing pass validates. Keys: ``"current"`` (the
    form new seals use), ``"pre_sealed_via"`` (the tightening-era
    form), ``"legacy"`` (the pre-tightening form). A stored
    ``row_hash`` matching ANY variant is a recognised seal; a match
    under a non-current variant is re-sealed under the current form by
    the pass's cascade (round-4 fix spec, pinned interfaces; G2).
    Variants can coincide — a row whose ``sealed_via`` is None hashes
    identically under the current and pre-sealed-via forms.
    """
    prev = row.get("prev_hash")
    return {
        "current": row_hash(prev, row),
        "pre_sealed_via": row_hash_pre_sealed_via(prev, row),
        "legacy": row_hash_legacy(prev, row),
    }


def _canonical_from_values(values) -> str:
    """``canonical_row`` for a row carried as raw values.

    ``values`` aligns with :data:`LEDGER_FIELDS`; the serialization
    must byte-match :func:`canonical_row` for the same row (round-5
    fix spec H3) — it builds the identical dict and dumps it with the
    identical parameters, which the equivalence tests pin byte for
    byte.
    """
    body = dict(zip(LEDGER_FIELDS, values))
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


#: The pinned raw-tuple layout of ``Store.ledger_rows_raw()``
#: (round-5 fix spec H3): the row's canonical fields in
#: :data:`LEDGER_FIELDS` order, then the seal plumbing appended.
_RAW_FIELDS = LEDGER_FIELDS + ("prev_hash", "row_hash")

#: Layout variants the raw path tolerates from duck-typed stores:
#: the pinned layout first, then ``sealed_via`` carried in the
#: appended seal group instead of its LEDGER_FIELDS slot, then the
#: ledger table's physical column order. The seal itself is the
#: layout oracle (see :func:`_detect_raw_layout`), so a variant is
#: only ever trusted after it has produced a sealed, linked row.
_RAW_LAYOUTS = (
    _RAW_FIELDS,
    tuple(f for f in LEDGER_FIELDS if f != "sealed_via")
    + ("prev_hash", "row_hash", "sealed_via"),
    (
        "id", "target_id", "contract_id", "candidate_id", "stage",
        "expert_id", "tokens_in", "tokens_out", "cost_usd", "outcome",
        "note", "created_at", "prev_hash", "row_hash", "provenance",
        "sealed_via",
    ),
)


def _detect_raw_layout(rows):
    """The raw-tuple layout that seals this book, or None.

    Under the true layout, the first hash-carrying row whose stored
    ``prev_hash`` links to its predecessor's stored hash (genesis for
    row one) must recompute to its stored ``row_hash``. A layout that
    produces no such row is not the layout; if NO layout does — an
    empty or hashless book, or a tamper at the head — None is
    returned and the caller falls back to the dict path, whose
    verdicts are the reference. A wrong layout cannot be confirmed
    short of a hash collision: the canonical bytes differ per layout,
    so only the true layout's reading seals.
    """
    for fields in _RAW_LAYOUTS:
        width = len(fields)
        if any(len(tup) != width for tup in rows):
            continue
        pos = {field: i for i, field in enumerate(fields)}
        prev_stored = GENESIS_HASH
        confirmed = False
        for tup in rows:
            stored_prev = tup[pos["prev_hash"]]
            stored_hash = tup[pos["row_hash"]]
            if stored_prev and stored_hash:
                if stored_prev == prev_stored:
                    values = tuple(tup[pos[f]] for f in LEDGER_FIELDS)
                    if _digest(
                        stored_prev, _canonical_from_values(values)
                    ) == stored_hash:
                        confirmed = True
                break  # the first hash-carrying row decides the layout
            prev_stored = stored_hash
        if confirmed:
            return fields
    return None


def _records_from_dicts(rows):
    """Walk records ``(id, prev_hash, row_hash, canonical)`` from
    dict rows — the reference source."""
    for row in rows:
        yield (
            row.get("id"),
            row.get("prev_hash"),
            row.get("row_hash"),
            canonical_row(row),
        )


def _records_from_tuples(rows, fields):
    """Walk records from raw tuples under a confirmed layout."""
    pos = {field: i for i, field in enumerate(fields)}
    for tup in rows:
        values = tuple(tup[pos[f]] for f in LEDGER_FIELDS)
        yield (
            tup[pos["id"]],
            tup[pos["prev_hash"]],
            tup[pos["row_hash"]],
            _canonical_from_values(values),
        )


def _chain_records(store, upto_id=None):
    """Walk records from the cheapest confirmed source.

    The raw-tuple path (round-5 fix spec H3) skips the per-row dict
    construction ``ledger_rows`` performs; it is used only when
    ``ledger_rows_raw`` exists AND its layout confirms against the
    book's own seals. Anything else — no raw method, an unconfirmable
    layout — falls back to the dict rows, so the verdicts are the
    dict path's verdicts by construction.

    With ``upto_id``, the raw source is asked for the bounded prefix
    (round-6 fix spec J6; round-6 performance F4): the walk stops
    hashing at the bound either way, but an unbounded fetch made a
    25-row prefix cost like the whole book. A raw source that does
    not accept the bound (the test doubles' zero-arg shape) is
    retried unbounded — same rows, same verdicts, just fetched whole.
    """
    raw = getattr(store, "ledger_rows_raw", None)
    if callable(raw):
        try:
            rows = raw(upto_id)
        except TypeError:
            try:
                rows = raw()
            except Exception:  # noqa: BLE001 - dict path below
                rows = None
        except Exception:  # noqa: BLE001 - fall back to the dict path
            rows = None
        if rows is not None:
            rows = list(rows)
            if not rows:
                return iter(())
            fields = _detect_raw_layout(rows)
            if fields is not None:
                return _records_from_tuples(rows, fields)
    return _records_from_dicts(store.ledger_rows())


def _walk_records(records, upto_id=None):
    """The chain walk over normalized records.

    Returns ``(ok, first_bad_id, head)``; ``head`` is
    ``(max_id, head_hash, row_count)`` of the verified scope on
    success, else None. With ``upto_id``, only rows with
    ``id <= upto_id`` are in scope and the walk stops AT the bound
    (rows arrive in id order; round-4 fix spec G3).
    """
    prev = GENESIS_HASH
    count = 0
    max_id = 0
    for rid, stored_prev, stored_hash, canonical in records:
        if upto_id is not None:
            if _is_int(rid) and rid > upto_id:
                break
            if not _is_int(rid):
                continue
        if not stored_prev or not stored_hash:
            return False, rid, None
        if stored_prev != prev:
            return False, rid, None
        if _digest(prev, canonical) != stored_hash:
            return False, rid, None
        prev = stored_hash
        count += 1
        if _is_int(rid):
            max_id = rid
    return True, None, (max_id, prev, count)


def _mark_cache_untrusted(store):
    """Record a failed cache clear on the store instance (round-7
    fix spec K7; round-7 attack F4).

    ``store._chain_cache_untrusted`` is the marker
    :func:`verify_chain_cached` reads: while it stands, the cached
    check distrusts the on-disk cache — which a proven break may
    have left behind, because the delete that was supposed to drop
    it just failed — and falls back to the full walk. Best-effort,
    like every cache interaction: a store that refuses attributes
    simply keeps the pre-K7 behavior.
    """
    try:
        store._chain_cache_untrusted = True
    except Exception:  # noqa: BLE001 - the cache must never break a walk
        pass


def _mark_cache_trusted(store):
    """Clear the cache-distrust marker (round-7 fix spec K7): a
    clear or a seed just succeeded, so the cache's state is known
    again and the cached check may trust it."""
    try:
        store._chain_cache_untrusted = False
    except Exception:  # noqa: BLE001 - the cache must never break a walk
        pass


def _refresh_verified_head(store, head):
    """Record a successful walk in the chain-verified head cache.

    Never regresses a fuller cache, and never fails the verification
    that produced the head: the cache is an optimization for the
    hook-path gate check, so bookkeeping errors are swallowed
    (round-5 fix spec, pinned interfaces).

    A seed that lands — or a cache already holding exactly this
    verified head — also clears the instance's cache-distrust
    marker (round-7 fix spec K7): the cache now provably holds a
    head a full verification produced.
    """
    get = getattr(store, "get_chain_verified_head", None)
    set_head = getattr(store, "set_chain_verified_head", None)
    if not callable(get) or not callable(set_head):
        return
    try:
        max_id, head_hash, row_count = head
        cached = get()
        if cached is not None:
            if cached[0] > max_id:
                return
            if tuple(cached) == (max_id, head_hash, row_count):
                _mark_cache_trusted(store)
                return
        set_head(max_id, head_hash, row_count)
        _mark_cache_trusted(store)
    except Exception:  # noqa: BLE001 - the cache must never break a walk
        pass


def _clear_verified_head(store):
    """Drop the verified-head cache after a FAILED full walk.

    Round-6 fix spec J3 (round-6 attack F1): the cache was only ever
    written on success and never invalidated, so once any full walk
    proved the book broken, the hook's cached check went on trusting
    its stale head — spot-checking the (untouched) head row, passing,
    and printing the doctored tier at every session start,
    indefinitely. A failed walk now deletes the cache, so
    cached-check surfaces fall back to the full walk until a
    successful walk re-seeds it. Best-effort, mirroring
    :func:`_refresh_verified_head`: a store without the accessor
    keeps working, and a deletion error never masks the walk's
    verdict.

    Round-7 fix spec K7 (round-7 attack F4): best-effort was also
    SILENT — when the delete itself failed (the reviewer's stand-in
    was WAL lock contention on the meta rows at exactly the failure
    moment), the stale cache survived byte-identical and the hook
    went on certifying the doctored book, with no trace anywhere
    that the clear had failed. A failed clear now records itself on
    the store instance (:func:`_mark_cache_untrusted`), and
    :func:`verify_chain_cached` distrusts the cache on that
    instance until a successful clear or seed. The verdict is still
    never masked; what changed is that the failure is no longer
    invisible to the one instance that knows about it.
    """
    clear = getattr(store, "clear_chain_verified_head", None)
    if not callable(clear):
        return
    try:
        clear()
    except Exception:  # noqa: BLE001 - the cache must never break a walk
        _mark_cache_untrusted(store)
        return
    _mark_cache_trusted(store)


def verify_chain(store, upto_id=None):
    """Recompute the chain from genesis — the FULL walk.

    Returns ``(True, None)`` when every row in scope seals, else
    ``(False, first_bad_id)`` — the id of the first row whose stored
    ``prev_hash`` is not the previous row's hash, whose stored
    ``row_hash`` does not recompute from its own fields, or that carries
    no hashes at all. With ``upto_id``, only rows with
    ``id <= upto_id`` are in scope (prefix verification — a certificate
    verifies the book as it stood at issuance, tightening spec T4).

    Rows come from the raw-tuple path when the store offers it
    (``ledger_rows_raw``; round-5 fix spec H3) and from dict rows
    otherwise; the two sources produce identical verdicts (pinned by
    the equivalence tests). A successful walk refreshes the store's
    chain-verified head cache — the cache the hook-path
    :func:`verify_chain_cached` trusts — and a FAILED walk deletes
    that cache (round-6 fix spec J3), so a proven break is never
    followed by a cached pass. If the delete itself fails, the
    failure is recorded on the store instance and the cached check
    distrusts the cache until a successful clear or seed
    (round-7 fix spec K7). Operator surfaces use this walk,
    never the cached check.
    """
    ok, bad, head = _walk_records(_chain_records(store, upto_id), upto_id)
    if ok:
        _refresh_verified_head(store, head)
    else:
        _clear_verified_head(store)
    return ok, bad


def _head_snapshot(store, max_id):
    """``(row_count, head_row, predecessor_hash)`` for ``id <= max_id``.

    The head row is the one with the largest id in scope, as a dict;
    ``predecessor_hash`` is the stored ``row_hash`` of the row before
    it (genesis when the head is the first row, or when the scope is
    empty). SQL through the store's connection when it exposes one,
    else the rows walk. Returns None only when neither source works.
    """
    conn = getattr(store, "_conn", None)
    if conn is not None:
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM ledger WHERE id<=?",
                (max_id,)).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM ledger WHERE id<=?"
                " ORDER BY id DESC LIMIT 2", (max_id,)).fetchall()
            if not rows:
                return count, None, GENESIS_HASH
            head = {key: rows[0][key] for key in rows[0].keys()}
            pred = rows[1]["row_hash"] if len(rows) > 1 else GENESIS_HASH
            return count, head, pred
        except Exception:  # noqa: BLE001 - fall back to the rows walk
            pass
    try:
        rows = store.ledger_rows()
    except Exception:  # noqa: BLE001 - no source at all
        return None
    scoped = [
        row for row in rows
        if _is_int(row.get("id")) and row["id"] <= max_id
    ]
    if not scoped:
        return len(scoped), None, GENESIS_HASH
    head = scoped[-1]
    pred = scoped[-2]["row_hash"] if len(scoped) > 1 else GENESIS_HASH
    return len(scoped), head, pred


def _suffix_records(store, after_id):
    """Walk records for rows with ``id > after_id``, in id order.

    SQL through the store's connection when it exposes one (so the
    cached check does not materialize the verified prefix), else the
    cheapest confirmed row source, filtered.
    """
    conn = getattr(store, "_conn", None)
    if conn is not None:
        try:
            cursor = conn.execute(
                "SELECT * FROM ledger WHERE id>? ORDER BY id ASC",
                (after_id,))
            cols = [col[0] for col in cursor.description]
            records = []
            for raw in cursor.fetchall():
                row = dict(zip(cols, raw))
                records.append((
                    row.get("id"), row.get("prev_hash"),
                    row.get("row_hash"), canonical_row(row)))
            return records
        except Exception:  # noqa: BLE001 - fall back to the row source
            pass
    return [
        record for record in _chain_records(store)
        if _is_int(record[0]) and record[0] > after_id
    ]


def _full_walk_outcome(store):
    """:func:`verify_chain`'s verdict in the cached check's outcome
    shape: ``(ok, bad, "full", None)``."""
    ok, bad = verify_chain(store)
    return ok, bad, "full", None


def _verify_chain_cached_detail(store):
    """The cached check, naming the check that ran.

    Returns ``(ok, bad, mode, head_id)`` — the verdict plus, for
    surfaces that word a claim about the check (round-6 fix spec
    J3), which check produced it: ``mode == "cached"`` when the
    verdict rests on the cache (the trusted head at ``head_id``
    spot-checked, the suffix verified), ``mode == "full"`` when the
    full walk produced the verdict (``head_id`` is None then).

    Round-7 fix spec K7 (round-7 attack F4): while the store
    instance carries the cache-distrust marker — a failed walk's
    attempt to delete the cache itself failed, so the on-disk
    cache may be the stale head a proven break left behind — the
    cache is not consulted at all: the full walk answers, until a
    successful clear or seed clears the marker.
    """
    if getattr(store, "_chain_cache_untrusted", False):
        return _full_walk_outcome(store)
    get = getattr(store, "get_chain_verified_head", None)
    cache = None
    if callable(get):
        try:
            cache = get()
        except Exception:  # noqa: BLE001 - a broken cache is no cache
            cache = None
    if not cache:
        return _full_walk_outcome(store)
    try:
        max_id, head_hash, row_count = cache
    except (TypeError, ValueError):
        return _full_walk_outcome(store)
    if (
        not _is_int(max_id)
        or not _is_int(row_count)
        or not isinstance(head_hash, str)
    ):
        return _full_walk_outcome(store)
    snapshot = _head_snapshot(store, max_id)
    if snapshot is None:
        return _full_walk_outcome(store)
    count, head_row, pred_hash = snapshot
    if count != row_count:
        return _full_walk_outcome(store)
    if max_id == 0:
        if head_row is not None:
            return _full_walk_outcome(store)
        prev = GENESIS_HASH
    else:
        if head_row is None or head_row.get("id") != max_id:
            return _full_walk_outcome(store)
        if head_row.get("row_hash") != head_hash:
            return _full_walk_outcome(store)
        if head_row.get("prev_hash") != pred_hash:
            return _full_walk_outcome(store)
        if row_hash(pred_hash, head_row) != head_hash:
            return _full_walk_outcome(store)
        prev = head_hash
    verified = row_count
    last_id = max_id
    for rid, stored_prev, stored_hash, canonical in _suffix_records(
        store, max_id
    ):
        if not stored_prev or not stored_hash:
            return False, rid, "cached", max_id
        if stored_prev != prev:
            # The suffix does not link to the spot-checked head: the
            # book changed in a way the spot check cannot localize —
            # get the exact answer from the full walk.
            return _full_walk_outcome(store)
        if _digest(prev, canonical) != stored_hash:
            return False, rid, "cached", max_id
        prev = stored_hash
        verified += 1
        if _is_int(rid):
            last_id = rid
    _refresh_verified_head(store, (last_id, prev, verified))
    return True, None, "cached", max_id


def verify_chain_cached(store):
    """The cache-aware gate check: suffix verification from the
    chain-verified head cache.

    Returns ``(True, None)`` / ``(False, first_bad_id)`` like
    :func:`verify_chain`, but it is NOT a full verification — the
    split is documented in the module docstring and matters: the
    cached prefix's interior is trusted from the full walk that
    wrote the cache; this check confirms the head row still
    recomputes and links and that the row count at/below it is
    unchanged, then verifies only the rows after the head. Any
    doubt — no cache, a malformed cache, the head moved or fails to
    recompute, the count changed, the first suffix row does not
    link to the cached head — falls back to the full walk and
    returns ITS verdict. A successful suffix walk extends the
    cache to the new head. This is the hook path's check; operator
    surfaces use :func:`verify_chain`.

    The check that ran is recorded on the store as
    ``_mm_last_chain_check`` — ``("cached", head_id)`` when the
    cached head was trusted, ``("full", None)`` when the full walk
    ran — so a surface can word its claim to match the check
    (round-6 fix spec J3: the CLI's verify-cert scoping sentence
    reads it, and must not assert a whole-chain walk the cached
    check did not perform). A failed full walk anywhere in this
    check deletes the cache (see :func:`verify_chain`), and nothing
    here resurrects a cache a failed walk cleared: with no cache,
    this check IS the full walk. And when the delete itself failed,
    the instance's cache-distrust marker (round-7 fix spec K7)
    makes this check the full walk too — a cache a proven break
    may have left behind is not trusted on the say-so of a delete
    that never landed.
    """
    ok, bad, mode, head_id = _verify_chain_cached_detail(store)
    try:
        store._mm_last_chain_check = (mode, head_id)
    except Exception:  # noqa: BLE001 - a store that refuses attributes
        pass
    return ok, bad


# ---------------------------------------------------------------------------
# The local key
# ---------------------------------------------------------------------------

#: A valid key file is exactly this many raw bytes.
_KEY_LEN = 32

_KEY_ID_MESSAGE = b"mm-key-id"

# A racing reader that finds the key file momentarily empty retries the
# read briefly before concluding the file is corrupt (security F9).
_KEY_READ_ATTEMPTS = 50
_KEY_READ_DELAY_S = 0.01


def key_id(key: bytes) -> str:
    """The public id of a store key.

    First 16 hex chars of ``HMAC(key, b"mm-key-id")`` — enough for
    verification to tell "issued by a different store" apart from
    "forged", without exposing any of the key (host F2).
    """
    return hmac.new(key, _KEY_ID_MESSAGE, hashlib.sha256).hexdigest()[:16]


def _read_key_file(key_path):
    """The key file's raw bytes, or None when it does not exist.

    Raw bytes, never stripped: a key whose last byte is whitespace is a
    valid key, and stripping it minted certificates no later process
    could verify (security F6). An empty read retries briefly — a
    racing creator may be caught mid-write — before the caller treats
    the file as corrupt.
    """
    for _attempt in range(_KEY_READ_ATTEMPTS):
        try:
            data = key_path.read_bytes()
        except FileNotFoundError:
            return None
        if data:
            return data
        time.sleep(_KEY_READ_DELAY_S)
    return b""


def _fsync_dir(parent):
    """Best-effort fsync of a directory after a publish/replace in it."""
    try:
        dir_fd = os.open(str(parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass  # the directory fsync is best-effort durability


def _write_key_file(key_path, key):
    """Persist ``key`` atomically at ``key_path``.

    Temp file in the same directory (mode 0600), fsync, ``os.replace``
    onto the path, directory fsync — a crash or a racing reader never
    sees a half-written or empty key (host F1).
    """
    key_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(key_path.parent), prefix=".cert_key.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, key_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    _fsync_dir(key_path.parent)


def _publish_key_file(key_path, key):
    """Publish ``key`` at ``key_path`` ONLY if no file is there (G5).

    The temp file is fully written and fsynced first, then hard-linked
    onto the path: ``os.link`` fails with :class:`FileExistsError`
    when a racing creator won, and the loser discards its temp and
    ADOPTS the key the file already holds. Two processes first-opening
    a fresh store therefore converge on one key instead of each
    keeping its own (round-3 regression F3 — the tightening pass's
    unconditional ``os.replace`` had removed the pre-tightening code's
    race convergence). Returns the key the file holds on return.
    """
    key_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(key_path.parent), prefix=".cert_key.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, key_path)
        except FileExistsError:
            adopted = _read_key_file(key_path)
            if adopted is not None and len(adopted) == _KEY_LEN:
                return adopted
            # The file that beat us is itself unusable; heal it via
            # the replace path with our key.
            return _replace_key_file(key_path, key)
        except OSError:
            # Round-5 fix (attack F6): on a filesystem without
            # hardlink support os.link raises PermissionError/EPERM,
            # which must not kill the certificate feature on an
            # otherwise writable directory — fall back to the replace
            # publish (its post-write adopt keeps racers convergent).
            return _replace_key_file(key_path, key)
        _fsync_dir(key_path.parent)
        return key
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _replace_key_file(key_path, key):
    """Replace a corrupt key file, then adopt the file's final content.

    The replace itself is the atomic ``os.replace`` write (the corrupt
    path keeps it — G5). Re-reading afterwards and adopting whatever
    the file then holds makes two processes racing to heal the same
    corrupt file converge on one key: the last replace wins the file,
    and every replacer ends up holding the file's content rather than
    its own candidate key.

    Round-5 fix (attack F6): re-read FIRST and adopt the file if it
    has already become valid — two healers acting in SEQUENCE (not
    simultaneously) otherwise diverge: the second would replace over
    the first's now-valid key while the first process keeps its
    cached key, and its certificates read "different store" until a
    restart. Only a file still corrupt at this moment is replaced.
    """
    existing = None if key_path.is_symlink() else _read_key_file(key_path)
    if existing is not None and len(existing) == _KEY_LEN:
        return existing
    _write_key_file(key_path, key)
    final = _read_key_file(key_path)
    if final is not None and len(final) == _KEY_LEN:
        return final
    return key


def cert_key(store) -> bytes:
    """The store's LOCAL HMAC key, generated once and cached.

    File-backed stores keep it at ``<data dir>/cert_key`` beside the
    database; a ``:memory:`` store gets an ephemeral per-instance key.
    A missing key file is published only-if-absent (temp + ``os.link``)
    so two processes first-opening a fresh store converge on the one
    key the file ends up holding (round-4 fix spec G5); an existing
    file that is not exactly 32 raw bytes — empty, truncated,
    overlong — or a symlink at the path is corrupt and is replaced
    atomically with a fresh key, after which the file's final content
    is adopted so racing healers converge too (certificates signed
    under a corrupt key were unverifiable anyway). A file-backed store
    whose key cannot be persisted raises
    :class:`CertificateKeyError` — there is NO ephemeral fallback
    (tightening spec T5). This key is a store-integrity seal only —
    see the module docstring for what it does not prove.
    """
    cached = getattr(store, "_mm_cert_key", None)
    if cached is not None:
        return cached
    db_path = getattr(store, "db_path", None)
    if not db_path or db_path == ":memory:":
        key = os.urandom(_KEY_LEN)
    else:
        key_path = Path(db_path).expanduser().parent / "cert_key"
        try:
            if key_path.is_symlink():
                # A symlink at the path is corrupt (security F9): the
                # replace path swaps the link itself for a real file,
                # never following it.
                key = _replace_key_file(key_path, os.urandom(_KEY_LEN))
            else:
                data = _read_key_file(key_path)
                if data is not None and len(data) == _KEY_LEN:
                    key = data
                elif data is None:
                    key = _publish_key_file(
                        key_path, os.urandom(_KEY_LEN))
                else:
                    key = _replace_key_file(
                        key_path, os.urandom(_KEY_LEN))
        except CertificateKeyError:
            raise
        except OSError as err:
            raise CertificateKeyError(
                f"cannot persist certificate key at {key_path}: {err}"
            ) from err
    try:
        store._mm_cert_key = key
    except Exception:  # noqa: BLE001 - a store that refuses attributes
        pass
    return key


# ---------------------------------------------------------------------------
# Totals and the stamp
# ---------------------------------------------------------------------------

def run_totals(store, contract_id, upto_id=None):
    """Tokens, cost and cost coverage recorded for one contract.

    Delegates to the one aggregation
    (:func:`hybrid_critic_loop.standing.ledger_totals`) restricted to
    this contract — and, with ``upto_id``, to the rows at or below a
    certificate's high-water mark — so the stamp, the certificate and
    the standing surfaces are the same numbers by construction
    (tightening spec T1). A figure no provider reported stays ``None``
    ("not reported"), never an estimate.
    """
    totals = _ledger_totals(store, contract_id=contract_id, upto_id=upto_id)
    tokens = totals["tokens"]
    if isinstance(tokens, float) and tokens.is_integer():
        # Integral totals surface as ints (round-4 fix spec G4): a
        # pre-validation book can carry fractional per-row counts
        # whose sum is whole, and the certificate payload's token
        # field is an int by shape.
        tokens = int(tokens)
    return {
        "tokens": tokens,
        "cost_usd": totals["cost_usd"],
        "cost_rows": totals["cost_rows"],
        "cost_missing": totals["cost_missing"],
    }


def lifetime_spend(store):
    """Lifetime measured spend: the one aggregation's cost over the book."""
    return _ledger_totals(store)["cost_usd"]


def stamp_text(totals) -> str:
    """The MM CERTIFIED stamp for a run's totals.

    ``MM CERTIFIED · <tokens> TOKENS SPENT · $<cost> BURNED · ONE ANSWER``
    — the dollar segment is omitted when the run's cost was not reported
    by any provider (never estimated), and an unreported token count is
    named as such instead of being invented.
    """
    tokens = totals.get("tokens")
    cost = totals.get("cost_usd")
    parts = ["MM CERTIFIED"]
    if tokens is None:
        parts.append("TOKENS NOT REPORTED")
    else:
        parts.append(f"{int(tokens):,} TOKENS SPENT")
    if cost is not None:
        parts.append(f"${float(cost):,.2f} BURNED")
    parts.append("ONE ANSWER")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------

def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(key: bytes, payload_b64: str) -> str:
    sig = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(sig)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sql_scalar(store, sql, params=()):
    """One scalar via the store's connection, or None when the store
    exposes no usable connection (round-5 fix spec H3: the one-row
    questions below are indexed lookups on the real store — the
    round-5 performance audit measured the whole-book walks they
    replace at ~1.95 s per call on a 200k-row book, against 0.0001 s
    for the indexed query). A query that fails also yields None:
    callers fall back to the rows walk, whose answer is the
    reference."""
    conn = getattr(store, "_conn", None)
    if conn is None:
        return None
    try:
        row = conn.execute(sql, params).fetchone()
    except Exception:  # noqa: BLE001 - fall back to the rows walk
        return None
    if row is None:
        return None
    return row[0]


def _chain_head(store, contract_id, upto_id=None):
    """The row_hash of the contract's last ledger row in scope, or None."""
    if upto_id is None:
        head = _sql_scalar(
            store,
            "SELECT row_hash FROM ledger WHERE contract_id=?"
            " ORDER BY id DESC LIMIT 1",
            (contract_id,))
        if head is not None:
            return head
    else:
        head = _sql_scalar(
            store,
            "SELECT row_hash FROM ledger WHERE contract_id=?"
            " AND id<=? ORDER BY id DESC LIMIT 1",
            (contract_id, upto_id))
        if head is not None:
            return head
    head = None
    for row in store.ledger_rows():
        if not isinstance(row, dict):
            continue
        if row.get("contract_id") != contract_id:
            continue
        if upto_id is not None:
            rid = row.get("id")
            if not _is_int(rid) or rid > upto_id:
                continue
        head = row.get("row_hash")
    return head


def _ledger_upto(store, contract_id) -> int:
    """The contract's high-water mark: its max ledger id (0 when none)."""
    top = _sql_scalar(
        store,
        "SELECT MAX(id) FROM ledger WHERE contract_id=?",
        (contract_id,))
    if top is not None:
        return int(top)
    upto = 0
    for row in store.ledger_rows():
        if not isinstance(row, dict):
            continue
        if row.get("contract_id") != contract_id:
            continue
        rid = row.get("id")
        if _is_int(rid) and rid > upto:
            upto = rid
    return upto


def _book_max_ledger_id(store) -> int:
    """The whole book's high-water mark: its max ledger id (0 if empty).

    Used ONLY when phrasing a verify failure reason, so an operator
    can see at a glance whether their book is simply BEHIND the
    certificate's prefix or genuinely diverges from it (round-4
    upgrade F4) — it is computed lazily, at the moment a reason is
    being built, never on a successful verify (round-5 fix spec H3;
    round-5 performance findings).
    """
    top = _sql_scalar(store, "SELECT MAX(id) FROM ledger")
    if top is not None:
        return int(top)
    top = 0
    for row in store.ledger_rows():
        if not isinstance(row, dict):
            continue
        rid = row.get("id")
        if _is_int(rid) and rid > top:
            top = rid
    return top


#: Contract statuses that mean the run completed (the loop sets
#: exactly this one on the approval path, alongside the final answer).
_TERMINAL_COMPLETED_STATUSES = frozenset({"completed"})


def _contract_completed(store, contract_id) -> bool:
    """Whether the contract's run COMPLETED (round-5 fix spec H1;
    round-5 gate-core F2).

    Completed means a ``final_answers`` row exists for the contract,
    or the contract's status is a terminal completed status. The
    verdict row alone is NOT completion: under claim-first gating
    the verdict records the decision before the merge and the final
    answer exist, so a claimed-but-uncompleted gate used to mint a
    verifying certificate for an answer that never landed.

    Surfaces, in order: the store's connection (the real Store has
    no final-answer getter, so the lookup is SQL), then
    ``get_contract`` for the status. A duck-typed store exposing NO
    completion surface at all — no connection and no
    ``get_contract`` — degrades to the pre-fix behavior (issuance
    proceeds): such stores are test doubles whose verdict rows are
    their whole world, and the completion question cannot even be
    expressed there. The real Store exposes both surfaces, so
    production issuance is always gated.
    """
    surface = False
    conn = getattr(store, "_conn", None)
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT 1 FROM final_answers WHERE contract_id=?"
                " LIMIT 1", (contract_id,)).fetchone()
            surface = True
            if row is not None:
                return True
        except Exception:  # noqa: BLE001 - try the status surface
            pass
    get_contract = getattr(store, "get_contract", None)
    if callable(get_contract):
        surface = True
        try:
            contract = get_contract(contract_id)
        except Exception:  # noqa: BLE001 - unprovable is not completed
            return False
        return (
            isinstance(contract, dict)
            and contract.get("status") in _TERMINAL_COMPLETED_STATUSES
        )
    return not surface


def issue_certificate(store, contract_id, _chain_verified=False):
    """Issue the certificate block for a contract, or None.

    A certificate exists only for a run that reached a verdict with a
    winner — a contract with no verdict row, or an escalated verdict
    whose winner is None, gets None, and no stamp is printed for it
    anywhere (tightening spec T6; claims #1). Also None when the run
    has no COUNTED ledger rows behind it (round-4 fix spec G4;
    round-3 attack F8): the empty prefix trivially seals, so a
    zero-row verdict would otherwise mint a certificate carrying the
    whole book's tier on numbers that are all "not reported". Also
    None when the run's token total is fractional — a pre-validation
    book can hold fractional counts, and the payload shape requires an
    int, so minting would produce a token verify calls malformed
    (round-3 regression F5). Also None when the run never COMPLETED
    — no final answer, no terminal completed status (round-5 fix
    spec H1; round-5 gate-core F2: issuance used to read only the
    verdict row, which claim-first gating flips at claim time, so a
    claimed-but-unmerged gate certified an answer that never
    landed). Also None when the chain prefix the certificate would
    seal does not verify (round-5 fix spec H2; round-5 attack F2:
    the verdict report minted a fresh stamp and token over a
    mid-book-tampered store because issuance never consulted the
    chain). Also None when the store's certificate key cannot be
    persisted (:class:`CertificateKeyError`): no certificate beats
    one that can never verify. Returns ``{"stamp", "cert",
    "payload"}``.

    ``_chain_verified`` is an internal threading seam (round-6 fix
    spec J6; round-6 performance F2): a caller that has JUST
    full-walked this book successfully in the same operation —
    reports.verdict_report's chain gate — passes True, and issuance
    reuses that walk instead of re-walking the prefix the gate just
    verified (a verdict report used to pay both walks). Standalone
    callers leave it False and issuance verifies its own prefix,
    exactly as before; passing it without a just-completed
    successful full walk of the same book state voids the
    chain-gated-issuance guarantee, so nothing outside that one
    call site may set it. The payload's ``issued_at`` is the verdict row's
    own timestamp, so the same run renders the same token every time.
    The payload also seals the verdict's gate outcome — ``measured``
    and ``waived`` — so a winner that failed the measured gate and
    merged only by human waiver is distinguishable from a clean pass
    (round-4 regression F3).
    """
    verdict = store.latest_verdict(contract_id)
    if verdict is None or verdict.get("winner") is None:
        return None
    try:
        key = cert_key(store)
    except CertificateKeyError:
        return None
    upto = _ledger_upto(store, contract_id)
    totals = run_totals(store, contract_id, upto_id=upto)
    if totals["cost_rows"] + totals["cost_missing"] < 1:
        return None
    if totals["tokens"] is not None and not _is_int(totals["tokens"]):
        return None
    if not _contract_completed(store, contract_id):
        return None
    if not _chain_verified:
        prefix_ok, _bad = verify_chain(store, upto_id=upto)
        if not prefix_ok:
            return None
    payload = {
        "contract_id": contract_id,
        "verdict_id": verdict.get("id"),
        "tokens": totals["tokens"],
        "cost_usd": totals["cost_usd"],
        "tier": tier_for(lifetime_spend(store)),
        "chain_head": _chain_head(store, contract_id, upto_id=upto),
        "issued_at": verdict.get("created_at"),
        "ledger_upto": upto,
        "kid": key_id(key),
        "cost_rows": totals["cost_rows"],
        "cost_missing": totals["cost_missing"],
        "measured": verdict.get("measured"),
        "waived": verdict.get("human_gate") == "waived",
    }
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload_b64 = _b64url_encode(payload_json.encode("utf-8"))
    token = f"{_TOKEN_PREFIX}.{payload_b64}.{_sign(key, payload_b64)}"
    return {"stamp": stamp_text(totals), "cert": token, "payload": payload}


def _base_payload_ok(payload) -> bool:
    """The pre-tightening payload keys, present and sanely typed."""
    if not isinstance(payload.get("contract_id"), str):
        return False
    if not payload["contract_id"]:
        return False
    if not isinstance(payload.get("verdict_id"), str):
        return False
    if not payload["verdict_id"]:
        return False
    if "tokens" not in payload:
        return False
    if payload["tokens"] is not None and not _is_int(payload["tokens"]):
        return False
    if "cost_usd" not in payload:
        return False
    cost = payload["cost_usd"]
    if cost is not None and (
        not isinstance(cost, (int, float))
        or isinstance(cost, bool)
        or not math.isfinite(cost)
    ):
        return False
    if payload.get("tier") not in _TIER_RANK:
        return False
    if "chain_head" not in payload:
        return False
    if payload["chain_head"] is not None and not isinstance(
        payload["chain_head"], str
    ):
        return False
    if not isinstance(payload.get("issued_at"), str):
        return False
    if not payload["issued_at"]:
        return False
    return True


def _current_payload_ok(payload) -> bool:
    """The current-format payload keys, present and sanely typed."""
    if not _is_int(payload.get("ledger_upto")) or payload["ledger_upto"] < 0:
        return False
    if not isinstance(payload.get("kid"), str) or not payload["kid"]:
        return False
    for field in ("cost_rows", "cost_missing"):
        if not _is_int(payload.get(field)) or payload[field] < 0:
            return False
    measured = payload.get("measured")
    if measured is not None and not isinstance(measured, str):
        return False
    if not isinstance(payload.get("waived"), bool):
        return False
    return True


def _named_verdict(store, contract_id, verdict_id):
    """The verdict a certificate NAMES (round-4 fix spec G4).

    Uses ``store.verdict_for`` — the named verdict, never the
    contract's latest: a re-run's attempt-2 verdict must not orphan
    the attempt-1 certificate (round-3 regression F2). Duck-typed
    stores predating the accessor fall back to ``latest_verdict``
    plus id equality, which keeps the semantics identical (for such a
    store the named verdict verifies only when it IS the latest).
    """
    verdict_for = getattr(store, "verdict_for", None)
    if callable(verdict_for):
        return verdict_for(contract_id, verdict_id)
    latest = store.latest_verdict(contract_id)
    if latest is not None and latest.get("id") == verdict_id:
        return latest
    return None


def _totals_mismatch_reason(store, upto, what, seals, counts):
    """The figured reason for a certificate/store totals mismatch.

    Built LAZILY — only when a mismatch has already been found —
    because phrasing it needs the whole book's high-water mark, a
    one-row question that must not run on a successful verify
    (round-5 fix spec H3; round-5 performance findings). When the
    book's max id is BELOW the certificate's sealed prefix, the
    reason says rows are missing outright — the book behind the
    certificate — instead of the bare mismatch wording that made a
    truncated restore indistinguishable from a divergent one
    (round-5 fix spec H6). Otherwise the wording is the format the
    earlier rounds established, unchanged.
    """
    book_max = _book_max_ledger_id(store)
    scope = (
        f"certificate covers ledger rows up to {upto}; "
        f"this book's ledger runs to row {book_max}"
    )
    if book_max < upto:
        return (
            f"run totals do not match the ledger ({what}): "
            f"certificate seals {seals}, this book counts {counts}"
            f" — rows are missing: {scope}, so the book is behind"
            " the certificate (a truncated or older restore?)"
        )
    return (
        f"run totals do not match the ledger ({what}): certificate "
        f"seals {seals}, this book counts {counts} ({scope})"
    )


def verify_certificate(store, token):
    """Verify a certificate token against this store.

    Returns ``(True, payload)`` or ``(False, reason)``. Checks, in
    order: the token's shape; the payload decodes to a dict carrying
    the full key set with sane types (else "malformed certificate
    payload" — an empty, mistyped or field-less payload never verifies,
    correctness F10); the legacy-format check (a token without
    ``ledger_upto`` / ``kid`` / ``measured`` / ``waived`` predates the
    current format); the key id (a mismatch means this store's
    ``cert_key`` is not the key that signed — the reason names the key
    file and, for a book restored from backup, the remedy: restore its
    ``cert_key`` beside it — host F2, round-4 upgrade F4); the HMAC
    signature under the store's local key; the NAMED verdict — the
    verdict row the payload's ``verdict_id`` identifies, with matching
    id, ``created_at``, gate outcome (``measured`` / ``waived``) and a
    winner, NOT the contract's latest verdict (a certificate for a
    contract with no such verdict is a forgery, security F3; the
    named-verdict rule is round-4 fix spec G4); the chain prefix up to
    ``ledger_upto``; the run totals and chain head recomputed over
    that prefix (totals failures name the sealed and recomputed
    figures and the prefix bound against the book's own high-water
    mark, so a book that is merely behind reads differently from one
    that diverges — round-4 upgrade F4); and the tier, recomputed from
    the CURRENT lifetime spend under the one aggregation.

    The tier rule, stated plainly: the sealed tier is a point-in-time
    claim. Verification rejects a certificate whose sealed tier ranks
    ABOVE the store's current tier — spend recorded later can only
    raise the current tier, so a lower sealed tier is legitimate
    growth, while a higher one means the book no longer backs the
    claim (rows were deleted or re-attributed). The pre-tightening
    text claimed the tier is not recomputed because "lifetime spend
    only grows"; that claim was false and is deleted (security F3).
    """
    if not isinstance(token, str):
        return False, "not a certificate token"
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_PREFIX:
        return False, "malformed certificate token"
    _, payload_b64, sig_b64 = parts
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:  # noqa: BLE001 - any decode failure is one reason
        return False, "payload does not decode"
    if not isinstance(payload, dict):
        return False, "payload does not decode"
    if not _base_payload_ok(payload):
        return False, "malformed certificate payload"
    if (
        "ledger_upto" not in payload
        or "kid" not in payload
        or "measured" not in payload
        or "waived" not in payload
    ):
        return False, (
            "certificate predates the current format; "
            "re-issue it from the store"
        )
    if not _current_payload_ok(payload):
        return False, "malformed certificate payload"
    try:
        key = cert_key(store)
    except CertificateKeyError:
        return False, "certificate key unavailable"
    if payload["kid"] != key_id(key):
        return False, (
            "the store's cert_key does not match the certificate's "
            "key id; if this book was restored from backup, restore "
            "its cert_key beside it"
        )
    expected = _sign(key, payload_b64)
    if not hmac.compare_digest(expected, sig_b64):
        return False, "signature mismatch"
    contract_id = payload["contract_id"]
    verdict = _named_verdict(store, contract_id, payload["verdict_id"])
    if (
        verdict is None
        or verdict.get("winner") is None
        or verdict.get("id") != payload["verdict_id"]
        or verdict.get("created_at") != payload["issued_at"]
        or verdict.get("measured") != payload["measured"]
        or (verdict.get("human_gate") == "waived") != payload["waived"]
    ):
        return False, "verdict does not match the certificate"
    upto = payload["ledger_upto"]
    ok, bad_id = verify_chain(store, upto_id=upto)
    if not ok:
        return False, f"ledger chain broken at row {bad_id}"
    totals = run_totals(store, contract_id, upto_id=upto)
    if payload["tokens"] != totals["tokens"]:
        return False, _totals_mismatch_reason(
            store, upto, "tokens", payload["tokens"], totals["tokens"])
    cost = payload["cost_usd"]
    actual_cost = totals["cost_usd"]
    if (cost is None) != (actual_cost is None) or (
        cost is not None and abs(float(cost) - float(actual_cost)) > 1e-9
    ):
        return False, _totals_mismatch_reason(
            store, upto, "cost_usd", cost, actual_cost)
    if payload["chain_head"] != _chain_head(store, contract_id, upto_id=upto):
        return False, "chain head does not match the ledger"
    current_tier = tier_for(lifetime_spend(store))
    if _TIER_RANK[payload["tier"]] < _TIER_RANK[current_tier]:
        return False, "certificate tier is above the store's current tier"
    return True, payload
