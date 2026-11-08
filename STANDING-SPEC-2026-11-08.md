# Standing, tiers, and the anti-fake chain — build spec (owner: Dino, 2026-11-08)

Rework target: this tree (The Millionaire Matrix, HEAD 20b971e). Claude's original
stays untouched on the PC; all work happens in this clone. Local commits only —
never push, never publish, never install into a live Hermes.

## Owner decisions baked in (do not redesign)

- Tagline: "You want to prove you're rich? Show us you're running the Millionaire Matrix."
- Tiers are LIFETIME measured spend (the store's client book), final ladder:
  WINDOW SHOPPER (no measured spend) · CLIENT ($0.01–$999.99) · HIGH ROLLER
  ($1,000–$9,999.99) · PLAYER ($10,000–$99,999.99) · BALLER ($100,000–$999,999.99) ·
  MILLIONAIRE ($1,000,000+).
- The per-run token identifier (the receipt): `MM CERTIFIED · <tokens> TOKENS SPENT · $<cost> BURNED · ONE ANSWER`.
  If the run's cost was not reported by any provider, the dollar segment is omitted —
  never estimated. The stamp is issued only for a run that reached a verdict.
- Tokens never convert to dollars. Where cost is unmeasured, standing shows tokens
  and spend as "not reported", per the plugin's existing discipline.

## Unit A — standing (tiers in the agent)

1. New module `hybrid_critic_loop/standing.py`:
   - `compute_standing(store)` → lifetime totals from the ledger (tokens in+out summed
     where recorded; cost summed where recorded) + per-expert rows (reuse the experts
     table's validity fields; do not duplicate aggregation logic that reports.py
     already owns — import/reuse where clean).
   - `tier_for(spend)` → the ladder above; spend None (nothing measured) → WINDOW SHOPPER
     with spend shown "not reported".
   - `standing_line(store)` → one line: `Millionaire Matrix standing: <TIER> — $<spend> lifetime measured spend · <tokens> tokens` (segments omitted when unmeasured).
   - `standing_report(store)` → the full board for the local store: standing line,
     per-expert table (same columns as the ledger report plus tier per expert from
     that expert's measured validity_cost where present), next tier name and the
     exact additional measured spend required to reach it (omitted at MILLIONAIRE).
2. Session surface: `hooks.pre_llm_call` prepends the standing line to the injected
   context (above the lessons block), still under the 10,000-char cap, still
   never-raises, still first-turn only. If lessons are empty but standing exists,
   inject the standing line alone.
3. CLI: `standing` subcommand in cli.py (`hermes critic standing`, `hcl standing`,
   `/critic standing` via the existing plugin dispatch) printing `standing_report`.
   Also append the standing line to `session-start` output.
4. Verdict surface: `reports.verdict_report` gains a standing line (tier after this
   run + whether this run changed the tier, computed by comparing lifetime totals
   before/after is NOT available — instead state the current tier and the run's own
   contribution: tokens and cost recorded for this contract, or "not reported").
5. Tests: `tests/test_standing.py` — tier boundaries (each threshold edge), empty
   store, unmeasured store (all None → WINDOW SHOPPER + "not reported"), standing
   line format, hook injection includes the line and respects the cap, CLI command
   runs. Every existing test must still pass.

## Unit B — hash-chained ledger + per-run certificate (the anti-fake core)

1. New module `hybrid_critic_loop/chain.py`:
   - Canonical row serialization (stable key order, the ledger row's own fields).
   - `row_hash(prev_hash, row)` = sha256 over prev_hash + canonical row.
   - `verify_chain(store)` → (ok, first_bad_id or None): recompute from genesis.
2. Store: ledger gains `prev_hash` and `row_hash` columns (migration-safe: create
   if missing; pre-existing rows without hashes are backfilled in id order on first
   open, marked by a store meta flag so it happens once). Every new ledger append
   computes and stores both. Appends happen under the store's existing lock.
3. Certificate: at verdict time the run result and `reports.verdict_report` carry a
   certificate block:
   - `MM CERTIFIED · <tokens> TOKENS SPENT · $<cost> BURNED · ONE ANSWER` (stamp rules above)
   - `cert: mm1.<base64url(payload)>.<base64url(sig)>` where payload = {contract_id,
     verdict_id, tokens, cost_usd (or null), tier, chain_head (row_hash of the last
     ledger row for this contract), issued_at} and sig = HMAC-SHA256 over the payload
     with the store's local key. Local key: generated once into the plugin data dir
     (`cert_key`, 32 random bytes, file perms 600) — labelled in code and README as
     the LOCAL key: it proves the certificate came from this store unaltered; the
     hosted verification service (future, named in README as not built) is what
     makes a public claim checkable. Do not claim more than that anywhere.
   - `verify_certificate(store, token)` → (ok, payload or reason): recompute HMAC,
     recompute the run totals from the ledger, recompute the chain head; any
     mismatch → not ok with the reason named.
   - CLI: `verify-cert <token>` subcommand printing the verdict of verify_certificate.
4. Tests: `tests/test_chain.py` (chain verifies on a fresh store; tampering with a
   stored row's tokens breaks verification at that row; backfill works on a store
   created without hashes) and `tests/test_certificate.py` (stamp format with and
   without cost; certificate verifies; edited payload fails; edited ledger row
   fails; certificate absent for a run with no verdict). Every existing test must
   still pass.

## Both units

- Stdlib only. No new dependencies. No network calls. No telemetry.
- README: add a "Standing — the client book" section (tiers, lifetime rule, where
  standing appears) and a "Certificates and the chain" section stating exactly
  what the local signature does and does not prove, and naming the hosted
  verification service + live receipts + top-tier audit as the designed next
  step, NOT built. Update SUBMISSION.md only if a claim there becomes false.
- Run the FULL suite (`python3 -m unittest discover -s tests`, sandboxed
  HERMES_HOME/HCL_DATA_DIR) and the demo (`python3 demo/run_demo.py`) before
  finishing; report the exact result lines. Commit locally in this clone with
  clear messages. Do not touch anything outside this clone.
