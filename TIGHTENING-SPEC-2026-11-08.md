# Tightening spec — Millionaire Matrix rework fixes (2026-11-08)

Owner order (Dino, 2026-11-08): fix what the four-agent review found. Source
findings: `~/workspace/mm-review2-2026-11-08/1-security.md`,
`2-correctness.md`, `3-host-integration.md`, `4-claims.md`; consolidated in
the goal files (`mm-review2-consolidated-2026-11-08.md`). Every item below
names its finding. Nothing here redesigns; all fixes are mechanical. The
honesty frame is unchanged: a local key is a store-integrity seal, not public
proof — the hosted verifier remains the designed, unbuilt next step.

## Pinned interfaces (all units build against these, exactly)

- Ledger gains two columns via the existing migration style:
  `provenance TEXT` and `sealed_via TEXT`.
- `provenance` values: the dispatch path passes the candidate's recorded
  provenance, or `"executor-reported"` when none is recorded; internal
  bookkeeping writers pass `"internal"`; any `add_ledger` call that passes
  nothing records `"unattributed"`. Pre-migration rows are backfilled
  `"legacy-import"` (see T2).
- `sealed_via` values: `"append"`, `"backfill"`, `"repair"`.
- `add_ledger(..., provenance=None)` — new keyword, default None →
  `"unattributed"`.
- `chain.verify_chain(store, upto_id=None)` — with `upto_id`, verifies only
  rows with `id <= upto_id` (prefix verification).
- Certificate payload gains `ledger_upto` (int), `kid` (str), `cost_rows`
  (int), `cost_missing` (int). Tokens in the pre-tightening format (no
  `ledger_upto`/`kid`) fail verification with the reason
  `"certificate predates the current format; re-issue it from the store"`.

## T1 — One totals implementation (security F5, correctness F6, claims #2)

One aggregation for spend, shared by standing and chain: reported-only sums
over generation-outcome rows (`_GENERATION_OUTCOMES`) that carry counted
provenance (T2). `chain.lifetime_spend` and `chain.run_totals` consume it;
`standing.contract_totals` delegates to the same helper instead of its own
loop. The certificate, the standing line, and the board can never disagree
again. State the definition once, in `standing.py`'s docstring.

## T2 — Ledger provenance (security F2)

- Migration adds `provenance`; existing rows backfill to `"legacy-import"`.
- Counted provenances for totals (T1): `"executor-reported"`, any candidate
  provenance string the dispatch path passes, and `"legacy-import"`.
  NOT counted: `"internal"`, `"unattributed"`.
- `compute_standing` also returns the excluded totals (`excluded_cost_usd`,
  `excluded_rows`) so nothing is dropped silently; the standing report
  prints the excluded line when non-zero ("$Y across N rows not counted —
  unattributed/internal writers").
- Every in-tree `add_ledger` call site passes an explicit provenance:
  dispatch sites pass the candidate's provenance (or `"executor-reported"`),
  queue/router/lesson bookkeeping passes `"internal"`.

## T3 — Sealing pass: gate on NULL hashes, not the flag (correctness F1/F9, security F1/F4)

- On open: find rows with NULL `prev_hash`/`row_hash`. Pre-chain stores
  (`user_version < 1`): seal all in id order, `sealed_via="backfill"`, then
  set the flag. Flagged stores: seal a NULL-hash SUFFIX (rows after the last
  hashed row — the legacy-writer append case) with `sealed_via="repair"`;
  NULL hashes in the middle (a hashed row exists after them) are corruption:
  leave them, `verify_chain` names them, and `add_ledger` raises
  `ValueError` rather than sealing onto GENESIS when its predecessor row is
  unsealed.
- During any sealing pass, recompute pre-existing hashes encountered instead
  of trusting them as links; a stored hash that does not recompute is
  corruption (same treatment as middle-NULL).
- `Store` records `self.chain_repairs` (count of rows sealed via "repair"
  this open) and `self.chain_backfilled` (count via "backfill"); the ledger
  report prints a chain line with totals per `sealed_via` when any
  non-"append" rows exist.

## T4 — Certificate: high-water mark + verify parity (security F3/F7, correctness F2/F10, host F2)

- Issue: payload gains `ledger_upto` = the contract's max ledger id at
  issuance; `chain_head` and run totals computed over the contract's rows
  with `id <= ledger_upto` only; `kid` = first 16 hex chars of
  `HMAC(key, b"mm-key-id")`; `cost_rows`/`cost_missing` = the run's counted
  rows / counted rows with no cost (run-level coverage, T8).
- Verify, in order: token shape → payload is a dict with the full key set
  and sane types (else "malformed certificate payload") → legacy-format
  check → `kid` mismatch returns the distinct reason
  `"certificate was issued by a different store (key id mismatch)"` →
  signature → the named verdict exists for the contract and its id and
  `created_at` match `verdict_id`/`issued_at` → prefix chain verification
  (`verify_chain(store, upto_id=ledger_upto)`) → prefix totals and prefix
  chain head recompute → tier recomputed from CURRENT lifetime spend
  (unified totals, T1): reject when the payload tier ranks ABOVE the
  current tier; a lower sealed tier is legitimate (spend grew).
- Delete the false "lifetime spend only grows" justification from the
  docstring; state the actual rule.

## T5 — Key handling (security F6/F9, host F1, correctness F8)

Rewrite `cert_key`'s file handling:
- Read raw bytes; NEVER `.strip()`. A valid key file is exactly 32 bytes.
- Missing file: create atomically — write temp file in the same directory
  (mode 0600), fsync, `os.replace` onto `cert_key`, fsync the directory.
- Existing file with wrong length (incl. zero bytes) or a symlink at the
  path: treat as corrupt — replace atomically with a fresh key the same
  way. (Certificates signed under a corrupt key were unverifiable anyway.)
- A racing reader that finds the file momentarily empty retries the read
  briefly (bounded loop) before concluding corrupt.
- File-backed store whose key cannot be persisted (OSError): NO ephemeral
  fallback — `cert_key` raises a named `CertificateKeyError`;
  `issue_certificate` catches it and returns None (reports print no
  certificate); `verify_certificate` returns `(False, "certificate key
  unavailable")`. `:memory:` stores keep the ephemeral per-instance key.

## T6 — No winner, no certificate (claims #1)

`issue_certificate` returns None when the verdict's `winner` is None; drop
the `_attach_certificate` call in `loop._escalate`. Wording in `chain.py`'s
docstrings becomes "a run that reached a verdict **with a winner**".

## T7 — add_ledger input discipline (security F8, correctness F5/F7)

- Validate before insert: `tokens_in`/`tokens_out` must be None or
  non-negative ints (accept integral floats, store as int; reject bool,
  str, NaN/Inf); `cost_usd` must be None or a finite non-negative number
  (store as float). Violations raise `ValueError` in the existing style.
- Wrap the write body in try/except: on any exception, `rollback()` before
  re-raising. Audit the other `_begin_write` callers for the same leak
  shape; fix any identical one found and name it in the commit message.

## T8 — Money exactness + partial coverage (correctness F3/F4/F13)

- Round aggregated spend to cents at aggregation time (the unified totals,
  `compute_standing`, `next_tier` gaps): tier and gap math run on the
  rounded value.
- `compute_standing` returns `cost_rows` / `cost_missing` (counted rows /
  counted rows with no cost). Surfaces render partial coverage explicitly:
  the standing line/report shows `$900.00 lifetime measured spend
  (1 of 2 dispatches reported cost)` when `cost_missing > 0`, and the
  next-tier gap reads "at least $X more measured spend" in that case.
- Per-expert label `tier=` in the standing report becomes `validity_tier=`
  (it is computed from validity cost, a different stream than the dispatch
  cost printed beside it); update the test that pins the old label.

## T9 — CLI / host surfaces (correctness F11/F12, host F4/F5/F6)

- Read-only commands (`standing`, `verify-cert`, `session-start`) fail
  with a clear error and exit 1 when `<data dir>/data.db` does not exist —
  they never create a store.
- `session-start` fallback gated on `has_standing(store)` (matches the
  hook: empty store prints nothing).
- Lazy-import `standing` where used in `plugin.py` (slash `standing`
  branch; on ImportError return a "standing unavailable" line) and in
  `hooks.py` (inside `pre_llm_call`, where the never-raises guard then
  genuinely covers it) — mirroring the `_certificate_lines` pattern.
- Correct the `resolve_data_dir` docstring: step 1 is a forward-compat
  probe (the real host's PluginContext has no `plugin_data_dir`);
  `plugins.plugin_storage` is the authoritative in-host source.
- CLI registration/help strings name the new surfaces
  (`standing`, `verify-cert`); `plugin.py` docstring "four lifecycle
  hooks" → "two lifecycle hooks".

## T10 — Docs refresh (claims #3–#8, security F10)

- README chain paragraph: qualify — the chain detects any edit *that does
  not also recompute the chain*; verification gains meaning against a head
  or certificate exported earlier. Keep the honesty paragraph as is.
- README certificate example: replace the invented figures with the demo's
  real stamp (`MM CERTIFIED · TOKENS NOT REPORTED · ONE ANSWER`).
- README Usage: add `/critic standing`; CLI list gains
  `standing|verify-cert`; tools line gains the clause "standing and
  certificate verification are operator surfaces (slash/CLI), not agent
  tools."
- README demo section: regenerate `demo-output-transcript.txt` from a
  fresh demo run AFTER the code settles; Fresh-session bullet mentions the
  standing line.
- README scope/architecture: "Shipped in this entry" gains lifetime
  standing/tiers, the hash-chained ledger, signed per-run certificates;
  architecture table gains `standing.py` (Memory) and `chain.py` (State).
- README standing bullets: bullet 3 → "carries the store's current tier
  and the run's recorded contribution, and closes with the run's
  certificate"; bullet 2 → session-start prints the standing *line*, not
  the board.
- SUBMISSION.md: Step 2 beats end "…the verdict report prints the MM
  CERTIFIED stamp → run 2 starts with the standing line and the Lesson in
  context"; Step 1 note names both post-validation passes (fix pass +
  standing/certificates rework).
- `chain.py`: delete `_FALLBACK_TIERS`/`_fallback_tier_for`; import
  `tier_for` from `standing` directly (claims #10).

## T11 — Version 0.4.0 (host F3, claims #9)

Bump `0.3.0` → `0.4.0` in all four pinned places (pyproject.toml,
plugin.yaml, the-millionaire-matrix.yaml, `__init__.py`) and the README's
"Shipped in this entry (v0.3)" line → (v0.4). README chain section gains
one sentence: pre-chain builds must not append to a chained store.
Historical statements in SUBMISSION.md about what was validated for the
v0.3 tree stay historically worded; only present-tense framings of what
ships move to v0.4.

## Acceptance (every unit)

- Full test suite green, grown with regression tests for each T-item the
  unit touches (name the finding in the test docstring/comment).
- Demo exits 0 (run in a sandbox: `HERMES_HOME`/`HCL_DATA_DIR` under /tmp).
- No behaviour change outside this spec. The checkup-gate and deletion
  rules from AGENTS.md apply to any PC-side work; this build is local.
