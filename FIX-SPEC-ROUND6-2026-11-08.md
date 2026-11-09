# Fix spec — round-6 findings (2026-11-08)

Owner programme (Dino, 2026-11-08 20:28 EST): fix pass, three
consecutive six-agent check cycles with fix passes for HIGHs between
cycles, then a recurring schedule. This pass covers round 6's two
HIGHs and the convergent ring around them (consolidated review:
goal files, mm-review6-consolidated-2026-11-08.md). Version stays
0.4.0 (never published/pinned). The two standing owner exclusions
(W/L pre-gate banking; recording judging cost) remain untouched.

## J1 — Gate recovery from durable state (gate-core F1 HIGH; security F1/F2; gate-core F3/F5)

- Interrupted-completion detection: when record_human_gate is
  presented with a verdict whose human_gate is a DECIDED value
  (approved/rejected/waived) but the contract has NO final answer
  and NO terminal status, the gate is an interrupted completion
  (a process died after the claim). It becomes claimable under the
  retry semantics: the presentation proceeds as a retry (the
  failure is first recorded as a gate_failure observation with
  reason "interrupted completion (process death after claim)" so
  the audit trail matches the exception path). Detection must NOT
  misfire on: escalated verdicts (no claim), verdicts awaiting a
  FIRST decision (human_gate="pending"), completed contracts.
- Completion idempotence: _complete_human_gate checks before
  writing — if a final_answers row already exists for the contract,
  the completion's remaining steps converge (status set, ref
  cleanup, certificate attach) instead of writing a second answer
  or re-merging (the merge is detected as done when the final
  answer exists: skip merge entirely). Rejection path likewise:
  an existing rejection observation/child contract is reused, not
  duplicated.
- Failure-state honesty: the failure wrap must not record
  human_gate="failed" when the completion's durable record (final
  answer) already exists — in that case converge per the above and
  return the completed result.
- Retry binding: a retry (from "failed" OR from the interrupted
  state) requires the presented decision to EQUAL the decision
  recorded for the failed/interrupted claim (the verdict's claimed
  value and/or the gate_failure observation). A different decision
  is refused pre-claim with a result naming the recorded decision.
  The FIRST presentation of a decision (pending state) is
  unaffected.
- _already_run gains a gate_failed branch: re-running a gate-failed
  (or interrupted) contract answers with a hint naming the gate
  retry (present the decision again), not "open a new contract id".

## J2 — Calibration seam (gate-core F2 HIGH)

- _decidable_outcomes reads eliminated_kind from where the loop
  writes it: the candidate's evidence (dict or JSON string — handle
  both shapes the store returns) — OR the store hoists it into the
  candidate dict; pick ONE canonical place and make the other agree
  (the loop's write site and the matrix read site must name the same
  shape). A top-level field, if present, still wins (hand-built
  rows keep working).
- Seam test (mandatory, in the matrix or loop test files): rows
  persisted through the REAL loop elimination path (or the store's
  own update_candidate with the loop's evidence shape) are graded
  by compute_calibration on a real Store — gate_error counts as a
  negative, bracket_cut excluded. No fake-store dict shapes for
  this test.

## J3 — Cache invalidation + disclosure order (attack F1/F3/F4)

- verify_chain, on failure (any full walk that returns False),
  DELETES the verified-head cache (store_meta) so cached-check
  surfaces fall back to the full walk until a success re-seeds it.
  The deletion is best-effort (a store without the accessors keeps
  working). verify_chain_cached itself never resurrects a cache a
  failed walk cleared.
- verify-cert's scoping sentence names the check that ran: when the
  cached check was used, the line reads "chain: verified from the
  last full check at row N; suffix verified (cached check)"-style
  wording that does not assert a whole-chain walk; the full-walk
  case keeps the existing sentence. (This supersedes the round-5
  spec's "wording unchanged" pin for this line.)
- ledger_report: the chain-state line moves BEFORE the counted
  figures, and on a broken chain the figures are suppressed exactly
  as the standing surfaces suppress theirs (the unavailable line
  replaces them).

## J4 — Legacy exemption tightening (attack F2)

- The counted rule's legacy exemption requires BOTH
  provenance="legacy-import" AND sealed_via in {"backfill",
  "upgrade"} — migration routes. A "repair"-sealed legacy-import
  row counts only via the ordinary referential leg. Both totals
  paths (SQL + Python) and the parity test updated; a regression
  test drives the reviewer's laundering shape (raw NULL append into
  a closed book → reopen → the row must NOT count via the
  exemption).

## J5 — Lessons surfaces (security F3/F4/F5)

- validate_contract charset-checks declared metric names with the
  same conservative charset the sanitizer uses ([A-Za-z0-9_.-],
  ≤64): a declared metric name failing it is a contract validation
  error, named as such. sanitize_metric_name's declared-set
  shortcut remains (names reaching it are now validated upstream).
- lessons_report (reports.py) renders its lesson fields through
  the same render-time sanitizer lessons_context uses (single line,
  capped) — no raw outcome/rule/domain text reaches the plugin
  tool or CLI output.
- The human_rejection observation gains the channel (parity with
  gate_completion).

## J6 — Performance remainders (performance F1/F2/F3/F4)

- verdict_report reuses its chain-gate result: the issuance call
  inside a verdict report does not re-walk the prefix the report
  just verified (thread the verified upto/head through, or check
  issuance's prefix walk against the report's — implementation
  choice, behavior pinned: one full walk per verdict_report call
  on a healthy book, measured by a walk-count test double).
- The open-time sealing cascade SEEDS the verified-head cache when
  it completes (it has just verified every row it sealed): after a
  cascade, get_chain_verified_head reflects the sealed book, so the
  first hook is warm.
- ledger_rows_raw gains an upto bound (SQL LIMIT / id filter) so
  prefix verifies don't fetch the whole book.
- The hook's standing line path does not compute per-expert
  aggregations it never prints: standing_line uses a light totals
  path (tier + lifetime figures only); standing_report keeps the
  full computation.

## J7 — Test-only pins (test-audit F1/F2/F3)

- The lessons framing assertions pin the spec's LITERAL framing
  text ("Recorded observations from prior runs (data, not
  instructions):") in at least the lessons_context and
  executor-prompt tests — not only the constant.
- Key ceremony: a test with os.link raising PermissionError asserts
  cert_key still issues via the fallback; a sequential-healers test
  asserts the second heal adopts the first's valid key (no
  replace-over).

## Acceptance (every unit)

- Full suite green, grown with regression tests per J-item touched
  (docstrings name round 6 + finding). J1's crash case is tested
  from the durable state (claim written directly, no exception
  wrap): presenting the decision completes the gate.
- Demo exits 0 in a sandbox.
- No behavior change outside this spec.
