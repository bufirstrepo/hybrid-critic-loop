# FIX SPEC — ROUND 12 (2026-11-09)
Tree: 31a8c3e. Source: mm-review12-consolidated-2026-11-09.md
(1 HIGH — a pin gap, no live defect; 5 MEDIUM; 15 LOW). Units commit
separately, in order T, F, D, L. Every unit: full suite green before
commit; mutation spot-check (sever the fix, the unit's pins must
fail; restore byte-identical); TMPDIR under ~/workspace/.tmp-r12fix.

## UNIT T — pins for the attempt scoping (HIGH testaudit F1)
Files: tests only (test_fix_round11_loop.py or a new round-12 file).
The deciding-attempt scoping at loop.py:2241-2245 (deciding_rows via
candidates_for(attempt=deciding_attempt)) is load-bearing and
unpinned: dropping the attempt= filter survives the targeted set,
and the mutant misbehaves live (attempt-1-only passer promoted;
pass-then-fail seat promoted over the sole deciding passer).
Add discriminating end-to-end pins for BOTH shapes against the real
derivation, plus a direct-drive pin if the shapes cannot be made
single-confound end-to-end. Acceptance: with the attempt= filter
removed (mutation applied by hand), the new pins FAIL; restored,
green. Also close testaudit's secondary pin gaps where they are
cheap: the M1a pin's store-shape predicate half; the matrix shuffle
site pinned by behavior, not source-text assertion; the three
nominally multi-confounded leg-1 plants get single-confound siblings
if Unit F does not already cover them.

## UNIT F — gate-record surface hardening (attack F1/F2/F3, store F1)
Files: hybrid_critic_loop/state.py, loop.py (+ tests).
1. EVIDENCE WRITE PATH (attack F1): the derivation reads elimination
   kinds from candidate-row evidence, and update_candidate accepts
   evidence= writes with no protection — a caller can clear a
   gate_error kind (reviving a gate-killed seat) or forge kind="hard"
   on the sole passer. Give the elimination record the write-once /
   loop-only protection provenance got in round 5: public
   update_candidate must not be able to set or clear the elimination
   kind on a candidate row; the loop's own marking path keeps working
   (route it through the protected channel provenance uses, or an
   equivalent guard). Pins: both attack directions refused at the API.
2. EXEMPTION BINDING (attack F2 + store F1, convergent): the FIFO
   exemption keys on the bare kind string, so planted
   gate_completion records defeat the 50-cap AND genuine
   gate_failure / gate_answer_refused records remain evictable (the
   eviction of a gate_failure flips _recorded_gate_decision
   approved -> None and silently defeats the retry refusal at
   loop.py:2444-2452). Fix both halves: (a) the exemption covers the
   gate-record family — gate_completion, gate_failure,
   gate_answer_refused (every record chain/loop gate logic reads as
   evidence); (b) the exemption cannot be used to grow the log
   without bound — cap exempt records per contract at the same 50
   (oldest exempt beyond the cap may trim; genuine records are
   written once per gate by construction, so a >50 exempt log is a
   plant by definition). Pins: the lane probes — 60 planted records
   no longer defeat the cap; 50 appends cannot evict a gate_failure;
   _recorded_gate_decision survives the flood.
3. DEDUPE ON CONTENT (attack F3): the convergence path at
   loop.py:751 dedupes gate_completion on kind alone, so one stray
   record (wrong commit) suppresses the genuine merge record. Dedupe
   on (kind, commit, decision): a prior record suppresses the write
   only when it records the SAME commit and decision; a stray with a
   different commit does not. Pin: the lane's exact shape — stray
   first, genuine merge still recorded, certificate still issues and
   verifies.

## UNIT D — documents (documents F1 + LOWs)
Files: SUBMISSION.md, README.md, hybrid_critic_loop/bracket.py
(docstring only).
1. SUBMISSION.md:22 validate delta: the self-referential count
   breaks every round. Rewrite anchored and permanently true:
   state the count for the range ending at THIS UNIT'S PARENT
   commit, in the form "N commits after the validated v0.3 tree,
   through <parent short sha>" — re-derive N by rev-list at fix
   time. (Unit D runs third; its parent is Unit F's commit. The
   sentence must be true of the tree it ships in: the documents
   commit itself is a docs-only commit after the named endpoint,
   and the sentence says "through <parent>", so it stays accurate.)
2. Video beats: the two demo runs are presented out of order —
   resequence the beats to the demo's actual run order (read
   demo/run_demo.py output; do not paraphrase from memory).
3. bracket.py module docstring: "earlier completion" -> the
   corrected "shorter generation duration" wording (the README line
   was fixed in round 11; its origin docstring was not).

## UNIT L — LOWs (attack, gatecore, store leftovers)
1. Unknown elimination kinds: the derivation's by-construction claim
   covers kinds in _ELIMINATED_KINDS only; a row carrying an UNKNOWN
   kind string is currently treated as passed. Treat any non-empty
   elimination kind on a deciding row as gate-failed (the row was
   eliminated by something; the burden of proof is on passing).
   Pin it.
2. Non-dict evidence on a candidate row must not crash _write_lesson
   after verdict creation — coerce/guard at the read, pin the shape.
3. Legacy judged retry vs MAX_ATTEMPTS: a contract starting at
   attempt >= 3 must not exceed the ceiling via the judged path —
   align the judged branch with the ceiling or document why the
   ceiling does not apply there, with evidence; pin the chosen
   behavior.
4. Legacy judged gate seed: the judged gate receives the attempt
   seed, not constant 0 — or the constant is shown to be the
   documented design; pin the chosen behavior.
5. Recorded None duration at the tie-break sites: +inf handling must
   treat None as missing (no TypeError) at both sites; pin it. (No
   in-tree executor produces one today — this is shape hardening,
   stated as such.)
6. verify_certificate older-form reason: where the completion check
   or chain check names "chain broken" for an older-form book, use
   the older-form wording (same class as round-11 store F1).
7. NOT in scope (recorded, deliberately): append_observation
   atomicity under two-connection interleave (single-writer design;
   document as a named residual in the consolidated fix report if
   the unit agrees, else escalate); _normalized_commit GIT: case
   variant (a plant AROUND the documented N1 residual — the residual
   itself is unchanged and not re-reported; fixing the case variant
   is allowed only if it does not touch the residual's semantics —
   judge and state); router fallback for non-reporting doubles
   (real store unaffected; test-double shape only).

## OUT OF SCOPE / residuals (unchanged)
The documented N1 residual stands. Owner exclusions untouched.
lessons_for cost residual stands as documented.

## ACCEPTANCE (whole pass)
- Full suite green (795 + new pins) in the units' runs AND in
  Muse's independent run before the pass is reported.
- Round-10 probes, round-11 probes (incl. probe_gateerror), and the
  round-12 lane probes behave as specified.
- PC sync of the final HEAD; new pin candidate stated; round 13
  follows.
