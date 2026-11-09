"""Regression tests for the tightening pass — Unit B (chain + standing).

One test per review finding the spec assigns to chain.py / standing.py,
each docstring naming its finding (source reports in
~/workspace/mm-review2-2026-11-08/):

- T1 unified totals — security F5, correctness F6, claims #2
- T2 provenance counting + excluded totals — security F2
- T4 certificate high-water mark + verify parity — security F3/F7,
  correctness F2/F10, host F2
- T5 key handling — security F6/F9, host F1, correctness F8
- T6 no winner, no certificate — claims #1
- T8 money exactness + partial coverage — correctness F3/F4/F13

Round-4 fix pass additions (FIX-SPEC-ROUND4-2026-11-08, Unit 2):

- Pinned canonical variants + sealed_via inside the seal — round-3
  attack F3, fix spec G2
- Named-verdict verify (verdict_for) — round-3 regression F2
- Waiver/measured sealed into the payload — round-4 regression F3
- Issue refusals: zero counted rows, fractional totals — round-3
  attack F8, round-3 regression F5 (fix spec G4)
- Verify reasons carry figures — round-4 upgrade F4
- Key publish race convergence — round-3 regression F3 (fix spec G5)
"""

import base64
import inspect
import json
import multiprocessing
import os
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from hybrid_critic_loop import chain, standing
from hybrid_critic_loop.state import Store

# Tightening spec T2 lands add_ledger's provenance kwarg in the
# parallel state unit; these tests exercise the counted / not-counted
# provenance distinction only when the kwarg is present (before it
# lands, a row with no provenance reads as "legacy-import" and counts).
_HAS_PROVENANCE = (
    "provenance" in inspect.signature(Store.add_ledger).parameters)


def _prov(name):
    return {"provenance": name} if _HAS_PROVENANCE else {}


def _b64url_encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign_payload(store, payload):
    """A token for an arbitrary payload, signed with the store's key."""
    payload_b64 = _b64url_encode(
        json.dumps(payload, sort_keys=True,
                   separators=(",", ":")).encode("utf-8"))
    return f"mm1.{payload_b64}.{chain._sign(chain.cert_key(store), payload_b64)}"


class TighteningTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.db_path = self.data_dir / "data.db"
        self.store = Store(self.db_path)

    def tearDown(self):
        try:
            self.store.close()
        finally:
            self._tmp.cleanup()

    def _candidate(self, contract_id, candidate_id="cand_1",
                   provenance="executor-reported"):
        """Insert the candidate row a dispatch's ledger rows name.

        Counted-row rule v2 (fix spec, pinned interfaces) counts a
        non-'legacy-import' row only when its candidate exists — and,
        for adapter provenance strings, when the candidate's stored
        provenance equals the row's. The real loop creates the
        candidate before dispatching (loop.py), so the fixtures do
        too; before rule v2 landed, the bare 'cand_1' string sufficed.
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO candidates (id, contract_id,"
                " stage, slot, provenance, created_at) VALUES"
                " (?, ?, 'solve', '0', ?,"
                " '2026-01-01T00:00:00+00:00')",
                (candidate_id, contract_id, provenance))
            conn.commit()
        finally:
            conn.close()

    def _contract(self, contract_id="ctr_0001"):
        tid = self.store.create_target("do the thing", "code")
        self.store.create_contract(
            tid, stage="solve", task="fix the bug", domain="code",
            tags=[], acceptance=[], contract_id=contract_id)
        self._candidate(contract_id)
        return contract_id

    def _verdict(self, contract_id, winner="cand_1"):
        return self.store.create_verdict(
            contract_id=contract_id, attempt=1, winner=winner,
            eliminated=[], hard="pass", measured="pass", judged=None,
            escalated=winner is None,
            escalation_reason="no winner" if winner is None else None,
            waiver=None, human_gate="approved", tie_break=None)

    def _dispatch(self, contract_id, tokens_in=100, tokens_out=50,
                  cost_usd=1.5, expert="exp-1", provenance="executor-reported",
                  candidate_id="cand_1"):
        return self.store.add_ledger(
            "tgt_0001", contract_id, candidate_id, "solve", expert,
            tokens_in, tokens_out, cost_usd, "completed", None,
            **_prov(provenance))

    def _bookkeeping(self, contract_id, cost_usd=None,
                     provenance="internal"):
        return self.store.add_ledger(
            "tgt_0001", contract_id, None, "solve", "queue",
            None, None, cost_usd, "observed", "bookkeeping",
            **_prov(provenance))


# ---------------------------------------------------------------------------
# T1 — one totals implementation (security F5, correctness F6, claims #2)
# ---------------------------------------------------------------------------

class TestUnifiedTotals(TighteningTestCase):
    def test_all_surfaces_agree_on_one_store(self):
        """claims #2 / F5 / F6: chain.lifetime_spend, compute_standing
        and the per-contract totals are one aggregation — an 'observed'
        bookkeeping row carrying cost moves NONE of them (the old code
        had chain summing all rows, standing only generation rows, and
        the same store read two tiers)."""
        cid = self._contract()
        self._dispatch(cid, cost_usd=100.0)
        self._bookkeeping(cid, cost_usd=2_000_000.0)
        self.assertEqual(chain.lifetime_spend(self.store), 100.0)
        result = standing.compute_standing(self.store)
        self.assertEqual(result["cost_usd"], 100.0)
        self.assertEqual(result["tier"], "CLIENT")
        self.assertEqual(
            chain.run_totals(self.store, cid),
            {**standing.contract_totals(self.store, cid),
             "cost_rows": 1, "cost_missing": 0})
        # The uncounted money is named as excluded, not vanished.
        self.assertEqual(result["excluded_cost_usd"], 2_000_000.0)
        self.assertEqual(result["excluded_rows"], 1)

    @unittest.skipUnless(_HAS_PROVENANCE, "add_ledger provenance pending")
    def test_internal_and_unattributed_provenance_never_counts(self):
        """security F2: a front-door add_ledger with invented numbers
        under an internal or unattributed provenance raises no tier —
        the ledger now distinguishes who reported a row."""
        cid = self._contract()
        self._dispatch(cid, cost_usd=2_000_000.0, provenance="internal")
        self._dispatch(cid, cost_usd=2_000_000.0,
                       provenance="unattributed", expert="exp-2")
        self.assertIsNone(chain.lifetime_spend(self.store))
        result = standing.compute_standing(self.store)
        self.assertEqual(result["tier"], "WINDOW SHOPPER")
        self.assertEqual(result["excluded_cost_usd"], 4_000_000.0)
        self.assertEqual(result["excluded_rows"], 2)
        text = standing.standing_report(self.store)
        self.assertIn(
            "$4,000,000.00 across 2 rows not counted", text)
        self.assertIn("unattributed/internal writers", text)

    @unittest.skipUnless(_HAS_PROVENANCE, "add_ledger provenance pending")
    def test_counted_provenances_all_count(self):
        """Spec T2, as tightened by counted-row rule v2 (fix spec,
        pinned interfaces): executor-reported counts when its
        candidate exists; an adapter provenance string counts when it
        IS the candidate's stored provenance ('model-measured' is
        cand_2's); legacy-import counts outright. The excluded line
        stays out of the report when nothing is excluded."""
        cid = self._contract()
        self._dispatch(cid, cost_usd=10.0, provenance="executor-reported")
        self._candidate(cid, "cand_2", "model-measured")
        self._dispatch(cid, cost_usd=10.0, provenance="model-measured",
                       expert="exp-2", candidate_id="cand_2")
        self._dispatch(cid, cost_usd=10.0, provenance="legacy-import",
                       expert="exp-3")
        self.assertEqual(chain.lifetime_spend(self.store), 30.0)
        text = standing.standing_report(self.store)
        self.assertNotIn("not counted", text)


# ---------------------------------------------------------------------------
# T4 — high-water mark + verify parity (security F3/F7, correctness F2/F10,
# host F2)
# ---------------------------------------------------------------------------

class TestCertificateParity(TighteningTestCase):
    def _issued(self, contract_id="ctr_0001"):
        cid = self._contract(contract_id)
        self._dispatch(cid, 100, 50, 1.5)
        self._verdict(cid)
        cert = chain.issue_certificate(self.store, cid)
        self.assertIsNotNone(cert)
        return cid, cert

    def test_post_issuance_appends_still_verify(self):
        """correctness F2 / security F7: the certificate seals the
        prefix at its high-water mark — later bookkeeping rows AND
        later counted dispatches for the same contract no longer
        invalidate a genuine certificate."""
        cid, cert = self._issued()
        self._bookkeeping(cid)  # the route-hold style row that killed it
        self._dispatch(cid, 999, 999, 999.0, expert="exp-2")
        ok, payload = chain.verify_certificate(self.store, cert["cert"])
        self.assertTrue(ok, payload)

    def test_reissue_is_deterministic(self):
        cid, cert = self._issued()
        again = chain.issue_certificate(self.store, cid)
        self.assertEqual(again["cert"], cert["cert"])

    def test_forged_certificate_for_verdictless_contract_fails(self):
        """security F3: verify never checked a verdict existed — a
        correctly signed token naming a fake verdict id for a contract
        with totals of nothing verified clean. Now the named verdict
        must exist, match, and have a winner."""
        cid = self._contract()
        key = chain.cert_key(self.store)
        payload = {
            "contract_id": cid,
            "verdict_id": "vrd_9999",
            "tokens": None,
            "cost_usd": None,
            "tier": "WINDOW SHOPPER",
            "chain_head": None,
            "issued_at": "2026-01-01T00:00:00+00:00",
            "ledger_upto": 0,
            "kid": chain.key_id(key),
            "cost_rows": 0,
            "cost_missing": 0,
            "measured": "pass",
            "waived": False,
        }
        ok, reason = chain.verify_certificate(
            self.store, _sign_payload(self.store, payload))
        self.assertFalse(ok)
        self.assertIn("verdict", reason)

    def test_tier_above_current_standing_is_rejected(self):
        """security F3: the old docstring claimed the tier is not
        recomputed because 'lifetime spend only grows' — false:
        deleting trailing rows outside the sealed prefix keeps the
        chain and totals intact while the spend that justified the
        tier disappears. Verification now recomputes the tier."""
        rich = self._contract("ctr_rich")
        self._dispatch(rich, cost_usd=5.0)
        self._candidate("ctr_other", "cand_9", "executor-reported")
        big = self.store.add_ledger(
            "tgt_0001", "ctr_other", "cand_9", "solve", "exp-9",
            None, None, 2_500_000.0, "completed", None,
            **_prov("executor-reported"))
        self._verdict(rich)
        cert = chain.issue_certificate(self.store, rich)
        self.assertEqual(cert["payload"]["tier"], "MILLIONAIRE")
        ok, payload = chain.verify_certificate(self.store, cert["cert"])
        self.assertTrue(ok, payload)
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("DELETE FROM ledger WHERE id=?", (big,))
            conn.commit()
        finally:
            conn.close()
        ok, reason = chain.verify_certificate(self.store, cert["cert"])
        self.assertFalse(ok)
        self.assertIn("tier", reason)

    def test_lower_sealed_tier_is_legitimate_growth(self):
        """The other direction of the T4 tier rule: spend recorded
        after issuance raises the current tier; the older, lower
        sealed tier still verifies."""
        cid, cert = self._issued()
        self.assertEqual(cert["payload"]["tier"], "CLIENT")
        self._dispatch(cid, cost_usd=2_000_000.0, expert="exp-2")
        ok, payload = chain.verify_certificate(self.store, cert["cert"])
        self.assertTrue(ok, payload)

    def test_legacy_format_token_gets_the_legacy_reason(self):
        """Spec T4 pinned reason: a pre-tightening token (no
        ledger_upto / kid), even correctly signed, fails with the
        re-issue instruction — not a generic signature error."""
        cid, cert = self._issued()
        payload = dict(cert["payload"])
        del payload["ledger_upto"]
        del payload["kid"]
        del payload["cost_rows"]
        del payload["cost_missing"]
        ok, reason = chain.verify_certificate(
            self.store, _sign_payload(self.store, payload))
        self.assertFalse(ok)
        self.assertEqual(
            reason,
            "certificate predates the current format; "
            "re-issue it from the store")

    def test_other_store_reports_key_id_mismatch(self):
        """host F2: verifying against the WRONG store used to read as
        forgery ('signature mismatch'); the kid names it as a key
        mismatch instead — and since round-4 upgrade F4 the reason
        names the cert_key file and the restore remedy, because the
        common case is a book restored from backup WITHOUT its key,
        which is not a different store at all."""
        cid, cert = self._issued()
        with tempfile.TemporaryDirectory() as tmp:
            other = Store(Path(tmp) / "data.db")
            try:
                ok, reason = chain.verify_certificate(other, cert["cert"])
            finally:
                other.close()
        self.assertFalse(ok)
        self.assertEqual(
            reason,
            "the store's cert_key does not match the certificate's "
            "key id; if this book was restored from backup, restore "
            "its cert_key beside it")

    def test_malformed_payloads_never_verify(self):
        """correctness F10: signed empty / mistyped payloads verified
        as (True, ...) — the payload must now carry the full key set
        with sane types."""
        cid, cert = self._issued()
        base = dict(cert["payload"])
        cases = [
            {},
            {**base, "tokens": "many"},
            {**base, "tokens": [375]},
            {**base, "verdict_id": None},
            {**base, "cost_usd": float("nan")},
            {**base, "cost_rows": -1},
            {**base, "tier": "WHALE"},
            {**base, "chain_head": 42},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                ok, reason = chain.verify_certificate(
                    self.store, _sign_payload(self.store, payload))
                self.assertFalse(ok)
                self.assertEqual(reason, "malformed certificate payload")
        ok, reason = chain.verify_certificate(
            self.store, _sign_payload(self.store, [1, 2, 3]))
        self.assertFalse(ok)
        self.assertEqual(reason, "payload does not decode")


# ---------------------------------------------------------------------------
# T6 — no winner, no certificate (claims #1)
# ---------------------------------------------------------------------------

class TestNoWinnerNoCertificate(TighteningTestCase):
    def test_escalated_verdict_gets_no_certificate(self):
        """claims #1: an escalated run — every candidate eliminated,
        winner None — used to receive the MM CERTIFIED 'ONE ANSWER'
        stamp. issue_certificate now returns None for it."""
        cid = self._contract()
        self._dispatch(cid, 10, 5, 0.5)
        self._verdict(cid, winner=None)
        self.assertIsNone(chain.issue_certificate(self.store, cid))


# ---------------------------------------------------------------------------
# T5 — key handling (security F6/F9, host F1, correctness F8)
# ---------------------------------------------------------------------------

class TestKeyHandling(TighteningTestCase):
    def _issue_and_close(self):
        cid = self._contract()
        self._dispatch(cid, 10, 5, 0.5)
        self._verdict(cid)
        cert = chain.issue_certificate(self.store, cid)
        self.store.close()
        return cert

    def test_whitespace_ending_key_verifies_across_instances(self):
        """security F6: cert_key used to .strip() the key file — a
        generated key ending in a whitespace byte (~1 in 43 stores)
        read back one byte short in every later process, so its
        certificates verified only in the process that minted them."""
        key = bytes(range(1, 32)) + b"\n"  # 32 bytes, last is whitespace
        (self.data_dir / "cert_key").write_bytes(key)
        cid = self._contract()
        self._dispatch(cid, 10, 5, 0.5)
        self._verdict(cid)
        cert = chain.issue_certificate(self.store, cid)
        self.store.close()
        self.assertEqual((self.data_dir / "cert_key").read_bytes(), key)
        store2 = Store(self.db_path)
        try:
            self.assertEqual(chain.cert_key(store2), key)
            ok, payload = chain.verify_certificate(store2, cert["cert"])
            self.assertTrue(ok, payload)
        finally:
            store2.close()
            self.store = Store(self.db_path)

    def test_zero_byte_key_file_self_heals(self):
        """host F1: a zero-byte cert_key (crash between create and
        write) used to brick verification forever, reported as
        tampering. It is corrupt and is replaced atomically; new
        certificates verify, and old ones name the key change via the
        kid — not a false 'signature mismatch'."""
        cert = self._issue_and_close()
        (self.data_dir / "cert_key").write_bytes(b"")
        store2 = Store(self.db_path)
        try:
            key = chain.cert_key(store2)
            self.assertEqual(len(key), 32)
            self.assertEqual(
                len((self.data_dir / "cert_key").read_bytes()), 32)
            ok, reason = chain.verify_certificate(store2, cert["cert"])
            self.assertFalse(ok)
            self.assertEqual(
                reason,
                "the store's cert_key does not match the certificate's "
                "key id; if this book was restored from backup, restore "
                "its cert_key beside it")
        finally:
            store2.close()
            self.store = Store(self.db_path)

    def test_symlink_key_is_replaced_not_followed(self):
        """security F9: a symlink at the key path is corrupt — the key
        is replaced atomically (the link itself, not its target)."""
        target = self.data_dir / "elsewhere"
        target.write_bytes(b"x" * 32)
        os.symlink(target, self.data_dir / "cert_key")
        key = chain.cert_key(self.store)
        self.assertEqual(len(key), 32)
        self.assertFalse((self.data_dir / "cert_key").is_symlink())
        self.assertEqual(target.read_bytes(), b"x" * 32)
        self.assertEqual((self.data_dir / "cert_key").read_bytes(), key)

    def test_unpersistable_key_is_loud_not_ephemeral(self):
        """correctness F8 / host F1: when the key cannot be persisted,
        cert_key raises CertificateKeyError, issue returns None, and
        verify answers 'certificate key unavailable' — the old code
        silently fell back to an ephemeral key whose certificates
        verified only on the issuing instance."""
        blocker = self.data_dir / "blocker"
        blocker.write_bytes(b"not a directory")

        class FakeStore:
            db_path = str(blocker / "data.db")

            def ledger_rows(self):
                return []

            def latest_verdict(self, contract_id):
                return {"id": "vrd_1", "winner": "cand_1",
                        "created_at": "2026-01-01T00:00:00+00:00"}

        fake = FakeStore()
        with self.assertRaises(chain.CertificateKeyError):
            chain.cert_key(fake)
        self.assertIsNone(chain.issue_certificate(fake, "ctr_1"))
        payload = {
            "contract_id": "ctr_1", "verdict_id": "vrd_1",
            "tokens": None, "cost_usd": None, "tier": "WINDOW SHOPPER",
            "chain_head": None, "issued_at": "2026-01-01T00:00:00+00:00",
            "ledger_upto": 0, "kid": "0" * 16,
            "cost_rows": 0, "cost_missing": 0,
            "measured": None, "waived": False,
        }
        payload_b64 = _b64url_encode(
            json.dumps(payload, sort_keys=True,
                       separators=(",", ":")).encode("utf-8"))
        ok, reason = chain.verify_certificate(
            fake, f"mm1.{payload_b64}.sig")
        self.assertFalse(ok)
        self.assertEqual(reason, "certificate key unavailable")

    def test_memory_store_key_is_ephemeral_and_roundtrips(self):
        """Spec T5 carve-out: :memory: stores keep the per-instance
        ephemeral key, and their certificates verify on that instance.
        (The fake carries the post-round-4 surface — verdict_for and
        one counted, sealed ledger row — because G4's named-verdict
        verify and counted-rows issue rule require both.)"""

        class MemoryStore:
            db_path = ":memory:"

            def __init__(self):
                self._verdict = {
                    "id": "vrd_1", "winner": "cand_1",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "measured": "pass", "human_gate": "approved"}
                row = {
                    "id": 1, "target_id": "tgt_1",
                    "contract_id": "ctr_1", "candidate_id": "cand_1",
                    "stage": "solve", "expert_id": "exp-1",
                    "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.5,
                    "outcome": "completed", "note": None,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "provenance": "legacy-import",
                    "sealed_via": "append",
                    "prev_hash": chain.GENESIS_HASH,
                }
                row["row_hash"] = chain.row_hash(
                    chain.GENESIS_HASH, row)
                self._rows = [row]

            def ledger_rows(self):
                return list(self._rows)

            def latest_verdict(self, contract_id):
                return self._verdict

            def verdict_for(self, contract_id, verdict_id):
                if (contract_id == "ctr_1"
                        and verdict_id == self._verdict["id"]):
                    return self._verdict
                return None

        store = MemoryStore()
        cert = chain.issue_certificate(store, "ctr_1")
        self.assertIsNotNone(cert)
        ok, payload = chain.verify_certificate(store, cert["cert"])
        self.assertTrue(ok, payload)


# ---------------------------------------------------------------------------
# T8 — money exactness + partial coverage (correctness F3/F4)
# ---------------------------------------------------------------------------

class TestMoneyAndCoverage(TighteningTestCase):
    def test_cent_rounding_tiers_on_the_rounded_value(self):
        """correctness F4: 598.68 + 388.01 + 13.31 is exactly $1,000.00
        in decimal but 999.9999999999999 in float — the old standing
        tiered it CLIENT while printing $1,000.00. Aggregation now
        rounds to cents before tier math."""
        cid = self._contract()
        for i, cost in enumerate((598.68, 388.01, 13.31)):
            self._dispatch(cid, cost_usd=cost, expert=f"exp-{i}")
        result = standing.compute_standing(self.store)
        self.assertEqual(result["cost_usd"], 1000.0)
        self.assertEqual(result["tier"], "HIGH ROLLER")
        self.assertEqual(chain.lifetime_spend(self.store), 1000.0)

    def test_partial_coverage_is_rendered(self):
        """correctness F3: one $900 dispatch + one unreported dispatch
        used to read as an exact '$900.00 lifetime' with an exact
        '$100.00 more' gap. Coverage is now printed, and the gap reads
        'at least'."""
        cid = self._contract()
        self._dispatch(cid, cost_usd=900.0)
        self._dispatch(cid, cost_usd=None, expert="exp-2")
        result = standing.compute_standing(self.store)
        self.assertEqual(result["cost_usd"], 900.0)
        self.assertEqual(result["cost_rows"], 1)
        self.assertEqual(result["cost_missing"], 1)
        line = standing.standing_line(self.store)
        self.assertIn(
            "$900.00 lifetime measured spend "
            "(1 of 2 dispatches reported cost)", line)
        text = standing.standing_report(self.store)
        self.assertIn(
            "next tier: HIGH ROLLER — at least $100.00 "
            "more measured spend", text)

    def test_full_coverage_keeps_the_exact_wording(self):
        cid = self._contract()
        self._dispatch(cid, cost_usd=900.0)
        line = standing.standing_line(self.store)
        self.assertIn("$900.00 lifetime measured spend", line)
        self.assertNotIn("dispatches reported cost", line)
        text = standing.standing_report(self.store)
        self.assertIn(
            "next tier: HIGH ROLLER — $100.00 more measured spend", text)
        self.assertNotIn("at least", text)


# ---------------------------------------------------------------------------
# Round-4 fix pass — pinned canonical variants (G2; round-3 attack F3)
# ---------------------------------------------------------------------------

class TestRowHashVariants(unittest.TestCase):
    def _row(self, **overrides):
        row = {
            "id": 1, "target_id": "tgt_1", "contract_id": "ctr_1",
            "candidate_id": "cand_1", "stage": "solve",
            "expert_id": "exp-1", "tokens_in": 10, "tokens_out": 5,
            "cost_usd": 0.5, "outcome": "completed", "note": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "provenance": "executor-reported", "sealed_via": "append",
            "prev_hash": chain.GENESIS_HASH,
        }
        row.update(overrides)
        return row

    def test_field_sets_are_the_pinned_forms(self):
        """Fix-spec pinned interfaces: ``sealed_via`` is appended LAST
        to LEDGER_FIELDS; the legacy form excludes provenance AND
        sealed_via; the pre-sealed-via form is exactly the
        tightening-era field set Unit 1's sealing pass recognises."""
        self.assertEqual(chain.LEDGER_FIELDS[-1], "sealed_via")
        self.assertNotIn("sealed_via", chain.LEDGER_FIELDS_LEGACY)
        self.assertNotIn("provenance", chain.LEDGER_FIELDS_LEGACY)
        self.assertEqual(
            chain.LEDGER_FIELDS_PRE_SEALED_VIA,
            tuple(f for f in chain.LEDGER_FIELDS if f != "sealed_via"))
        self.assertIn("provenance", chain.LEDGER_FIELDS_PRE_SEALED_VIA)

    def test_variants_are_the_form_specific_hashes(self):
        row = self._row()
        variants = chain.row_hash_variants(row)
        self.assertEqual(set(variants),
                         {"current", "pre_sealed_via", "legacy"})
        self.assertEqual(
            variants["current"], chain.row_hash(chain.GENESIS_HASH, row))
        self.assertEqual(
            variants["pre_sealed_via"],
            chain.row_hash_pre_sealed_via(chain.GENESIS_HASH, row))
        self.assertEqual(
            variants["legacy"],
            chain.row_hash_legacy(chain.GENESIS_HASH, row))
        # sealed_via="append" is inside the current seal, so the forms
        # genuinely differ for this row (no degenerate coincidence).
        self.assertNotEqual(variants["current"],
                            variants["pre_sealed_via"])
        self.assertNotEqual(variants["pre_sealed_via"],
                            variants["legacy"])

    def test_sealed_via_relabel_breaks_the_chain(self):
        """round-3 attack F3: sealed_via sat OUTSIDE the canonical
        form, so one UPDATE relabelled every backfilled/repaired row
        as an append and erased the disclosure the round-2 fix rested
        on. The mark is sealed now — relabelling breaks the chain at
        that row."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data.db"
            store = Store(db_path)
            try:
                rid = store.add_ledger(
                    "tgt_1", "ctr_1", "cand_1", "solve", "exp-1",
                    10, 5, 0.5, "completed", None)
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"chain broke at {bad}")
                conn = sqlite3.connect(str(db_path))
                try:
                    conn.execute(
                        "UPDATE ledger SET sealed_via='repair'"
                        " WHERE id=?", (rid,))
                    conn.commit()
                finally:
                    conn.close()
                ok, bad = chain.verify_chain(store)
                self.assertFalse(ok)
                self.assertEqual(bad, rid)
            finally:
                store.close()


# ---------------------------------------------------------------------------
# Round-4 fix pass — named verdict (G4; round-3 regression F2)
# ---------------------------------------------------------------------------

class TestNamedVerdictCertificates(TighteningTestCase):
    def _verdict_attempt(self, contract_id, attempt, winner="cand_1"):
        return self.store.create_verdict(
            contract_id=contract_id, attempt=attempt, winner=winner,
            eliminated=[], hard="pass", measured="pass", judged=None,
            escalated=False, escalation_reason=None, waiver=None,
            human_gate="approved", tie_break=None)

    def test_attempt1_certificate_survives_attempt2_verdict(self):
        """round-3 regression F2 (also the round-4 test audit's
        named-verdict probe): verify compared the payload against
        latest_verdict, so recording an attempt-2 verdict orphaned
        the attempt-1 certificate with 'verdict does not match'.
        Verify now checks the NAMED verdict via verdict_for."""
        cid = self._contract()
        self._dispatch(cid, 100, 50, 1.5, provenance="legacy-import")
        v1 = self._verdict_attempt(cid, 1)
        cert1 = chain.issue_certificate(self.store, cid)
        self.assertIsNotNone(cert1)
        self.assertEqual(cert1["payload"]["verdict_id"], v1)
        ok, payload = chain.verify_certificate(self.store, cert1["cert"])
        self.assertTrue(ok, payload)
        v2 = self._verdict_attempt(cid, 2, winner="cand_2")
        self.assertNotEqual(v1, v2)
        ok, payload = chain.verify_certificate(self.store, cert1["cert"])
        self.assertTrue(ok, payload)
        # A certificate issued now names attempt 2, and also verifies.
        cert2 = chain.issue_certificate(self.store, cid)
        self.assertEqual(cert2["payload"]["verdict_id"], v2)
        self.assertNotEqual(cert2["cert"], cert1["cert"])
        ok, payload = chain.verify_certificate(self.store, cert2["cert"])
        self.assertTrue(ok, payload)

    def test_named_verdict_fields_compared_not_latest(self):
        """The comparison runs against the named row's own fields:
        attempt 1's id paired with attempt 2's created_at fails even
        though attempt 2 IS the contract's latest verdict."""
        cid = self._contract()
        self._dispatch(cid, 100, 50, 1.5, provenance="legacy-import")
        v1 = self._verdict_attempt(cid, 1)
        v2 = self._verdict_attempt(cid, 2, winner="cand_2")
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "UPDATE verdicts SET created_at=? WHERE id=?",
                ("2026-02-02T00:00:00+00:00", v2))
            conn.commit()
        finally:
            conn.close()
        cert = chain.issue_certificate(self.store, cid)
        self.assertEqual(cert["payload"]["verdict_id"], v2)
        payload = dict(cert["payload"])
        payload["verdict_id"] = v1  # attempt 1's id, attempt 2's stamp
        ok, reason = chain.verify_certificate(
            self.store, _sign_payload(self.store, payload))
        self.assertFalse(ok)
        self.assertEqual(reason, "verdict does not match the certificate")


# ---------------------------------------------------------------------------
# Round-4 fix pass — waiver in the payload (G4 pin; round-4 regression F3)
# ---------------------------------------------------------------------------

class TestWaiverPayload(TighteningTestCase):
    def _waived_verdict(self, contract_id):
        return self.store.create_verdict(
            contract_id=contract_id, attempt=1, winner="cand_1",
            eliminated=[], hard="pass", measured="fail", judged=None,
            escalated=False, escalation_reason=None,
            waiver={"offered": True, "granted": True},
            human_gate="waived", tie_break=None)

    def test_waived_verdict_seals_measured_and_waived(self):
        """round-4 regression F3: a winner that failed the measured
        gate and merged only by human waiver was certified
        indistinguishably from a clean pass. The payload now seals
        the gate outcome (fix-spec pin: ``measured`` + ``waived``)."""
        cid = self._contract()
        self._dispatch(cid, 100, 50, 1.5, provenance="legacy-import")
        self._waived_verdict(cid)
        cert = chain.issue_certificate(self.store, cid)
        self.assertIsNotNone(cert)
        self.assertEqual(cert["payload"]["measured"], "fail")
        self.assertIs(cert["payload"]["waived"], True)
        ok, payload = chain.verify_certificate(self.store, cert["cert"])
        self.assertTrue(ok, payload)

    def test_clean_pass_seals_waived_false(self):
        cid = self._contract()
        self._dispatch(cid, 100, 50, 1.5, provenance="legacy-import")
        self._verdict(cid)  # measured "pass", human_gate "approved"
        cert = chain.issue_certificate(self.store, cid)
        self.assertEqual(cert["payload"]["measured"], "pass")
        self.assertIs(cert["payload"]["waived"], False)

    def test_gate_outcome_change_after_issue_invalidates(self):
        """Verify compares measured/waived against the named verdict
        row: rewriting the recorded decision after issuance
        (waived → approved) breaks the certificate instead of letting
        the waiver be laundered out of the record."""
        cid = self._contract()
        self._dispatch(cid, 100, 50, 1.5, provenance="legacy-import")
        vid = self._waived_verdict(cid)
        cert = chain.issue_certificate(self.store, cid)
        self.store.set_verdict_human_gate(vid, "approved")
        ok, reason = chain.verify_certificate(self.store, cert["cert"])
        self.assertFalse(ok)
        self.assertEqual(reason, "verdict does not match the certificate")


# ---------------------------------------------------------------------------
# Round-4 fix pass — issue refusals (G4; round-3 attack F8, regression F5)
# ---------------------------------------------------------------------------

class TestIssueRefusals(TighteningTestCase):
    def _rewrite_tokens_and_reseal(self, row_id, tokens_in):
        """Rewrite one row's tokens_in via raw SQL and re-seal the
        whole chain — the pre-T7 book surgery round-3 regression F5
        describes (fractional counts legitimately sealed before
        add_ledger learned to validate them)."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "UPDATE ledger SET tokens_in=? WHERE id=?",
                (tokens_in, row_id))
            conn.commit()
        finally:
            conn.close()
        prev = chain.GENESIS_HASH
        conn = sqlite3.connect(str(self.db_path))
        try:
            for row in self.store.ledger_rows():
                digest = chain.row_hash(prev, row)
                conn.execute(
                    "UPDATE ledger SET prev_hash=?, row_hash=?"
                    " WHERE id=?", (prev, digest, row["id"]))
                prev = digest
            conn.commit()
        finally:
            conn.close()

    def test_zero_row_verdict_mints_nothing(self):
        """round-3 attack F8: contract B's $2M counted dispatch put
        the book at MILLIONAIRE; a verdict with a winner for a
        contract with NO ledger rows then minted a verifying
        certificate — empty prefix trivially seals, totals all
        'not reported', tier borrowed from B. Issue now requires
        counted rows behind the run."""
        rich = self._contract("ctr_rich")
        self._dispatch(rich, cost_usd=2_000_000.0,
                       provenance="legacy-import")
        empty = self._contract("ctr_empty")
        self._verdict(empty)
        self.assertIsNone(chain.issue_certificate(self.store, empty))
        # The rich contract's own certificate is unaffected.
        self._verdict(rich)
        self.assertIsNotNone(chain.issue_certificate(self.store, rich))

    def test_bookkeeping_only_contract_mints_nothing(self):
        """G4's counted-rows rule, second leg: the contract HAS rows
        (ledger_upto ≥ 1) but none are counted — an internal
        'observed' bookkeeping row is not a run."""
        cid = self._contract()
        self._bookkeeping(cid, cost_usd=5.0)
        self._verdict(cid)
        self.assertIsNone(chain.issue_certificate(self.store, cid))

    def test_fractional_token_total_mints_nothing(self):
        """round-3 regression F5: a counted row with tokens_in=100.5
        summed to a total the payload shape forbids (int or None) —
        issue minted a token verify then called 'malformed'. Issue
        now refuses instead of minting an unverifiable token."""
        cid = self._contract()
        rid = self._dispatch(cid, 100, 50, 1.5,
                             provenance="legacy-import")
        self._rewrite_tokens_and_reseal(rid, 100.5)
        self._verdict(cid)
        self.assertEqual(chain.run_totals(self.store, cid)["tokens"],
                         150.5)
        self.assertIsNone(chain.issue_certificate(self.store, cid))

    def test_integral_float_total_surfaces_as_int_and_issues(self):
        """G4's other half: 100.5 + 49.5 is a whole 150 — run_totals
        surfaces integral totals as ints and the certificate issues
        with an int payload (the F5 refusal is only for totals that
        are not integral)."""
        cid = self._contract()
        rid1 = self._dispatch(cid, 100, 0, 1.5,
                              provenance="legacy-import")
        rid2 = self._dispatch(cid, 0, 0, None, expert="exp-2",
                              provenance="legacy-import")
        self._rewrite_tokens_and_reseal(rid1, 100.5)
        self._rewrite_tokens_and_reseal(rid2, 49.5)
        totals = chain.run_totals(self.store, cid)
        self.assertEqual(totals["tokens"], 150)
        self.assertIsInstance(totals["tokens"], int)
        self._verdict(cid)
        cert = chain.issue_certificate(self.store, cid)
        self.assertIsNotNone(cert)
        self.assertEqual(cert["payload"]["tokens"], 150)
        self.assertIsInstance(cert["payload"]["tokens"], int)
        ok, payload = chain.verify_certificate(self.store, cert["cert"])
        self.assertTrue(ok, payload)


# ---------------------------------------------------------------------------
# Round-4 fix pass — verify reasons with figures (G4; round-4 upgrade F4)
# ---------------------------------------------------------------------------

class TestVerifyReasonsWithFigures(TighteningTestCase):
    def test_totals_mismatch_names_sealed_recomputed_and_bounds(self):
        """round-4 upgrade F4: a twin book (same key, fewer/different
        rows — the honest behind-book case) got the bare 'run totals
        do not match', indistinguishable from tampering and with no
        numbers to act on. The reason now carries the sealed figure,
        this book's recomputed figure, the certificate's ledger_upto
        and this book's own high-water mark."""
        cid = self._contract()
        self._dispatch(cid, 100, 50, 1.5, provenance="legacy-import")
        vid = self._verdict(cid)
        cert = chain.issue_certificate(self.store, cid)
        created_at = self.store.verdict_for(cid, vid)["created_at"]
        key_bytes = (self.data_dir / "cert_key").read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            twin_dir = Path(tmp)
            (twin_dir / "cert_key").write_bytes(key_bytes)
            twin = Store(twin_dir / "data.db")
            try:
                tid = twin.create_target("do the thing", "code")
                twin.create_contract(
                    tid, stage="solve", task="fix the bug", domain="code",
                    tags=[], acceptance=[], contract_id=cid)
                twin.add_ledger(
                    "tgt_0001", cid, "cand_1", "solve", "exp-1",
                    90, 10, 1.5, "completed", None,
                    **_prov("legacy-import"))
                twin_vid = twin.create_verdict(
                    contract_id=cid, attempt=1, winner="cand_1",
                    eliminated=[], hard="pass", measured="pass",
                    judged=None, escalated=False, escalation_reason=None,
                    waiver=None, human_gate="approved", tie_break=None)
                self.assertEqual(twin_vid, vid)  # same sequential id
                conn = sqlite3.connect(str(twin_dir / "data.db"))
                try:
                    conn.execute(
                        "UPDATE verdicts SET created_at=? WHERE id=?",
                        (created_at, twin_vid))
                    conn.commit()
                finally:
                    conn.close()
                ok, reason = chain.verify_certificate(twin, cert["cert"])
            finally:
                twin.close()
        self.assertFalse(ok)
        self.assertIn("seals 150", reason)
        self.assertIn("counts 100", reason)
        self.assertIn("up to 1", reason)        # certificate ledger_upto
        self.assertIn("runs to row 1", reason)  # the twin's max row id


# ---------------------------------------------------------------------------
# Round-4 fix pass — key race (G5; round-3 regression F3)
# ---------------------------------------------------------------------------

def _key_race_child(db_path, barrier, queue):
    """One racing first-opener: build a bare store handle and take the
    key at the barrier release, so both children enter cert_key's
    creation path together."""
    store = types.SimpleNamespace(db_path=str(db_path))
    barrier.wait(timeout=30)
    queue.put(chain.cert_key(store).hex())


class TestKeyRace(unittest.TestCase):
    def test_publish_is_only_if_absent(self):
        """G5, unit level: the second publisher adopts the first
        key — its own candidate never lands in the file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cert_key"
            first = chain._publish_key_file(path, b"a" * 32)
            self.assertEqual(first, b"a" * 32)
            second = chain._publish_key_file(path, b"b" * 32)
            self.assertEqual(second, b"a" * 32)
            self.assertEqual(path.read_bytes(), b"a" * 32)

    def test_replace_adopts_the_files_final_content(self):
        """G5, corrupt path: a healer whose replace is overtaken by a
        racing healer's later replace adopts the file's final
        content, not its own candidate — the two converge."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cert_key"
            path.write_bytes(b"bad")
            winner = b"w" * 32
            real_write = chain._write_key_file

            def racing_write(p, _key):
                real_write(p, winner)  # the other healer lands last

            with mock.patch.object(chain, "_write_key_file",
                                   racing_write):
                adopted = chain._replace_key_file(path, b"o" * 32)
            self.assertEqual(adopted, winner)
            self.assertEqual(path.read_bytes(), winner)

    def test_two_processes_first_open_converge(self):
        """round-3 regression F3, cross-process (acceptance asks for
        a real race): two processes first-opening a fresh store at
        the same moment must end up holding the SAME key — the one in
        the file. The tightening pass's unconditional os.replace let
        each process keep its own key for its lifetime, and its
        certificates then failed on its OWN store as foreign (kid
        mismatch)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data.db"
            ctx = multiprocessing.get_context("fork")
            barrier = ctx.Barrier(2)
            queue = ctx.Queue()
            procs = [
                ctx.Process(target=_key_race_child,
                            args=(db_path, barrier, queue))
                for _ in range(2)
            ]
            for proc in procs:
                proc.start()
            keys = sorted(queue.get(timeout=60) for _ in procs)
            for proc in procs:
                proc.join(timeout=60)
                self.assertEqual(proc.exitcode, 0)
            self.assertEqual(keys[0], keys[1])
            self.assertEqual(
                (Path(tmp) / "cert_key").read_bytes().hex(), keys[0])

    def test_corrupt_key_heals_to_one_key_across_instances(self):
        """After one instance heals a corrupt key file, a second
        instance reads the healed key — no second replacement, no
        divergence between instances."""
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "cert_key"
            key_path.write_bytes(b"short")
            first = types.SimpleNamespace(
                db_path=str(Path(tmp) / "data.db"))
            healed = chain.cert_key(first)
            self.assertEqual(len(healed), 32)
            second = types.SimpleNamespace(
                db_path=str(Path(tmp) / "data.db"))
            self.assertEqual(chain.cert_key(second), healed)
            self.assertEqual(key_path.read_bytes(), healed)


if __name__ == "__main__":
    unittest.main()
