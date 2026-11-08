"""Unit tests for the MM CERTIFIED certificate (chain.issue_certificate).

Covers: the stamp with cost (full form) and without cost (dollar segment
omitted, never estimated); a certificate verifies against its own store;
an edited payload fails on the signature; an edited ledger row fails;
no verdict means no certificate anywhere (issue returns None and the
verdict report carries no block); the local key file is created beside
the database with 0600 perms; the verify-cert CLI answers 0 / 1.

The local signature is a store-integrity seal only — these tests assert
integrity against the store's own book, nothing about public claims
(the hosted verification service is designed, not built).
"""

import base64
import json
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from hybrid_critic_loop import chain, cli, reports
from hybrid_critic_loop.state import Store


def _b64url_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class CertificateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.db_path = self.data_dir / "data.db"
        self.store = Store(self.db_path)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _contract(self, contract_id="ctr_0001"):
        tid = self.store.create_target("do the thing", "code")
        self.store.create_contract(
            tid, stage="solve", task="fix the bug", domain="code",
            tags=[], acceptance=[], contract_id=contract_id)
        return contract_id

    def _verdict(self, contract_id):
        return self.store.create_verdict(
            contract_id=contract_id, attempt=1, winner="cand_1",
            eliminated=[], hard="pass", measured="pass", judged=None,
            escalated=False, escalation_reason=None, waiver=None,
            human_gate="approved", tie_break=None)

    def _dispatch(self, contract_id, tokens_in, tokens_out, cost_usd,
                  expert="exp-1"):
        return self.store.add_ledger(
            "tgt_0001", contract_id, "cand_1", "solve", expert,
            tokens_in, tokens_out, cost_usd, "completed", None)


class TestStamp(CertificateTestCase):
    def test_stamp_with_cost_is_the_full_form(self):
        cid = self._contract()
        self._dispatch(cid, 100, 50, 1.5)
        self._dispatch(cid, 200, None, None, expert="exp-2")
        self._verdict(cid)
        cert = chain.issue_certificate(self.store, cid)
        self.assertIsNotNone(cert)
        self.assertEqual(
            cert["stamp"],
            "MM CERTIFIED · 350 TOKENS SPENT · $1.50 BURNED · ONE ANSWER")

    def test_stamp_without_cost_omits_the_dollar_segment(self):
        cid = self._contract()
        self._dispatch(cid, 40, 10, None)
        self._verdict(cid)
        cert = chain.issue_certificate(self.store, cid)
        self.assertIsNotNone(cert)
        self.assertEqual(
            cert["stamp"], "MM CERTIFIED · 50 TOKENS SPENT · ONE ANSWER")
        self.assertNotIn("$", cert["stamp"])
        self.assertIsNone(cert["payload"]["cost_usd"])

    def test_stamp_with_nothing_reported_names_it(self):
        cid = self._contract()
        self._dispatch(cid, None, None, None)
        self._verdict(cid)
        cert = chain.issue_certificate(self.store, cid)
        self.assertIn("TOKENS NOT REPORTED", cert["stamp"])
        self.assertNotIn("$", cert["stamp"])


class TestVerify(CertificateTestCase):
    def _issued(self):
        cid = self._contract()
        self._dispatch(cid, 100, 50, 1.5)
        self._dispatch(cid, 200, 25, 0.25, expert="exp-2")
        self._verdict(cid)
        return cid, chain.issue_certificate(self.store, cid)

    def test_certificate_verifies_against_its_store(self):
        cid, cert = self._issued()
        self.assertTrue(cert["cert"].startswith("mm1."))
        ok, payload = chain.verify_certificate(self.store, cert["cert"])
        self.assertTrue(ok, payload)
        self.assertEqual(payload["contract_id"], cid)
        self.assertEqual(payload["tokens"], 375)
        self.assertAlmostEqual(payload["cost_usd"], 1.75)
        self.assertEqual(payload["chain_head"],
                         self.store.ledger_rows()[-1]["row_hash"])

    def test_edited_payload_fails_the_signature(self):
        _, cert = self._issued()
        head, payload_b64, sig = cert["cert"].split(".")
        payload = json.loads(_b64url_decode(payload_b64))
        payload["tokens"] = 999999999
        forged_b64 = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")
        ).rstrip(b"=").decode("ascii")
        forged = f"{head}.{forged_b64}.{sig}"
        ok, reason = chain.verify_certificate(self.store, forged)
        self.assertFalse(ok)
        self.assertIn("signature", reason)

    def test_edited_ledger_row_fails(self):
        _, cert = self._issued()
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "UPDATE ledger SET tokens_in=tokens_in+1 WHERE id=1")
            conn.commit()
        finally:
            conn.close()
        ok, reason = chain.verify_certificate(self.store, cert["cert"])
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_malformed_token_fails(self):
        ok, reason = chain.verify_certificate(self.store, "not-a-cert")
        self.assertFalse(ok)
        self.assertTrue(reason)


class TestNoVerdictNoCertificate(CertificateTestCase):
    def test_issue_returns_none_without_a_verdict(self):
        cid = self._contract()
        self._dispatch(cid, 10, 5, 0.5)
        self.assertIsNone(chain.issue_certificate(self.store, cid))

    def test_verdict_report_carries_the_block_only_with_a_verdict(self):
        cid = self._contract()
        self._dispatch(cid, 10, 5, 0.5)
        self.assertNotIn("MM CERTIFIED", reports.verdict_report(
            self.store, cid))
        self._verdict(cid)
        text = reports.verdict_report(self.store, cid)
        self.assertIn("MM CERTIFIED · 15 TOKENS SPENT", text)
        self.assertIn("cert: mm1.", text)


class TestLocalKey(CertificateTestCase):
    def test_key_file_beside_the_database_with_0600(self):
        cid = self._contract()
        self._verdict(cid)
        chain.issue_certificate(self.store, cid)
        key_path = self.data_dir / "cert_key"
        self.assertTrue(key_path.exists())
        self.assertEqual(len(key_path.read_bytes()), 32)
        mode = stat.S_IMODE(key_path.stat().st_mode)
        self.assertEqual(mode, 0o600, oct(mode))


class TestVerifyCertCli(CertificateTestCase):
    def test_cli_valid_then_invalid(self):
        cid = self._contract()
        self._dispatch(cid, 10, 5, 0.5)
        self._verdict(cid)
        cert = chain.issue_certificate(self.store, cid)
        self.store.close()
        rc = cli.main(["verify-cert", cert["cert"],
                       "--data-dir", str(self.data_dir)])
        self.assertEqual(rc, 0)
        rc = cli.main(["verify-cert", cert["cert"] + "x",
                       "--data-dir", str(self.data_dir)])
        self.assertEqual(rc, 1)
        # Reopen so tearDown's close targets a live store.
        self.store = Store(self.db_path)


if __name__ == "__main__":
    unittest.main()
