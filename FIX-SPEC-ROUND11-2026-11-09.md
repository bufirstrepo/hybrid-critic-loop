# FIX SPEC — ROUND 11 (2026-11-09)
Tree: cc727b5. Source: mm-review11-consolidated-2026-11-09.md (3 HIGH,
9 MEDIUM, 14 LOW; all HIGHs and MEDIUMs reproduced by the lanes;
HIGH-1 and MEDIUM-1's mechanism read personally by Muse in the code).
Units commit separately. Every unit: full suite green before commit;
mutation spot-check per unit (sever the fix, the unit's new pins must
fail); TMPDIR under ~/workspace/.tmp-r11fix for all runs.

## UNIT A — gate_passed derivation completeness (HIGH-1, HIGH-2, store F2)
File: hybrid_critic_loop/loop.py (_write_lesson derivation + the
dissent re-run refresh path).
1. The deciding attempt's failed set must subtract EVERY elimination
   class the gate stack produces, not only hard rows and failing
   measurement rows: candidates eliminated at "gate_error" and at
   "provenance" on the deciding attempt are gate-FAILED. Source of
   truth: the candidate rows' own elimination records for the
   deciding attempt (the kinds _mark_eliminated persists:
   _ELIMINATED_KINDS) — derive from the rows, so a future elimination
   class is excluded by construction rather than by enumeration.
   Judged-eliminated candidates STAY in the set (they passed the
   decidable gates; M1's design, unchanged).
2. The measurement predicate mirrors lessons.derive_outcome_cause:
   a row fails when pass is False OR ("pass" absent AND passed is
   False). Both shapes, one predicate, shared if a helper exists.
3. The dissent re-run refresh (loop.py ~1544-1563) applies the same
   complete derivation after a re-run elimination.
Pins: end-to-end probes for gate_error and provenance eliminations
(a gate-killed seat never lands primary ahead of the sole passer);
a mutation pin that fails when ONLY the measurements half of the
subtraction is removed (HIGH-2's exact mutant); a raw-shape
(passed-only) measurement pin.

## UNIT B — N1 leg-1 hardening + pins (HIGH-3, MEDIUM-1/2/3, testaudit F4/F5/F6/F7, attack F4/F5/F6/F7)
Files: hybrid_critic_loop/chain.py (_contract_completed,
verify_certificate), hybrid_critic_loop/state.py (observation
capacity), callers that read final_answers.
1. LEG 1 (gate-record) gains leg 2's discipline: the observation's
   decision must be in _GATE_DECISION_VALUES (approved/waived — the
   answer-producing decisions a gate writes); the row's commit must
   be git:-form, full 40-char lowercase hex, and EQUAL the
   observation's bare sha (this equality is HIGH-3's pin); the
   contract status must be settled (completed) — leg 1 exists to
   corroborate the loop's own completed writes. The "banana" /
   rejected / None / non-sha plants must all read NOT completed.
2. FIFO (MEDIUM-2): the gate_completion record must not live only in
   the 50-entry observation FIFO. Give completion records a durable
   home that is not capacity-evicted (e.g. excluded from the FIFO
   trim, or a dedicated record the chain reads) — pick the smallest
   change that makes 50 front-door appends unable to revoke a genuine
   completion; pin it with the lane's exact probe.
3. VERIFY (MEDIUM-3): verify_certificate additionally requires the
   contract to read completed under _contract_completed at verify
   time; a book whose answer/gate record was deleted after issuance
   no longer verifies (reason names the failed completion check).
4. PINS (testaudit F3-F7): decision-match pin; winner-contract pin
   rebuilt on a fixture that discriminates (not confounded by the
   baseline leg); settled-status pin likewise; kind-check pin; the
   m3c control rebuilt single-confound.
5. LOWs in scope: F4 — the duck-typed error path must not resurrect
   status-only completion (a raising final_answers read degrades to
   NOT completed, never to the status leg); F5 — settled-status
   comparison normalized (strip/case) at the guard, or status writes
   validated against the known set — pick one, pin the "escalated "
   shape; F6 — the loop's final-answer reader and chain's read the
   SAME row (latest by rowid, one helper); F7 — baseline exclusion
   compares normalized commits (strip git: prefix, lowercase) on
   both sides.

## UNIT C — documents (MEDIUM documents F1/F2/F3 + standing item (b) fix)
Files: README.md, SUBMISSION.md.
1. README certificate section: the demo's local solvers report
   NEITHER tokens nor cost (the stamp's honest shape is "totals are
   the run's own ledger rows" — correct the rationale sentence to
   the measured fact; check demo output, do not paraphrase).
2. SUBMISSION validate line: replace the "two passes" delta with the
   true one (48 commits after the validated v0.3 tree 3923dad —
   re-derive the count at fix time and write the number with its
   range), and promote the re-run-validate instruction to its own
   unchecked checklist item in Step 1 (not a trailing clause).
3. SUBMISSION video beats: remove the dissent-flag/drift promises
   the shipped demo never produces (demo verdicts carry dissent: []
   in both runs); beats describe only what demo/run_demo.py prints.

## UNIT D — LOWs (store F1/F3, gatecore F2/F3/F4, documents LOWs, performance F1)
1. reports.py:750 passes the store to _unavailable_line (older-form
   diagnosis consistent across surfaces).
2. router honest-record branch: distinguish "no passer exists" from
   "order already lawful" in the line AND the ledger note.
3. Legacy judged gate retry count matches its spec (once, not twice)
   — or the spec text is corrected to the code if the code is the
   intended behavior; state which, with evidence.
4. Bracket tie-break: a missing duration is not 0 ms (treat as
   slowest / +inf, consistent with _tokens_total's convention).
5. Matrix/judges shuffle: replace hash((seed, judge_id)) with a
   stable hash (hashlib on the string form) so a seeded shuffle is
   reproducible across processes; pin with a two-process check.
6. lessons_for superlinear cost (perf F1): document as a named
   residual with the measured trajectory, OR fix if the fix is a
   bounded change (index/limit pushdown) — unit decides by size and
   states which; no silent scope growth.
7. Documents LOWs from lane 2's list: correct each at string level.

## OUT OF SCOPE / residuals (unchanged)
The documented N1 residual (winner-bound + git:-form + fabricated
full-length commit + no gate record on an unsettled book) stands —
Unit B's leg-1 discipline does not claim to close it; if a unit
believes it has, it must say so explicitly and prove it against
m3b.py. Owner exclusions untouched (W/L + validity banking before
the human gate; judging cost into the ledger). Named residuals list
unchanged otherwise.

## ACCEPTANCE (whole pass)
- Full suite green (743 + new pins) in the unit's run AND in Muse's
  independent run before the pass is reported.
- Round-10 probes (probe3.py, m3b.py) and round-11 lane probes all
  still behave as specified.
- PC sync of the final HEAD; new pin candidate stated.
