# Fix spec — Owner decisions (2026-11-09)

Dino ruled on the four OWNER-DECISIONS-MM items on 2026-11-09 (chat,
after Muse's plain-language walkthrough). This pass implements the
rulings. Source document: PC `~/Desktop/claude
build/OWNER-DECISIONS-MM-2026-11-08.md` (options/evidence there).

## D1 — Item 1 = Option A (Claude's suggestion, verbatim)

"Demote = move the loser behind every seat that passed the deciding
gate; never promote a seat that failed it; a waiver demotes nobody —
the human accepted the measured failure."

Implementation consequences (from the decisions doc, verified
2026-11-08):
- `router.apply_lesson_route_change` must know the deciding gate's
  results: promotion only ever selects a seat that PASSED the
  deciding gate; a demotion reorders the loser behind all
  gate-passing seats (defect 1: no unchecked compete_with
  promotion).
- `state.apply_route_demotion`: the lineup can never end with
  `primary == compete_with` (defect 2 guard).
- Demoting a seat that holds no lineup position is an honest no-op:
  no UPDATE with unchanged values masquerading as a change
  (defect 3) — either skip the write or record it as the no-op it
  is; the observable behavior must not claim a reshuffle happened.
- `lessons.derive_outcome_cause`: when the verdict was WAIVED
  (waiver_candidate set / the human accepted the measured failure),
  NO route_change is emitted — suppression, not a held row (that
  was option C; the owner chose A).

## D2a — Item 2(a) = B: a measured-only regression is NOT a valid contract

A candidate that passes the hard gates but regresses on the
measured gate (metric worse than baseline) no longer yields a valid
verdict with a waiver on offer. The measured regression fails the
contract: no waiver path for a measured-only regression. (Hard-gate
failures were already non-waivable; the waiver mechanism itself
stays for the cases it legitimately serves — what changes is that a
measured regression is a failure, not a waivable pass. If the
current code ties the waiver offer exclusively to measured
regressions, surface that in the unit report rather than silently
shrinking the waiver's domain beyond this ruling.)

## D2b — Item 2(b) = B: bracket-eliminated passers are NOT calibration negatives

In `matrix.compute_calibration`, a candidate that PASSED its gates
but was eliminated by the bracket (cut/step-down) must not count as
a negative example when scoring judges. Only genuine failures
(gate-failed candidates, and the existing hard negatives) count as
negatives. Pin the mixed-book numbers: the round-6/7 seam family
(fam-x: judged-pass graded n stays as established for true
negatives) — construct the calibration fixtures so a bracket-cut
passer and a gate-failed candidate sit side by side and only the
failure moves the grade.

## D3 — Item 3 = C (decided by Muse at the owner's delegation)

Both surfaces keep apply/dismiss for a held route change (CLI
`hermes critic gate` and the `critic_gate` tool) — current behavior.
**No code change.** Recorded here so the decision is on the books:
two surfaces because the owner works from both, and auto-apply (D)
would take the human out of a lineup change.

## D4 — Item 4 = delete (DONE by Muse, 2026-11-09)

`~/.hermes/plugin-data/hybrid-critic-loop/` on the PC — contents
verified first (data.db: exactly 5 lessons, 1 candidate, the Phase 0
test artifact as recorded), deleted on the owner's explicit
instruction, removal verified.

## Acceptance for the pass

Full suite green (existing tests that pin the OLD behavior — waiver
offered on measured regression, calibration counting eliminated
passers, unchecked promotion, waiver demotion — are updated to pin
the RULED behavior, with the ruling cited in the test), demo exit 0,
PC copy synced. The pin candidate moves to this pass's HEAD.
