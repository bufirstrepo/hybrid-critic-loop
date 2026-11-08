"""Hash-chained ledger and per-run certificates (Unit B, anti-fake core).

The ledger is the plugin's book: every dispatch, token count and measured
dollar lives there. This module seals it and signs what it says.

**The chain.** Every ledger row carries ``prev_hash`` / ``row_hash``:
``row_hash = sha256(prev_hash + canonical(row))`` where ``canonical`` is a
stable JSON serialization of the row's own fields and the first row's
``prev_hash`` is :data:`GENESIS_HASH`. Rewriting any stored row — inflating
a token count, inventing spend — breaks every recomputation downstream of
it, and :func:`verify_chain` names the first row that no longer seals.

**The certificate.** A run that reached a verdict is issued a certificate:
the ``MM CERTIFIED`` stamp plus a signed token
``mm1.<base64url(payload)>.<base64url(sig)>``. The payload names the
contract, the verdict, the run's totals *as recorded in the ledger*, the
operator's tier at issue time, and the chain head (the ``row_hash`` of the
contract's last ledger row). ``sig`` is HMAC-SHA256 over the payload with
the store's LOCAL key.

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
import os
from pathlib import Path

__all__ = [
    "GENESIS_HASH",
    "LEDGER_FIELDS",
    "canonical_row",
    "row_hash",
    "verify_chain",
    "cert_key",
    "run_totals",
    "stamp_text",
    "issue_certificate",
    "verify_certificate",
]

#: The prev_hash of the first ledger row in any store.
GENESIS_HASH = "0" * 64

#: The ledger row's own fields, in canonical order. The two hash columns
#: themselves are never part of the serialization (a row cannot seal
#: its own seal).
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
)

_TOKEN_PREFIX = "mm1"

# The tier ladder (spec 2026-11-08, lifetime measured spend). Unit A's
# ``standing.tier_for`` owns this table; it is mirrored here ONLY as a
# fallback so certificates can be issued from a tree where the standing
# module has not landed yet. When ``standing`` is importable its ladder
# is used and this copy is never consulted.
_FALLBACK_TIERS = (
    (1_000_000.0, "MILLIONAIRE"),
    (100_000.0, "BALLER"),
    (10_000.0, "PLAYER"),
    (1_000.0, "HIGH ROLLER"),
    (0.01, "CLIENT"),
)


def _fallback_tier_for(spend):
    if spend is None:
        return "WINDOW SHOPPER"
    for threshold, name in _FALLBACK_TIERS:
        if spend >= threshold:
            return name
    return "WINDOW SHOPPER"


def _tier_for(spend):
    try:
        from .standing import tier_for

        return tier_for(spend)
    except Exception:  # noqa: BLE001 - standing not landed yet; mirror above
        return _fallback_tier_for(spend)


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------

def canonical_row(row) -> str:
    """Stable serialization of a ledger row's own fields.

    Fixed field set, sorted keys, compact separators — the same dict
    always serializes to the same string, so a hash computed at append
    time recomputes identically at verify time.
    """
    body = {field: row.get(field) for field in LEDGER_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def row_hash(prev_hash, row) -> str:
    """``sha256(prev_hash + canonical(row))`` as a hex digest."""
    data = (str(prev_hash) + canonical_row(row)).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def verify_chain(store):
    """Recompute the whole chain from genesis.

    Returns ``(True, None)`` when every row seals, else
    ``(False, first_bad_id)`` — the id of the first row whose stored
    ``prev_hash`` is not the previous row's hash, whose stored
    ``row_hash`` does not recompute from its own fields, or that carries
    no hashes at all.
    """
    prev = GENESIS_HASH
    for row in store.ledger_rows():
        stored_prev = row.get("prev_hash")
        stored_hash = row.get("row_hash")
        if not stored_prev or not stored_hash:
            return False, row.get("id")
        if stored_prev != prev:
            return False, row.get("id")
        if row_hash(prev, row) != stored_hash:
            return False, row.get("id")
        prev = stored_hash
    return True, None


# ---------------------------------------------------------------------------
# The local key
# ---------------------------------------------------------------------------

def cert_key(store) -> bytes:
    """The store's LOCAL HMAC key, generated once and cached.

    File-backed stores keep it at ``<data dir>/cert_key`` (32 random
    bytes, file perms 600) beside the database; a ``:memory:`` store gets
    an ephemeral per-instance key. This key is a store-integrity seal
    only — see the module docstring for what it does not prove.
    """
    cached = getattr(store, "_mm_cert_key", None)
    if cached is not None:
        return cached
    key = None
    db_path = getattr(store, "db_path", None)
    if db_path and db_path != ":memory:":
        key_path = Path(db_path).expanduser().parent / "cert_key"
        try:
            if key_path.exists():
                key = key_path.read_bytes().strip()
            if not key:
                key = os.urandom(32)
                key_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(
                    str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                try:
                    os.write(fd, key)
                finally:
                    os.close(fd)
        except FileExistsError:
            # Another opener created it first; read theirs.
            key = key_path.read_bytes().strip()
        except OSError:
            key = None
    if not key:
        key = os.urandom(32)
    try:
        store._mm_cert_key = key
    except Exception:  # noqa: BLE001 - a store that refuses attributes
        pass
    return key


# ---------------------------------------------------------------------------
# Totals and the stamp
# ---------------------------------------------------------------------------

def _sum_reported(values):
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present)


def run_totals(store, contract_id):
    """Tokens and cost recorded in the ledger for one contract.

    Summed over reported values only — a figure that no provider reported
    stays ``None`` ("not reported"), never an estimate. ``tokens`` counts
    a row when either side (in or out) was recorded.
    """
    tokens = []
    costs = []
    for row in store.ledger_rows():
        if row.get("contract_id") != contract_id:
            continue
        tin, tout = row.get("tokens_in"), row.get("tokens_out")
        if tin is not None or tout is not None:
            tokens.append((tin or 0) + (tout or 0))
        costs.append(row.get("cost_usd"))
    return {
        "tokens": _sum_reported(tokens),
        "cost_usd": _sum_reported(costs),
    }


def lifetime_spend(store):
    """Lifetime measured spend: every recorded ``cost_usd`` in the book."""
    return _sum_reported(
        [row.get("cost_usd") for row in store.ledger_rows()]
    )


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


def _chain_head(store, contract_id):
    """The row_hash of the contract's last ledger row, or None."""
    head = None
    for row in store.ledger_rows():
        if row.get("contract_id") == contract_id:
            head = row.get("row_hash")
    return head


def issue_certificate(store, contract_id):
    """Issue the certificate block for a contract, or None.

    A certificate exists only for a run that reached a verdict — a
    contract with no verdict row gets None, and no stamp is printed for
    it anywhere. Returns ``{"stamp", "cert", "payload"}``. The payload's
    ``issued_at`` is the verdict row's own timestamp, so the same run
    renders the same token every time.
    """
    verdict = store.latest_verdict(contract_id)
    if verdict is None:
        return None
    totals = run_totals(store, contract_id)
    payload = {
        "contract_id": contract_id,
        "verdict_id": verdict.get("id"),
        "tokens": totals["tokens"],
        "cost_usd": totals["cost_usd"],
        "tier": _tier_for(lifetime_spend(store)),
        "chain_head": _chain_head(store, contract_id),
        "issued_at": verdict.get("created_at"),
    }
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload_b64 = _b64url_encode(payload_json.encode("utf-8"))
    token = f"{_TOKEN_PREFIX}.{payload_b64}.{_sign(cert_key(store), payload_b64)}"
    return {"stamp": stamp_text(totals), "cert": token, "payload": payload}


def verify_certificate(store, token):
    """Verify a certificate token against this store.

    Returns ``(True, payload)`` or ``(False, reason)``. Checks, in order:
    the token's shape; the HMAC signature under the store's local key;
    the ledger chain itself (any edited row breaks it); the run totals
    recomputed from the ledger; and the chain head recomputed from the
    ledger. The tier is a point-in-time claim sealed by the signature —
    lifetime spend only grows, so it is not recomputed here.
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
    expected = _sign(cert_key(store), payload_b64)
    if not hmac.compare_digest(expected, sig_b64):
        return False, "signature mismatch"
    ok, bad_id = verify_chain(store)
    if not ok:
        return False, f"ledger chain broken at row {bad_id}"
    contract_id = payload.get("contract_id")
    totals = run_totals(store, contract_id)
    if payload.get("tokens") != totals["tokens"]:
        return False, "run totals do not match the ledger (tokens)"
    cost = payload.get("cost_usd")
    actual_cost = totals["cost_usd"]
    if (cost is None) != (actual_cost is None) or (
        cost is not None and abs(float(cost) - float(actual_cost)) > 1e-9
    ):
        return False, "run totals do not match the ledger (cost_usd)"
    if payload.get("chain_head") != _chain_head(store, contract_id):
        return False, "chain head does not match the ledger"
    return True, payload
