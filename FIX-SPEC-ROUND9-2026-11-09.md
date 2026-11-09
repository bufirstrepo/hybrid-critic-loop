# Fix spec — Round 9 (2026-11-09)

Consolidated findings: goal files
`mm-review9-consolidated-2026-11-09.md`; lane reports in
`~/workspace/mm-review9-2026-11-09/`. Base HEAD `697f5f8`. Every
item below was reproduced by Muse personally (HIGHs, MEDIUM) or
mutation-verified by the test-audit lane (test gaps).

## M1 — gate_passed must be scoped to the deciding attempt's evaluated candidates (HIGH; loop.py)

In `Loop._write_lesson`, the passed set is currently
`candidate_experts` minus the deciding attempt's failed ids.
`candidate_experts` spans all attempts and includes seats that
never generated. Correct definition: the experts whose candidates
**on the deciding attempt** were actually gate-evaluated and did
not fail — i.e. candidate ids from
`store.candidates_for(contract_id)` with `attempt` equal to the
verdict's attempt and `status == "completed"` (the idiom the
verdict builder itself uses for the deciding attempt's scored
candidates), mapped through `candidate_experts`, minus the
experts of the deciding attempt's hard-failed and
measurement-failed candidates. Judged-eliminated candidates stay
in the set (they passed the deciding gates; lane 2 verified this
is the ruling's letter).

Regression tests (loop-level, real runs — these also close the
test-audit lane's wire gap): (a) two-attempt run — a seat
hard-failing BOTH attempts must not be promotable; the deciding
attempt's passer takes primary when the primary is demoted;
(b) unavailable seat in the panel — never in the passed set,
never promoted; (c) single-attempt control — today's correct
behavior unchanged. A mutation check the parent will run: sever
the wire (`gate_passed=None`) and these tests must fail.

## M2 — pin D2a at the matrix-fallback site (HIGH test gap; tests only)

Add the missing loop-level pin: a validity-matrix contract where
the matrix passes nobody and a measured-eliminated seat exists —
the run escalates, the verdict carries `waiver=None`, mirroring
`test_measured_regression_ends_failed_with_no_waiver` for the
anchor path. No production-code change is expected; if building
the shape reveals the offer is actually reachable, STOP and
report instead of fixing inside this item.

## M3 — _contract_completed must not rest on row existence alone (MEDIUM; chain.py)

`chain._contract_completed` returns True when a `final_answers`
row exists, even for a contract whose gate never decided
(escalated, `human_gate='not_reached'`). On the real Store, the
row leg must corroborate with the contract's gate state: a final
answer counts toward completion only when the contract's own
record is consistent with a gate decision having produced it
(status completed, or the latest verdict's human_gate decided in
a way the gate machinery itself converges). A planted/stray row
on an escalated or undecided contract must read NOT completed;
genuine completions — including crash-converged ones — must stay
completed. The documented duck-typed degradation for test doubles
exposing no completion surface may remain, as documented. Pins:
the lane-3 P3 shape (escalated + planted row → not completed;
issuance refuses) and the genuine/converged shapes staying True.

## M4 — route demotions preserve the seat set; records tell the truth (MEDIUM ×2 + LOWs; state.py, router.py)

Invariant for EVERY demotion mode (legacy and passed): the
route's seat set is preserved — every seat present before the
demotion is present after, exactly once (demotion reorders; it
never duplicates or deletes). Concretely: legacy promotion of a
fallback member moves it (no duplicate); legacy promotion of a
non-route expert must not evict `compete_with` (the evicted seat
keeps a fallback place, and the result/ledger line records what
actually happened). The passed-mode rebuild keeps the same
invariant and becomes linear-time (set-based dedupe). A passed-
mode demotee's landing and the router's human line must agree
(the line names where the demotee actually landed). The router's
honest-record branch extends to the fallback-demote / zero-passer
case (no false "holds no route position" line; the record matches
the outcome). `upsert_route` refuses `primary == compete_with`.
The `apply_lesson_route_change` docstring is corrected where it
overclaims explicit-promote honoring in passed mode.

## M5 — remove the dead waiver channels (LOW; lessons.py)

`lessons._verdict_waived` keeps exactly one channel: the verdict's
`waiver` record (the only one with a production writer). Remove
the `verdict["waiver_candidate"]` and `gate_details
["waiver_candidate"]` legs. Re-base the existing suppression pins
onto the surviving channel; suppression behavior for real waiver
verdicts (offered or granted records on the verdict row) is
unchanged.

## Acceptance

Full suite green (including the new pins), demo exit 0. The
parent re-runs the round-9 HIGH probes (stale-attempt,
unavailable-seat, P3) against the fixed tree and the test-audit
mutations (wire severed; matrix-fallback offer restored) before
accepting the pass.
