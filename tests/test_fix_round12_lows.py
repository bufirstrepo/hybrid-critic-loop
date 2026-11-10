"""Round-12 fix pass, Unit L — the LOWs (attack A4/A5, gatecore
G1/G2/G3, store S5, and the S3 case variant around the N1 residual).

Spec: FIX-SPEC-ROUND12-2026-11-09.md §Unit L. Findings closed:

* attack A4 — the Item 1 = A route derivation's by-construction
  claim covered only the kinds in ``_ELIMINATED_KINDS``: a deciding
  row carrying an UNKNOWN kind string (``sensor_drift``, or the
  case variant ``Gate_Error``) was treated as gate-passed and its
  seat could be promoted. The derivation now inverts the burden of
  proof: any non-empty kind that is not one of the two documented
  later-stage eliminations (``judged``, ``bracket_cut``) counts as
  a gate failure.
* attack A5 — a non-dict ``evidence`` value on a deciding row
  crashed ``_write_lesson`` with AttributeError AFTER the verdict
  existed. The derivation's read is shape-coerced
  (``loop._row_evidence``); the malformed row reads as no evidence.
* gatecore G1 — the legacy judged retry ignored MAX_ATTEMPTS: a
  contract starting at attempt >= 3 dispatched one more full
  generation + judging wave ([3, 4], [5, 6]) while the generic
  branch starting at the same attempt dispatched none. CHOICE:
  align the judged branch with the ceiling — the ceiling is a
  property of the contract's attempt budget, not of the failure
  class; contracts.py validates a starting attempt only as >= 1,
  so a start at/above the cap is a legitimate input the retry
  machinery must honour the cap for. A judged failure at the cap
  escalates on the generic retry-cap terms.
* gatecore G2 — the legacy judged gate always shuffled at
  constant seed 0 (``run_judged_gate``'s default), while the
  matrix passes ``seed=attempt``. CHOICE: pass the attempt seed —
  no document anywhere names the constant as design, the matrix
  site is the tree's own convention for gate presentation seeds,
  and the constant made a retry re-judge the identical
  presentation (persisted ``position_shown`` could never vary).
* gatecore G3 — a recorded None duration raised TypeError at both
  tie-break sites (``durations.get(id, inf)`` returns None when
  the key is present). None now sorts slowest (+inf), like a
  missing entry, at ``bracket.rank_candidates`` and
  ``loop._pick_winner``. Shape hardening, stated as such: no
  in-tree executor records a None duration today.
* store S5 — ``verify_certificate``'s chain-failure reason had no
  older-form variant: "ledger chain broken at row 2" on a book
  every standing/report surface diagnoses "sealed under an older
  form". The reason now discriminates, outcome unchanged.
* store S3 (case variant, AROUND the documented N1 residual) —
  ``_normalized_commit`` stripped only a lowercase ``git:``
  prefix, so a baseline stored ``GIT:``/``Git:``-prefixed
  normalized to ``git:<sha>`` and never equalled the bare sha,
  slipping the verdict leg's baseline exclusion. JUDGMENT: fixed —
  the widening is prefix recognition only, inside the function's
  own documented contract ("a baseline recorded in git: form or
  uppercase is still the baseline"); the residual's own shape
  (lowercase ``git:``-form fabricated commit, unsettled book, no
  gate record) is untouched and pinned below as a control that
  still completes.

Deliberate non-scope (Unit L item 7), stated for the record:
``append_observation`` atomicity under a two-connection
interleave is NOT fixed — the store is single-writer by design
(one connection per book, the loop the sole writer in
production); it stands as a named residual. The router's
route-derived fallback for non-reporting store doubles is NOT
changed — the real store reports ``no_change_cause``; the
fallback's wording defects reach test doubles only.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hybrid_critic_loop import bracket, chain
from hybrid_critic_loop import loop as loop_mod
from hybrid_critic_loop.contracts import validate_contract
from hybrid_critic_loop.state import Store

from tests.test_fix_round11_loop import _gate_contract
from tests.test_fix_round4_loop import RealGitBase
from tests.test_loop_v03 import FakeGen, FakeJudge, LoopV03Base

SHA = "0123456789abcdef0123456789abcdef01234567"
FAB = "b" * 40


# ---------------------------------------------------------------------------
# attack A4 / A5 — direct _write_lesson drive (the probeA shape)
# ---------------------------------------------------------------------------

class _DerivationBase(RealGitBase):
    """Three completed deciding rows: exp-p (primary, the lesson's
    demote target via a failing measurement), exp-x (compete),
    exp-z (fallback, verdict winner). Kinds are planted through
    the loop's own channel, set_candidate_eliminated_kind."""

    def _build(self, cid, kinds):
        contract = _gate_contract(cid)
        self.store.upsert_route("code", "exp-p", "exp-x", ["exp-z"],
                                [], "seed")
        cids = {}
        for slot, eid in (("a", "exp-p"), ("b", "exp-x"), ("c", "exp-z")):
            cids[eid] = self.store.create_candidate(
                cid, 1, "implement", eid, f"fam-{eid[-1]}", slot,
                f"wt://{eid}", "basehash")
            self.store.update_candidate(cids[eid], status="completed")
            if kinds.get(eid):
                self.store.set_candidate_eliminated_kind(
                    cids[eid], kinds[eid])
        self.store.create_verdict(
            cid, 1, cids["exp-z"], [], "pass", "fail", None, False,
            None, None, "pending", None)
        gate_details = {
            "candidate_experts": {cids[e]: e for e in cids},
            "hard_failures": [],
            "measurements": [
                {"candidate_id": cids["exp-p"], "metric": "ops",
                 "value": 99, "op": "<=", "threshold": 8, "pass": False},
            ],
        }
        run = loop_mod._Run(self.store, contract, self.repo, {},
                            human_decider=None)
        return cids, run, gate_details

    def _primary(self):
        return self.store.get_route("code")["primary"]


class TestUnknownEliminationKind(_DerivationBase):
    def test_unknown_kind_counts_as_gate_failure(self):
        # probeA A3's exact shape: 'sensor_drift' is in no
        # registered class; before the fix exp-x was promoted.
        _cids, run, gd = self._build(
            "ctr_r12_unknown_kind", {"exp-x": "sensor_drift"})
        run._write_lesson(1, gd)
        self.assertEqual(self._primary(), "exp-z")
        self.assertNotEqual(self._primary(), "exp-x")

    def test_case_variant_kind_counts_as_gate_failure(self):
        # probeA A3b: exact-match frozenset, no normalization —
        # 'Gate_Error' is an unknown kind and fails the same way.
        _cids, run, gd = self._build(
            "ctr_r12_case_kind", {"exp-x": "Gate_Error"})
        run._write_lesson(1, gd)
        self.assertEqual(self._primary(), "exp-z")

    def test_registered_kind_still_counts_as_gate_failure(self):
        # Control: the registered classes behave exactly as before.
        _cids, run, gd = self._build(
            "ctr_r12_registered_kind", {"exp-x": "gate_error"})
        run._write_lesson(1, gd)
        self.assertEqual(self._primary(), "exp-z")

    def test_judged_kind_still_counts_as_passed(self):
        # Control: a judged elimination passed the decidable gates
        # and stays promotable — the inversion must not swallow
        # the two documented later-stage kinds.
        _cids, run, gd = self._build(
            "ctr_r12_judged_kind", {"exp-x": "judged"})
        run._write_lesson(1, gd)
        self.assertEqual(self._primary(), "exp-x")

    def test_bracket_cut_kind_still_counts_as_passed(self):
        _cids, run, gd = self._build(
            "ctr_r12_cut_kind", {"exp-x": "bracket_cut"})
        run._write_lesson(1, gd)
        self.assertEqual(self._primary(), "exp-x")


class TestNonDictEvidenceDoesNotCrash(_DerivationBase):
    def test_string_evidence_on_a_deciding_row(self):
        # probeA A5's exact shape: evidence='gate_error' (a bare
        # string) planted on the winner's row through the public
        # API (a clean row, no kind — the write is lawful; the
        # READ is what must not crash, after verdict creation).
        cids, run, gd = self._build(
            "ctr_r12_string_evidence", {"exp-x": "gate_error"})
        self.store.update_candidate(cids["exp-z"], evidence="gate_error")
        lesson_id = run._write_lesson(1, gd)
        self.assertIsNotNone(lesson_id)
        self.assertEqual(self._primary(), "exp-z")

    def test_list_evidence_on_a_deciding_row(self):
        cids, run, gd = self._build(
            "ctr_r12_list_evidence", {"exp-x": "gate_error"})
        self.store.update_candidate(cids["exp-z"], evidence=["gate_error"])
        lesson_id = run._write_lesson(1, gd)
        self.assertIsNotNone(lesson_id)
        self.assertEqual(self._primary(), "exp-z")


# ---------------------------------------------------------------------------
# gatecore G1 / G2 — legacy judged ceiling and seed
# ---------------------------------------------------------------------------

class TestLegacyJudgedCeilingAndSeed(LoopV03Base):
    def _run_legacy(self, attempt_start, cid="ctr_fake"):
        self.markers.update({"gen-a": "m-a", "gen-b": "m-b"})
        low = {"m-a": 3.0, "m-b": 3.0}
        executors = {
            "gen-a": FakeGen("gen-a", "fam-a", "m-a"),
            "gen-b": FakeGen("gen-b", "fam-b", "m-b"),
            "judge-x": FakeJudge("judge-x", "fam-x", low),
            "judge-y": FakeJudge("judge-y", "fam-y", low),
            "judge-z": FakeJudge("judge-z", "fam-z", low),
        }
        self.panel("gen-a", "gen-b", [], ["gen-a", "gen-b"])
        self.store.upsert_route(
            "code", "gen-a", "gen-b", [],
            ["judge-x", "judge-y", "judge-z"], "peer")
        raw = {
            "id": cid, "stage": "implement",
            "task": "Implement the fake thing.", "domain": "code.game",
            "tags": ["fake"], "attempt": attempt_start,
            "acceptance": [
                {"class": "hard", "cmd": "true", "expect": {"exit": 0}},
                {"class": "judged", "rubric": "code_quality_v1",
                 "min": 7, "judges": 3},
            ],
        }
        result = loop_mod.run_contract(
            self.store, validate_contract(raw), "repo://fake", executors,
            human_decider=lambda info: "approved")
        attempts = sorted(
            {c["attempt"] for c in self.store.candidates_for(cid)})
        return result, attempts, executors

    def test_start_at_cap_dispatches_no_judged_retry(self):
        # probe_bcd_e B3: starting at 3 used to dispatch [3, 4] —
        # one full generation + judging wave past the ceiling the
        # generic branch honours (probe_b5's [3]-only contrast).
        result, attempts, executors = self._run_legacy(3)
        self.assertEqual(result.get("status"), "escalated")
        self.assertEqual(attempts, [3])
        self.assertEqual(executors["judge-x"].calls, 1)
        self.assertIn("retry cap", result.get("detail") or "")

    def test_start_beyond_cap_dispatches_no_judged_retry(self):
        # probe_bcd_e B4: starting at 5 used to dispatch [5, 6].
        result, attempts, executors = self._run_legacy(5)
        self.assertEqual(result.get("status"), "escalated")
        self.assertEqual(attempts, [5])
        self.assertEqual(executors["judge-x"].calls, 1)

    def test_start_below_cap_retries_up_to_the_cap(self):
        # The judged retry itself is untouched below the ceiling:
        # start at 2 retries onto 3, then the spent judged retry
        # escalates — never a dispatch at attempt 4.
        result, attempts, executors = self._run_legacy(2)
        self.assertEqual(result.get("status"), "escalated")
        self.assertEqual(attempts, [2, 3])
        self.assertEqual(executors["judge-x"].calls, 2)

    def test_judged_gate_receives_the_attempt_seed(self):
        # The seed is captured at the loop -> judges boundary: the
        # legacy gate must receive seed=attempt on every attempt,
        # exactly as the matrix path passes it — not the constant
        # default 0 that made every attempt's presentation identical.
        seeds = []
        original = loop_mod.judges_mod.run_judged_gate

        def capturing(*args, **kwargs):
            seeds.append(kwargs.get("seed", 0))
            return original(*args, **kwargs)

        with mock.patch.object(
                loop_mod.judges_mod, "run_judged_gate", capturing):
            result, attempts, _executors = self._run_legacy(1)
        self.assertEqual(result.get("status"), "escalated")
        self.assertEqual(attempts, [1, 2])
        self.assertEqual(seeds, [1, 2])


# ---------------------------------------------------------------------------
# gatecore G3 — a recorded None duration sorts slowest, never TypeError
# ---------------------------------------------------------------------------

class TestNoneDurationTieBreak(unittest.TestCase):
    def test_bracket_none_duration_sorts_slowest(self):
        # probe_bcd_e C8's shape: durations={'known': None, ...}
        # raised TypeError before the fix.
        field = [
            {"id": "known", "tokens_in": 10, "tokens_out": 10},
            {"id": "miss", "tokens_in": 10, "tokens_out": 10},
            {"id": "zero", "tokens_in": 10, "tokens_out": 10},
        ]
        validity = {"known": 8.0, "miss": 8.0, "zero": 8.0}
        ranked = bracket.rank_candidates(
            field, validity,
            durations={"known": None, "miss": 5, "zero": 5})
        self.assertEqual([c["id"] for c in ranked],
                         ["miss", "zero", "known"])

    def test_bracket_recorded_zero_still_beats_none(self):
        field = [
            {"id": "none", "tokens_in": 10, "tokens_out": 10},
            {"id": "zero", "tokens_in": 10, "tokens_out": 10},
        ]
        validity = {"none": 8.0, "zero": 8.0}
        ranked = bracket.rank_candidates(
            field, validity, durations={"none": None, "zero": 0})
        self.assertEqual([c["id"] for c in ranked], ["zero", "none"])

    def test_pick_winner_none_duration_sorts_slowest(self):
        # probe_bcd_e C9's shape, at the loop sibling site.
        run = object.__new__(loop_mod._Run)
        run.durations = {"known": None, "miss": 5}
        survivors = [
            {"id": "known", "tokens_in": 10, "tokens_out": 10},
            {"id": "miss", "tokens_in": 10, "tokens_out": 10},
        ]
        winner, tie_break = run._pick_winner(survivors, {})
        self.assertEqual(winner["id"], "miss")
        self.assertEqual(tie_break, "wall_clock_ms")

    def test_pick_winner_all_none_does_not_raise(self):
        run = object.__new__(loop_mod._Run)
        run.durations = {"a": None, "b": None}
        survivors = [
            {"id": "a", "tokens_in": 10, "tokens_out": 10},
            {"id": "b", "tokens_in": 10, "tokens_out": 10},
        ]
        winner, _tb = run._pick_winner(survivors, {})
        self.assertEqual(winner["id"], "a")


# ---------------------------------------------------------------------------
# store S5 — verify_certificate names an older-form book correctly
# ---------------------------------------------------------------------------

class TestVerifyCertificateOlderFormReason(unittest.TestCase):
    """The p5b_verify_olderform shape: a certificate issued on a
    current-form book, then an interior row re-sealed under the
    legacy form (and its successor re-linked). Every standing /
    report surface diagnoses the older form; the certificate
    verifier's chain reason must too."""

    def _issued_book(self, tmp):
        s = Store(Path(tmp) / "data.db")
        tid = s.create_target("do the thing", "code")
        s.create_contract(tid, stage="solve", task="fix", domain="code",
                          tags=[], acceptance=[], contract_id="ctr_0001")
        s._conn.execute(
            "INSERT OR REPLACE INTO candidates (id, contract_id, stage,"
            " slot, provenance, baseline_hash, created_at) VALUES"
            " ('cand_1', 'ctr_0001', 'solve', '0', 'executor-reported',"
            " ?, '2026-01-01T00:00:00+00:00')", ("0" * 40,))
        s._conn.commit()
        s.add_ledger(tid, "ctr_0001", "cand_1", "solve", "exp-1",
                     100, 50, 1.5, "completed", None,
                     provenance="executor-reported")
        s.create_verdict(contract_id="ctr_0001", attempt=1,
                         winner="cand_1", eliminated=[], hard="pass",
                         measured="pass", judged=None, escalated=False,
                         escalation_reason=None, waiver=None,
                         human_gate="approved", tie_break=None)
        s.add_ledger(tid, "ctr_0001", "cand_1", "solve", "exp-1",
                     10, 5, 1.0, "completed", None,
                     provenance="executor-reported")
        s.add_ledger(tid, "ctr_0001", "cand_1", "solve", "exp-1",
                     10, 5, 1.0, "completed", None,
                     provenance="executor-reported")
        s.create_final_answer(tid, "ctr_0001", f"git:{SHA}",
                              human_gate="approved")
        s.append_observation("ctr_0001", {
            "kind": "gate_completion", "decision": "approved",
            "merge_source": "verdict_snapshot", "commit": SHA,
            "channel": "test"})
        s.set_contract_status("ctr_0001", "completed")
        cert = chain.issue_certificate(s, "ctr_0001")
        self.assertIsNotNone(cert)
        return s, cert["cert"]

    def test_older_form_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            s, token = self._issued_book(tmp)
            self.assertTrue(chain.verify_certificate(s, token)[0])
            rows = s.ledger_rows()
            db = Path(tmp) / "data.db"
            s.close()
            conn = sqlite3.connect(str(db))
            r2 = dict(rows[1])
            h2 = chain.row_hash_legacy(r2["prev_hash"], r2)
            conn.execute("UPDATE ledger SET row_hash=? WHERE id=2", (h2,))
            r3 = dict(rows[2])
            r3["prev_hash"] = h2
            h3 = chain.row_hash(h2, r3)
            conn.execute(
                "UPDATE ledger SET prev_hash=?, row_hash=? WHERE id=3",
                (h2, h3))
            conn.commit()
            conn.close()
            s = Store(db)
            self.addCleanup(s.close)
            ok, reason = chain.verify_certificate(s, token)
            self.assertFalse(ok)
            self.assertEqual(
                reason, "ledger sealed under an older form at row 2")

    def test_genuinely_broken_reason_unchanged(self):
        # Control: a tampered row matching NO historical variant is
        # still "chain broken" — the discrimination is by form, not
        # a blanket rename.
        with tempfile.TemporaryDirectory() as tmp:
            s, token = self._issued_book(tmp)
            self.assertTrue(chain.verify_certificate(s, token)[0])
            s._conn.execute(
                "UPDATE ledger SET tokens_in = tokens_in + 1 WHERE id=2")
            s._conn.commit()
            ok, reason = chain.verify_certificate(s, token)
            self.assertFalse(ok)
            self.assertEqual(reason, "ledger chain broken at row 2")


# ---------------------------------------------------------------------------
# store S3 — the GIT: case variant around the documented N1 residual
# ---------------------------------------------------------------------------

class TestNormalizedCommitCaseVariant(unittest.TestCase):
    def test_prefix_recognized_case_insensitively(self):
        self.assertEqual(chain._normalized_commit("GIT:" + SHA), SHA)
        self.assertEqual(chain._normalized_commit("Git:" + SHA), SHA)
        self.assertEqual(
            chain._normalized_commit(" GIT:" + SHA.upper() + " "), SHA)
        # Controls: the forms that already normalized still do.
        self.assertEqual(chain._normalized_commit("git:" + SHA), SHA)
        self.assertEqual(chain._normalized_commit(SHA.upper()), SHA)
        self.assertEqual(chain._normalized_commit(SHA), SHA)

    def _crash_book(self, baseline, row_commit):
        """p2_norm's crash-window shape: claimed verdict, answer
        row, NO gate record, status unsettled — the baseline
        exclusion is the only leg that can refuse when the row
        commit equals the winner's baseline."""
        s = Store(":memory:")
        self.addCleanup(s.close)
        tid = s.create_target("t", "code")
        s.create_contract(tid, "solve", "task", "code", [], [],
                          contract_id="ctr_1")
        s._conn.execute(
            "INSERT OR REPLACE INTO candidates (id, contract_id, stage,"
            " slot, provenance, baseline_hash, created_at) VALUES"
            " ('cand_1', 'ctr_1', 'solve', '0', 'executor-reported', ?,"
            " '2026-01-01T00:00:00+00:00')", (baseline,))
        s._conn.commit()
        s.create_verdict(contract_id="ctr_1", attempt=1, winner="cand_1",
                         eliminated=[], hard="pass", measured="pass",
                         judged=None, escalated=False,
                         escalation_reason=None, waiver=None,
                         human_gate="approved", tie_break=None)
        s.create_final_answer(tid, "ctr_1", row_commit,
                              human_gate="approved")
        return s

    def test_uppercase_prefix_baseline_is_still_the_baseline(self):
        for baseline in ("GIT:" + SHA, "Git:" + SHA):
            s = self._crash_book(baseline, row_commit="git:" + SHA)
            self.assertFalse(
                chain._contract_completed(s, "ctr_1"), baseline)

    def test_documented_n1_residual_shape_still_completes(self):
        # Control: the residual itself is NOT closed by this fix —
        # winner-bound + lowercase git:-form + fabricated full-length
        # commit + no gate record on an unsettled book still
        # completes via the verdict leg, exactly as documented.
        s = self._crash_book("0" * 40, row_commit="git:" + FAB)
        self.assertTrue(chain._contract_completed(s, "ctr_1"))


if __name__ == "__main__":
    unittest.main()
