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
"""

import base64
import inspect
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

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

    def _contract(self, contract_id="ctr_0001"):
        tid = self.store.create_target("do the thing", "code")
        self.store.create_contract(
            tid, stage="solve", task="fix the bug", domain="code",
            tags=[], acceptance=[], contract_id=contract_id)
        return contract_id

    def _verdict(self, contract_id, winner="cand_1"):
        return self.store.create_verdict(
            contract_id=contract_id, attempt=1, winner=winner,
            eliminated=[], hard="pass", measured="pass", judged=None,
            escalated=winner is None,
            escalation_reason="no winner" if winner is None else None,
            waiver=None, human_gate="approved", tie_break=None)

    def _dispatch(self, contract_id, tokens_in=100, tokens_out=50,
                  cost_usd=1.5, expert="exp-1", provenance="executor-reported"):
        return self.store.add_ledger(
            "tgt_0001", contract_id, "cand_1", "solve", expert,
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
        """Spec T2: executor-reported, candidate provenance strings and
        legacy-import count; the excluded line stays out of the report
        when nothing is excluded."""
        cid = self._contract()
        self._dispatch(cid, cost_usd=10.0, provenance="executor-reported")
        self._dispatch(cid, cost_usd=10.0, provenance="model-measured",
                       expert="exp-2")
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
        forgery ('signature mismatch'); the kid names it as a
        different store instead."""
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
            "certificate was issued by a different store "
            "(key id mismatch)")

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
                "certificate was issued by a different store "
                "(key id mismatch)")
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
        ephemeral key, and their certificates verify on that instance."""

        class MemoryStore:
            db_path = ":memory:"

            def __init__(self):
                self._verdict = {"id": "vrd_1", "winner": "cand_1",
                                 "created_at": "2026-01-01T00:00:00+00:00"}

            def ledger_rows(self):
                return []

            def latest_verdict(self, contract_id):
                return self._verdict

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


if __name__ == "__main__":
    unittest.main()
