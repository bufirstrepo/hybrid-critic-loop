"""Regression tests for the tightening pass — Unit C (surfaces, wiring,
docs) plus the T12 canonical-form seal fix.

Each test names the review finding it pins (source reports in
~/workspace/mm-review2-2026-11-08/; spec: TIGHTENING-SPEC-2026-11-08.md):

- T2 call sites — security F2: no in-tree add_ledger call is left
  implicit; dispatch rows carry the candidate's provenance and
  bookkeeping rows carry "internal".
- T6 loop part — claims #1: an escalated (winnerless) run result
  carries no certificate stamp or token.
- T3 reports part — correctness F1/F9 surface: the ledger report names
  the sealing routes when any non-"append" rows exist.
- T9 CLI — correctness F11/F12, host F5: read-only commands never
  create a store; session-start gates its fallback on has_standing.
- T9 docstrings — host F4/F6: plugin docstring says two hooks;
  resolve_data_dir docstring names step 1 a forward-compat probe.
- T12 — provenance is sealed: flipping it breaks verify_chain at that
  row; a store sealed under the pre-T12 canonical form opens, re-seals
  as "upgrade" (round-4 spec G2), verifies, and still
  issues/verifies certificates.
"""

import inspect
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from hybrid_critic_loop import chain, cli, plugin
from hybrid_critic_loop.reports import ledger_report
from hybrid_critic_loop.state import Store


def _dispatch(store, contract_id="ctr_0001", cost_usd=1.5,
              provenance="executor-reported"):
    return store.add_ledger(
        "tgt_0001", contract_id, "cand_1", "solve", "exp-1",
        100, 50, cost_usd, "completed", None, provenance=provenance)


def _verdict(store, contract_id="ctr_0001", winner="cand_1"):
    return store.create_verdict(
        contract_id=contract_id, attempt=1, winner=winner, eliminated=[],
        hard="pass", measured="pass", judged=None,
        escalated=winner is None,
        escalation_reason="no winner" if winner is None else None,
        waiver=None, human_gate="approved", tie_break=None)


class TestCallSiteProvenance(unittest.TestCase):
    """T2 (security F2): every add_ledger call site in the package
    passes an explicit provenance — grep the sources, don't trust the
    runtime."""

    def test_no_implicit_add_ledger_call_sites(self):
        package = Path(inspect.getfile(Store)).parent
        offenders = []
        for path in sorted(package.glob("*.py")):
            if path.name == "state.py":
                continue  # defines add_ledger; doesn't call it
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                if "add_ledger(" not in line or "def add_ledger" in line:
                    continue
                # The call may span lines: scan to its closing paren.
                depth = 0
                block = []
                for j in range(i, len(lines)):
                    block.append(lines[j])
                    depth += lines[j].count("(") - lines[j].count(")")
                    if depth <= 0:
                        break
                if "provenance" not in "\n".join(block):
                    offenders.append(f"{path.name}:{i + 1}")
        self.assertEqual(offenders, [])


class TestEscalateCarriesNoCertificate(unittest.TestCase):
    """T6 (claims #1): loop._escalate no longer attaches a certificate —
    the escalated result has no stamp and no token. (issue_certificate
    returning None for winnerless verdicts is Unit B's pin; this is the
    loop-side call removal.)"""

    def test_escalate_source_has_no_attach_call(self):
        import hybrid_critic_loop.loop as loop_mod

        source = inspect.getsource(loop_mod._Run._escalate)
        self.assertNotIn("_attach_certificate", source)


class TestLedgerReportChainLine(unittest.TestCase):
    """T3 reports part: the chain line appears only when non-"append"
    sealed_via rows exist, with per-route totals; NULL sealed_via rows
    count as appends."""

    def _store_with_rows(self, tmp, sealed_vias):
        db_path = Path(tmp) / "data.db"
        store = Store(db_path)
        for _ in sealed_vias:
            _dispatch(store)
        store.close()
        conn = sqlite3.connect(str(db_path))
        try:
            for row_id, via in enumerate(sealed_vias, start=1):
                conn.execute(
                    "UPDATE ledger SET sealed_via=? WHERE id=?",
                    (via, row_id))
            conn.commit()
        finally:
            conn.close()
        return Store(db_path)

    def test_all_append_rows_print_no_chain_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store_with_rows(tmp, ["append", "append"])
            try:
                text = ledger_report(store)
            finally:
                store.close()
        self.assertNotIn("chain:", text)

    def test_null_sealed_via_counts_as_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store_with_rows(tmp, [None, None])
            try:
                text = ledger_report(store)
            finally:
                store.close()
        self.assertNotIn("chain:", text)

    def test_non_append_rows_print_chain_line_with_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store_with_rows(
                tmp, ["append", "backfill", "backfill", "repair"])
            try:
                text = ledger_report(store)
            finally:
                store.close()
        self.assertIn("chain: append=1, backfill=2, repair=1", text)


class TestReadOnlyCommandsNeverCreate(unittest.TestCase):
    """T9 (correctness F11): standing / verify-cert / session-start on
    a data dir with no data.db exit 1 with a clear error and create
    nothing — not even the directory."""

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_read_only_commands_on_missing_store(self):
        cases = [
            ["standing"],
            ["verify-cert", "mm1.aaa.bbb"],
            ["session-start"],
        ]
        for argv in cases:
            with tempfile.TemporaryDirectory() as tmp:
                missing = Path(tmp) / "no-such-dir"
                code, _out, err = self._run(
                    argv + ["--data-dir", str(missing)])
                self.assertEqual(code, 1, argv)
                self.assertIn("no store found", err, argv)
                self.assertFalse(missing.exists(), argv)

    def test_standing_on_existing_empty_store_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.db")
            store.close()
            code, out, _err = self._run(["standing", "--data-dir", tmp])
            self.assertEqual(code, 0)
            self.assertIn("standing", out)


class TestSessionStartGate(unittest.TestCase):
    """T9 (correctness F12, host F5): the CLI session-start fallback
    gates on has_standing — an existing but empty store prints no
    standing line (the pre-tightening fallback printed one)."""

    def test_empty_store_session_start_prints_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data.db")
            store.close()
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["session-start", "--data-dir", tmp])
            self.assertEqual(code, 0)
            self.assertEqual(out.getvalue().strip(), "")


class TestDocstringHonesty(unittest.TestCase):
    """T9 (host F4/F6): the docstrings name the real registration —
    two lifecycle hooks, and resolve_data_dir's step 1 is a
    forward-compat probe, not the host's authoritative source."""

    def test_plugin_docstring_says_two_hooks(self):
        self.assertIn("two", plugin.__doc__)
        self.assertIn("lifecycle hooks", plugin.__doc__)
        self.assertNotIn("four lifecycle hooks", plugin.__doc__)

    def test_resolve_data_dir_docstring_names_probe(self):
        doc = plugin.resolve_data_dir.__doc__
        self.assertIn("forward-compat", doc)
        self.assertIn("plugin_storage", doc)
        self.assertIn("authoritative in-host source", doc)


class TestProvenanceSealedInChain(unittest.TestCase):
    """T12: provenance is part of the canonical form — flipping a
    sealed row's provenance breaks verify_chain at that row, like any
    other sealed-field edit."""

    def test_provenance_flip_breaks_chain_at_that_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data.db"
            store = Store(db_path)
            try:
                _dispatch(store)
                _dispatch(store)
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"chain broke at {bad}")
            finally:
                store.close()
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "UPDATE ledger SET provenance='internal' WHERE id=2")
                conn.commit()
            finally:
                conn.close()
            store = Store(db_path)
            try:
                ok, bad = chain.verify_chain(store)
                self.assertFalse(ok)
                self.assertEqual(bad, 2)
            finally:
                store.close()


class TestLegacyFormStoreSurvives(unittest.TestCase):
    """T12 compatibility: a store sealed under the OLD canonical form
    (LEDGER_FIELDS without provenance) opens, re-seals those rows as
    sealed_via="upgrade" (round-4 fix spec G2 — the canonical-form
    cascade is an upgrade seal, distinct from genuine "repair" suffix
    seals), verifies under the new form, and its certificates still
    issue and verify."""

    def _old_form_store(self, tmp):
        """A file-backed store whose ledger is sealed with legacy
        hashes, built through the public API then re-hashed by hand."""
        db_path = Path(tmp) / "data.db"
        store = Store(db_path)
        # Counted-row rule v2 (round-4 fix spec, pinned interfaces):
        # a dispatch row counts only when its candidate exists with a
        # matching stored provenance — the real loop creates the
        # candidate before dispatching, so the fixture does too.
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO candidates (id, contract_id,"
                " stage, slot, provenance, created_at) VALUES"
                " ('cand_1', 'ctr_0001', 'solve', '0',"
                " 'executor-reported', '2026-01-01T00:00:00+00:00')")
            conn.commit()
        finally:
            conn.close()
        _dispatch(store, cost_usd=2.5)
        _dispatch(store, cost_usd=3.5)
        head = _verdict(store)
        # Round-5 integration: issuance is completion-gated (spec H1)
        # — record the completion the loop would have written.
        store.create_final_answer(
            "repo", "ctr_0001", "deadbeef")
        store.set_contract_status("ctr_0001", "completed")
        cert = chain.issue_certificate(store, "ctr_0001")
        self.assertIsNotNone(cert)
        store.close()
        # Re-seal every row under the legacy form and restore the
        # append marker, exactly as a pre-T12 build left the book.
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM ledger ORDER BY id ASC").fetchall()
            prev = chain.GENESIS_HASH
            for row in rows:
                entry = dict(row)
                digest = chain.row_hash_legacy(prev, entry)
                conn.execute(
                    "UPDATE ledger SET prev_hash=?, row_hash=?,"
                    " sealed_via='append' WHERE id=?",
                    (prev, digest, row["id"]))
                prev = digest
            conn.commit()
        finally:
            conn.close()
        return db_path, head, cert

    def test_old_form_store_reseals_and_certifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _head, old_cert = self._old_form_store(tmp)
            store = Store(db_path)
            try:
                self.assertEqual(store.chain_repairs, 0)
                self.assertEqual(store.chain_upgrades, 2)
                self.assertEqual(store.chain_backfilled, 0)
                rows = store.ledger_rows()
                self.assertEqual(
                    [r["sealed_via"] for r in rows], ["upgrade", "upgrade"])
                ok, bad = chain.verify_chain(store)
                self.assertTrue(ok, f"chain broke at {bad}")
                # A certificate issues and verifies against the
                # re-sealed book. (The token issued BEFORE the upgrade
                # sealed the legacy-form chain head, whose value the
                # re-seal necessarily changed; it no longer verifies
                # and is re-issued from the store, like any pre-format
                # token.)
                fresh = chain.issue_certificate(store, "ctr_0001")
                self.assertIsNotNone(fresh)
                valid, payload = chain.verify_certificate(
                    store, fresh["cert"])
                self.assertTrue(valid, payload)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
