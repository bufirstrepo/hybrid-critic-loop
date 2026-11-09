# Fix spec — rounds 3+4 findings (2026-11-08)

Owner order (Dino, 2026-11-08): fix what rounds 3 and 4 found, six agents,
orchestrated. Sources: ~/workspace/mm-review3-2026-11-08/1–4 and
~/workspace/mm-review4-2026-11-08/1–6, consolidated in the goal files.
Priority order: gate cluster → cascade/perf → input trust → bookkeeping →
ops/docs. Version stays 0.4.0 (the tree was never published or pinned).
Two items are EXCLUDED as owner decisions, do not touch them: (a) W/L and
validity banking before the human gate (round-3 observation — Dino's call);
(b) recording judging cost into the ledger (a design change; this pass only
makes CPVP's label honest about what it counts).

## Pinned interfaces (all units build against these, exactly)

- `chain.LEDGER_FIELDS` gains `"sealed_via"` (appended last). chain.py
  provides `row_hash_variants(row) -> dict` with keys `"current"`,
  `"pre_sealed_via"` (current minus sealed_via), `"legacy"` (current minus
  provenance and sealed_via), digest per form. The sealing pass treats a
  stored hash matching ANY variant as recognized; non-current matches are
  re-sealed under the current form via the existing cascade (marked
  "repair", or "upgrade" per G2).
- `Store.claim_human_gate(verdict_id, decision) -> bool`: one conditional
  `UPDATE verdicts SET human_gate=? WHERE id=? AND human_gate='pending'`,
  committed; True iff this caller claimed the gate.
- Certificate payload gains `"measured"` (the named verdict's measured
  value) and `"waived"` (bool: the named verdict's human_gate == "waived"
  at issue time). Verify compares both against the NAMED verdict row.
- Counted-row rule v2 (standing.ledger_totals): a row counts iff outcome
  is a generation outcome AND provenance is counted (not "internal", not
  "unattributed", not "") AND ( provenance == "legacy-import" OR the row's
  candidate_id names an existing candidate whose stored provenance equals
  the row's provenance OR (provenance == "executor-reported" AND the
  candidate exists) ). Rows failing only the referential leg are reported
  in the excluded totals under their own cause (G8).
- `record_human_gate(..., channel="cli")`: the recorded gate decision's
  `by` field is the channel. The plugin's gate tool passes
  `channel="tool"`; CLI passes "cli".

## G1 — Gate cluster (round 4: security F4/F7, contest F1/F2, regression F1)

- Atomic claim: record_human_gate FIRST calls
  `store.claim_human_gate(verdict_id, decision)`; on False it returns the
  existing "no pending human gate" error. The decision is therefore
  recorded BEFORE any merge; the old post-hoc `_record_decision` ordering
  and its swallowed write go away (the claim IS the record).
- Verdict-time snapshot: when a verdict with a winner is created, commit
  the winner's worktree and record that commit on the candidate (refresh
  its payload hash to the verdict-time commit). record_human_gate merges
  THAT commit — never the live worktree. If the snapshot commit is
  missing (legacy verdicts), fall back to the current behavior and say so
  in the result detail.
- Certificate placement: `_attach_certificate` is called ONLY on terminal
  non-error results — in record_human_gate after the decision completes,
  and in the verdict path only when the run's result is a completed,
  gate-decided state. Refused/rejected/error results carry no stamp and
  no token. (Direct issue via verdict_report for an already-decided
  contract is unchanged.)
- Channel: per the pinned interface; `critic_run`'s tool schema loses
  the `human_gate` parameter entirely.

## G2 — Cascade commit + sealing honesty (round 3 F1/F7, round 4 upgrade F1/F2, attack F3)

- `_backfill_chain` commits before EVERY non-abort return whenever the
  pass changed anything (and Store.close() commits any residual sealing
  transaction as a belt-and-braces). A persistence regression test closes
  and re-opens: second open reports 0 backfilled/repairs and reads the
  new hashes from disk.
- `chain_repairs` accumulates (`+=`), never overwrites.
- The canonical-form cascade gets its own sealed_via value `"upgrade"`
  (spec-pinned set becomes append/backfill/repair/upgrade); genuine
  suffix repairs stay "repair". The ledger report's chain line includes
  upgrade counts when present.
- sealed_via enters the canonical form (pinned interfaces above).
- Lock courtesy: when Store open hits "database is locked" during the
  sealing pass, surface an error naming the likely cause (another
  process holding the store) instead of the bare sqlite message, where
  the code controls the message (CLI open path at minimum).

## G3 — Fast paths (round 4 performance P1/P2/P3/P5/P6, regression F6)

- Open: gate the full chain recompute on a cheap probe — a NULL-hash
  scan plus a tail check (last row's stored hash recomputes under any
  recognized form, and the row before it links). Healthy current-form
  stores skip the walk entirely; verify_chain keeps the full walk.
- Totals aggregate in SQL: ledger_totals (and the per-contract/per-expert
  aggregations built on the same rows) become conditional-sum queries
  with the counted-row rule expressed in SQL (the referential leg joins
  candidates). No full-book dict materialization on the standing,
  certificate, or hook paths.
- Add the indexes the hot queries need (ledger by contract, ledger by
  expert, verdicts by contract) via the existing migration style.
- verify_chain(upto_id) pushes the bound into SQL.
- The hook computes standing ONCE: has_standing becomes an existence
  probe (one indexed query); pre_llm_call calls compute_standing at most
  once and reuses it for the line.
- Targets (auditor's method, synthetic books): healthy 200k-row open
  under ~0.1 s; first-turn hook under ~0.5 s at 200k rows. Add a
  large-book timing regression test at a smaller scale that fails if
  open regresses to a full walk (assert row-hash recomputations are not
  performed on a healthy open, e.g. by counting via a probe hook or by
  timing with generous margin — prefer the structural assertion).

## G4 — Named verdict + cert parity leftovers (round 3 regression F2/F5, attack F8/F4)

- verify_certificate uses `store.verdict_for(contract_id, verdict_id)`
  (the named verdict), comparing id, created_at, winner, measured, and
  waived. A later verdict on the same contract no longer affects it.
- issue_certificate requires the contract's counted rows to be non-empty
  (ledger_upto ≥ 1) — a zero-row verdict mints nothing.
- issue refuses fractional totals: if the run's token total is not
  integral, issue returns None rather than minting a token verify would
  reject as malformed (and run_totals surfaces integral tokens as ints).
- CLI verify-cert output scopes the claim: on success it prints the
  prefix validity AND the whole-chain state (from verify_chain); when
  the whole chain is broken beyond the prefix it prints the warning
  line naming the first bad row.
- Verify reasons get figures: totals mismatches name sealed vs
  recomputed values and ledger_upto vs the book's max ledger id; the
  kid-mismatch reason names the cert_key file ("…the store's cert_key
  does not match the certificate's key id; if this book was restored
  from backup, restore its cert_key beside it").

## G5 — Key race (round 3 regression F3)

cert_key creation publishes only-if-absent (e.g. write temp + os.link,
which fails if the target exists) instead of unconditional os.replace;
on contention the creator adopts the file's key. The corrupt-key replace
path (wrong length/symlink) keeps os.replace — contention there resolves
by re-reading after replace and adopting whatever the file then holds,
so two processes converge on one key.

## G6 — Input trust: parsers and executors (round 4 security F3/F5, round 3 core F12, regression F4)

- Usage: the JSONL walker trusts ONLY the provider envelope object (the
  top-level usage of the CLI's own output line / the API response
  object). Nested usage objects in model-authored text are ignored.
  Negative, NaN, or Inf token/cost values become "not reported" (None)
  at the walker — never passed on, so add_ledger's validation never
  detonates mid-run from executor data.
- Judge extraction: anchor to the LAST verdict-shaped object in the
  reply (or the named fenced block when present); reject a reply whose
  verdict object contains duplicate keys (object_pairs_hook) in the
  judge path.
- Legacy judged gate range check (round 3 core F1): judges.py's
  validator mirrors the matrix validator — every item score must lie
  within the rubric's [min, max]; out-of-range discards the score.

## G7 — Input trust: contracts and gates (round 4 security F1/F6/F8, contest F5/F6/F11, round 3 core)

- `timeout_s` is carried through validation (per item, hard and
  measured), capped at a sane maximum, and verify_baseline passes
  timeouts too — a contract's 1 s means 1 s everywhere.
- Gate/sensor commands run with a scrubbed environment: keys matching
  *_KEY, *_TOKEN, *_SECRET, or *PASSWORD* are removed from the env copy
  handed to gate commands. (Executor dispatch env is unchanged.)
- validate_contract: `id` is required and must match a conservative
  charset (letters, digits, dash, underscore; no path separators or
  dots); `judges` must be ≥ 1 when present; `expect.exit` must be a
  real bool or int (a bool is stored as bool, never satisfied by
  1 == True confusion — normalize explicitly); type errors raise
  ContractError, not bare ValueError. Worktree creation asserts
  containment before any mkdir.
- Measured comparisons: non-numeric metric values (strings, bools)
  raise GateError for that candidate's gate — and the loop converts a
  candidate's GateError into that candidate's elimination with the
  reason recorded, never a stranded contract (contest F3). A
  multi-metric item records all its rows or none (no partial-gate
  evidence).
- The tool schema and README disclose plainly: a contract's gate
  commands execute on the operator's machine, starting with the
  baseline run.

## G8 — Standing/report truth (round 3 attack F2, upgrade F4, regression F9, contest F8/F10/F12)

- Chain-gate: standing surfaces (standing_line, standing_report, the
  hook's line) check verify_chain first; on a broken chain they render
  "Millionaire Matrix standing unavailable — ledger chain broken at
  row N" instead of a tier. The hook still injects lessons; only the
  tier line degrades.
- Excluded totals split by cause: provenance-excluded vs
  outcome-excluded vs referential-excluded, each with its amount and
  row count, correctly labelled (and "1 row", not "1 rows").
- Per-expert dispatch counts/costs come from the same unified totals
  (ledger_totals), retiring the provenance-blind ledger_summary path
  for those figures — one book, one set of numbers. Experts-table
  stored counters are reconciled from the ledger or explicitly derived
  at read time (contest F8: the stored dispatches counter and
  refusal_rate must agree with the ledger's count of the same events).
- CPVP: computed from the unified totals; the label/definition in
  reports and README states what it counts (generation cost of scored
  candidates over their validity points; judging cost is not recorded
  by the system and is excluded — stated, not hidden). Off-scale
  medians can no longer enter it once G6's range check lands; no
  separate CPVP guard needed.
- verdict_report gains the winner's commit, attempt, and tie_break
  lines (what merged becomes visible).
- Escalated matrix runs credit measured validity (round 3 core F7):
  _escalate passes the scored candidate ids to _record_validity.
- Route demotions (round 3 core F5): apply_route_demotion handles the
  fallback branch and RETURNS what actually changed; router reports
  exactly that (no change → no "demoted" claim, no misleading ledger
  note).

## G9 — Contest mechanics leftovers (round 3 core F3/F4/F6/F8)

- Baseline rule: `proceed = hard_failed or measured_failed`
  (contracts.py), matching its docstring.
- Dissent second pass is per-candidate (matrix.py): one candidate's
  re-run request no longer suppresses the citation pass for others.
- Dissent re-runs replace, never duplicate, score rows: matrix
  persistence tags rows by round and a re-run supersedes that round's
  rows for calibration (calibration counts each judge–candidate pair's
  latest row per round exactly once).
- Calibration decidability (round 3 core F2): _decidable_outcomes
  counts winner = positive; judged-fail or gate-eliminated = negative;
  bracket-cut passers and ALL candidates of escalated verdicts are
  excluded (not decidable). NOTE in the commit message: this resolves
  the behavior half of OWNER-DECISIONS Item 2(b); the owner's policy
  answer is still his to give, and the code now matches the finding's
  recommended semantics.
- Slot guard fires before the wave (loop.py): contestants > SLOTS
  escalates with the capacity reason before any dispatch.
- The one-run guard becomes claim-like: creating the run's first
  record is the claim (a contract already "open"/running answers
  already_run BEFORE any worktree is created), and worktree/branch
  names carry the attempt so a racing second run fails cleanly, not
  with a raw RuntimeError (contest F7).
- Worktree lifecycle (contest F4): every exit path (escalation,
  rejection, error) removes candidate worktree dirs; branches are
  deleted on terminal states except the baseline branch is also
  removed once the contract is terminal. _dispatch checks seat
  availability BEFORE creating the worktree.

## G10 — Surfaces and docs (round 4 regression F2, upgrade F3/F4/F6, round 3 F8/F11)

- Tool handlers gain the CLI's exception boundary: critic_run and
  critic_gate wrap their loop calls and return {"error": …} JSON in
  the handlers' existing idiom instead of raising into the host.
- `_open_existing` validates: a 0-byte or non-sqlite data.db is an
  error ("store file exists but is not a database"), not a store to
  conjure a schema into. (A genuinely absent file keeps the T9 error.)
- README gains: (a) an upgrade note — pre-0.4 certificates stop
  verifying and are re-issued from the store; re-issued certificates
  may show different totals than originals because the totals
  definition was tightened (bookkeeping rows no longer count);
  (b) a backup paragraph — the book is data.db + data.db-wal +
  data.db-shm + cert_key, copied together (or via sqlite .backup);
  (c) the contract code-execution disclosure (G7); (d) the chain.py
  "certificates intact" comment is corrected wherever it lives
  (chain.py is Unit 2's file — that unit makes this edit).
- SUBMISSION.md: no change this pass (its re-validate instruction
  already covers the new tree).

## Acceptance (every unit)

- Full suite green, grown with regression tests per G-item touched
  (docstrings name the round + finding). Cross-process tests where the
  finding is cross-process (G1 claim race, G2 persistence, G5 key race).
- Demo exits 0 in a sandbox.
- No behavior change outside this spec; the two excluded owner items
  untouched. The demo transcript is regenerated ONCE by the parent
  after all units land — do not regenerate it in your unit.
