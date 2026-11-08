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

**The certificate.** A run that reached a verdict with a winner is issued
a certificate: the ``MM CERTIFIED`` stamp plus a signed token
``mm1.<base64url(payload)>.<base64url(sig)>``. The payload names the
contract, the verdict, the run's totals *as recorded in the ledger* at
issuance, the operator's tier at issue time, the run's cost coverage
(``cost_rows`` / ``cost_missing``), the id of the store key that signed
it (``kid``), and the chain head at the run's high-water mark
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
    "CertificateKeyError",
    "canonical_row",
    "row_hash",
    "verify_chain",
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


def verify_chain(store, upto_id=None):
    """Recompute the chain from genesis.

    Returns ``(True, None)`` when every row in scope seals, else
    ``(False, first_bad_id)`` — the id of the first row whose stored
    ``prev_hash`` is not the previous row's hash, whose stored
    ``row_hash`` does not recompute from its own fields, or that carries
    no hashes at all. With ``upto_id``, only rows with
    ``id <= upto_id`` are in scope (prefix verification — a certificate
    verifies the book as it stood at issuance, tightening spec T4).
    """
    prev = GENESIS_HASH
    for row in store.ledger_rows():
        if upto_id is not None:
            rid = row.get("id")
            if not isinstance(rid, int) or isinstance(rid, bool):
                continue
            if rid > upto_id:
                continue
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
    try:
        dir_fd = os.open(str(key_path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass  # the directory fsync is best-effort durability


def cert_key(store) -> bytes:
    """The store's LOCAL HMAC key, generated once and cached.

    File-backed stores keep it at ``<data dir>/cert_key`` beside the
    database; a ``:memory:`` store gets an ephemeral per-instance key.
    A missing key file is created atomically; an existing file that is
    not exactly 32 raw bytes — empty, truncated, overlong — or a
    symlink at the path is corrupt and is replaced atomically with a
    fresh key (certificates signed under a corrupt key were
    unverifiable anyway). A file-backed store whose key cannot be
    persisted raises :class:`CertificateKeyError` — there is NO
    ephemeral fallback (tightening spec T5). This key is a
    store-integrity seal only — see the module docstring for what it
    does not prove.
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
            key = None
            if not key_path.is_symlink():
                data = _read_key_file(key_path)
                if data is not None and len(data) == _KEY_LEN:
                    key = data
            if key is None:
                key = os.urandom(_KEY_LEN)
                _write_key_file(key_path, key)
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
    return {
        "tokens": totals["tokens"],
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


def _chain_head(store, contract_id, upto_id=None):
    """The row_hash of the contract's last ledger row in scope, or None."""
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


def issue_certificate(store, contract_id):
    """Issue the certificate block for a contract, or None.

    A certificate exists only for a run that reached a verdict with a
    winner — a contract with no verdict row, or an escalated verdict
    whose winner is None, gets None, and no stamp is printed for it
    anywhere (tightening spec T6; claims #1). Also None when the
    store's certificate key cannot be persisted
    (:class:`CertificateKeyError`): no certificate beats one that can
    never verify. Returns ``{"stamp", "cert", "payload"}``. The
    payload's ``issued_at`` is the verdict row's own timestamp, so the
    same run renders the same token every time.
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
    """The tightening-era payload keys, present and sanely typed."""
    if not _is_int(payload.get("ledger_upto")) or payload["ledger_upto"] < 0:
        return False
    if not isinstance(payload.get("kid"), str) or not payload["kid"]:
        return False
    for field in ("cost_rows", "cost_missing"):
        if not _is_int(payload.get(field)) or payload[field] < 0:
            return False
    return True


def verify_certificate(store, token):
    """Verify a certificate token against this store.

    Returns ``(True, payload)`` or ``(False, reason)``. Checks, in
    order: the token's shape; the payload decodes to a dict carrying
    the full key set with sane types (else "malformed certificate
    payload" — an empty, mistyped or field-less payload never verifies,
    correctness F10); the legacy-format check (a token without
    ``ledger_upto`` / ``kid`` predates the current format); the key id
    (a mismatch means the certificate was issued by a different store,
    named as such — host F2); the HMAC signature under the store's
    local key; the named verdict — it must be the contract's verdict,
    with matching id and ``created_at``, and a winner (a certificate
    for a contract with no such verdict is a forgery, security F3);
    the chain prefix up to ``ledger_upto``; the run totals and chain
    head recomputed over that prefix; and the tier, recomputed from
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
    if "ledger_upto" not in payload or "kid" not in payload:
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
            "certificate was issued by a different store (key id mismatch)"
        )
    expected = _sign(key, payload_b64)
    if not hmac.compare_digest(expected, sig_b64):
        return False, "signature mismatch"
    contract_id = payload["contract_id"]
    verdict = store.latest_verdict(contract_id)
    if (
        verdict is None
        or verdict.get("winner") is None
        or verdict.get("id") != payload["verdict_id"]
        or verdict.get("created_at") != payload["issued_at"]
    ):
        return False, "verdict does not match the certificate"
    upto = payload["ledger_upto"]
    ok, bad_id = verify_chain(store, upto_id=upto)
    if not ok:
        return False, f"ledger chain broken at row {bad_id}"
    totals = run_totals(store, contract_id, upto_id=upto)
    if payload["tokens"] != totals["tokens"]:
        return False, "run totals do not match the ledger (tokens)"
    cost = payload["cost_usd"]
    actual_cost = totals["cost_usd"]
    if (cost is None) != (actual_cost is None) or (
        cost is not None and abs(float(cost) - float(actual_cost)) > 1e-9
    ):
        return False, "run totals do not match the ledger (cost_usd)"
    if payload["chain_head"] != _chain_head(store, contract_id, upto_id=upto):
        return False, "chain head does not match the ledger"
    current_tier = tier_for(lifetime_spend(store))
    if _TIER_RANK[payload["tier"]] < _TIER_RANK[current_tier]:
        return False, "certificate tier is above the store's current tier"
    return True, payload
