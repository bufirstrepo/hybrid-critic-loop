# Fix spec — round-7 findings (2026-11-08)

Owner programme (Dino, 2026-11-08 20:28 EST): fix pass between
check cycles for the HIGHs (and the convergent ring). Consolidated
review: goal files, mm-review7-consolidated-2026-11-08.md. Version
stays 0.4.0. The two standing owner exclusions remain untouched.
Attack F3 (self-declared "upgrade" route) is a NAMED RESIDUAL, not
in scope: after K3, that record is indistinguishable from a
genuine cascade re-seal by construction.

## K1 — Convergence validates the answer it trusts (attack F1 HIGH; security F3)

- Before _complete_human_gate's convergence branch treats an
  existing final_answers row as the completion, it validates:
  (a) the row's human_gate EQUALS the decision being completed;
  (b) when the row's commit carries the "git:" prefix, the sha is
  a real commit in the contract's repository (the manager's
  is_commit check the normal merge path already applies); a
  non-git commit string is accepted only if it does not claim the
  git form.
- On validation failure: the presentation returns an error result
  naming the failed check ("a final answer exists for this
  contract but does not check out: …"), writes NOTHING (no status
  change, no observation, no certificate), and does not consume
  the gate. The bogus row is left for the operator, named in the
  detail.
- The exception-path convergence (post-answer failure in a live
  completion) goes through the same validation — a live completion
  that just wrote the row passes it by construction.

## K2 — The detector covers the post-answer window (security F1; gate-core F1)

- _interrupted_completion gains the second case: verdict decided
  (approved/rejected/waived) AND a final answer exists AND the
  contract has NO terminal status → interrupted completion. The
  presentation converges it through K1's validated path (status,
  ref cleanup, certificate) instead of answering "no pending
  human gate". Detection still never fires on pending gates,
  escalations, or terminally-statused contracts.
- The gate_failure recording for this case names the window
  ("interrupted completion (process death after the final
  answer)").

## K3 — Recovery under concurrency (gate-core F2; security F2)

- The detector's decided→failed flip is CONDITIONAL: it lands
  only if the verdict is still in the exact state the detector
  observed (same human_gate value). If the flip loses, the
  presentation re-reads the gate and answers per the fresh state
  (a completed gate answers as completed/refused, never a
  fabricated failure).
- The final-answer write is atomic check-and-insert: the
  existence re-check and the INSERT happen under one write
  transaction (the store's BEGIN IMMEDIATE discipline), so two
  presenters cannot both pass the check. A presenter that loses
  the race converges on the winner's row (K1-validated) instead
  of writing a second answer. (A uniqueness constraint may
  accompany this if it can be added migration-safely; the
  transactional check is the required mechanism.)
- Invariant pinned by test: two Store instances presenting the
  same decision concurrently (one slowed inside its merge) →
  exactly ONE final_answers row, one gate_completion observation,
  no gate_failure recorded over the live completion, both results
  coherent (one completes, the other converges or is refused).

## K4 — Backfill requires a genuinely pre-chain book (attack F2)

- The open-time pass grants the "backfill" route only when the
  book carries NO sealed current-form row: if any row already has
  a current-form seal, the store is not pre-chain regardless of
  PRAGMA user_version, and unsealed suffix rows seal as "repair".
  (user_version is still advanced as before.) Genuine pre-chain
  books (no seals at all) backfill exactly as today — the upgrade
  walk's five migration shapes must keep their counted totals.

## K5 — The plugin lessons tool renders safely (security F5)

- plugin.py's critic_lessons handler renders each row's fields
  through the same render sanitizer reports.lessons_report uses
  (single line, capped) — no raw stored text reaches tool output.
  The tool's other fields (ids, counts) are unchanged.

## K6 — Small gate correctness (gate-core F3/F4/F5)

- Waiver grant on retry: if the verdict already carries a GRANTED
  waiver record, the retry preserves it (reason/by/provenance of
  the original grant) instead of rewriting it with the retry's
  defaults — same preserve-if-present care as the answer and the
  gate_completion observation.
- _recorded_gate_decision: the VERDICT's claimed value is the
  authority; a gate_failure observation is used only when the
  verdict carries no decided value. (The observation remains the
  record of WHY, never of WHAT was decided.)
- Detector recording dedupe: an interrupted presentation does not
  write a second gate_failure observation when one with the same
  decision and interruption reason already exists for the verdict.

## K7 — Cache-clear failure is visible (attack F4)

- verify_chain's cache clear: when the delete fails, the failure
  is recorded on the store (an attribute the cached check reads)
  and verify_chain_cached treats the cache as UNTRUSTED for that
  store instance until a successful clear or seed — it falls back
  to the full walk rather than trusting a cache a proven break
  may have left behind. The hook's line on such a store therefore
  answers from the walk. (Cross-instance recording is NOT
  required; the failing instance is the one that knows.)

## K8 — Declared-metric collector alignment (security F4)

- lessons._declared_metrics collects names ONLY from the contract
  shapes validate_contract accepts (the post-normalization
  measured-item shape). Raw-dict shapes the validator refuses no
  longer feed the sanitizer's declared set.

## K9 — Canonicalization + wording (attack F6; upgrade-walk F1)

- matrix._candidate_eliminated_kind canonicalizes the resolved
  kind (strip + lowercase) before matching, in both the top-level
  and evidence positions.
- Standing/chain wording: when verify_chain's first bad row
  matches a HISTORICAL canonical form (its stored hash recomputes
  under a recognized historical variant), the chain-state line
  says the book is sealed under an older form at that row
  ("older form — reopening upgrades it" is wrong if open already
  ran; the wording names the state, e.g. "ledger sealed under an
  older form at row N"), distinct from a row matching NO form
  ("broken"). The probe's interior blind spot itself is a named
  residual (contrived reachability; era-authentic books carry
  historical rows at the head).

## Acceptance (every unit)

- Full suite green, grown with regression tests per K-item
  (docstrings name round 7 + finding). K1's plant construction and
  K3's two-presenter race are mandatory tests.
- Demo exits 0 in a sandbox.
- No behavior change outside this spec.
