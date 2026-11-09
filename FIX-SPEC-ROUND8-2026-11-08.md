# Fix spec — Round 8 (from the round-8 review of HEAD `5f30ea5`)

Owner programme (2026-11-08): three consecutive check cycles with fix
passes for the HIGHs between them; this pass follows cycle 3. Two
owner exclusions stand, untouched: (a) W/L + validity banking before
the human gate, (b) recording judging cost into the ledger.

Reports: `~/workspace/mm-review8-2026-11-08/1–6`; consolidated:
`mm-review8-consolidated-2026-11-08.md`. Both majors below were
reproduced by the parent personally
(`mm-review8-2026-11-08/muse-round8-verify.py`).

## L1 — Convergence validates identity, not just existence (attack F1 HIGH; security F1)

`_validate_final_answer` gains the identity legs for git-form
commits. A row is trusted only when ALL hold:

- the recorded decision equals the decision being completed (K1,
  unchanged);
- the commit is git-form (`git:<sha>`) — **a non-git form is now a
  validation failure**: no legitimate writer produces one (the sole
  production call site writes the post-merge head), so the round-7
  spec's acceptance of non-git forms is withdrawn;
- `mgr.is_commit(sha)` (K1, unchanged);
- **the winner's snapshot commit is an ancestor of the answer's
  commit** (`merge-base --is-ancestor <snapshot> <answer_commit>`),
  where the snapshot resolves exactly as the merge path resolves it
  (dedicated snapshot ref first, then the winner's `payload_hash`);
- **the answer's commit is reachable from the merged branch's
  current head** (ancestor of HEAD of the repo's main branch, via
  the manager's existing primitives).

Why this is safe for genuine rows (both reviewers' control): a real
completion merges the snapshot and writes the resulting head — that
head has the snapshot as ancestor and IS main's head at write time,
hence reachable. A fast-forward merge writes the snapshot itself,
which satisfies both legs once merged. A plant can satisfy neither
leg without actually merging the winner's work — at which point the
completion it claims is real.

Refusal semantics unchanged (K1): `_FinalAnswerMismatch` naming the
failed leg, nothing written, gate unconsumed.

**Fixture correction (part of this item):** the round-7 tests built
"genuine" post-answer states by planting the SNAPSHOT hash as the
answer commit (`test_wellformed_plant_still_converges`,
`TestPostAnswerDeathConverges`). Production writes the MERGED HEAD.
Correct those fixtures to production form — perform the merge in
the fixture (or record the merged head the completion would write)
— so the suite's genuine states are genuine. The plant tests then
add: snapshot-hash plant REFUSED (names the ancestry/reachability
leg), root-commit plant REFUSED, unrelated-branch commit plant
REFUSED.

## L2 — The backfill route requires a book free of ANY recognised seal (attack F2)

K4 counted only current-form seals as disqualifying. A prefix sealed
entirely under a historical form (legacy digest) still routes the
suffix to `backfill`, and the append counts (parent-reproduced:
MILLIONAIRE — $2,000,020). The route condition becomes: `backfill`
only when the prefix validation found **no recognised seal of any
form** — no row whose stored hash matches the current,
pre_sealed_via, OR legacy variant. Otherwise the suffix seals
`repair`, counters following the route used.

Why genuine books are safe (same argument as K4, widened): a genuine
pre-chain book's rows carry NO seals (row_hash NULL) — still
backfill. A genuine historical book's own rows count through the
cascade/exemption as before; only suffix rows that could count
SOLELY via the backfill exemption lose it — the laundering class.
The five genuine migration shapes' counted totals must be
re-verified unchanged (the upgrade walk's fixtures); the
partially-sealed tradeoff (upgrade-walk round-8 F1) is unchanged in
shape. Acceptance: the parent's P2 construction (legacy-form prefix
+ flag reset + $2M append) lands `repair` and counts nothing; the
round-7 K4 fixtures stay green.

## L3 — Cache distrust must survive the instance (attack F3)

K7's distrust flag is per-instance; a fresh Store on the same file
trusts the stale cache — and the hook the fix protects is itself a
fresh process per firing. Make the distrust **durable**: when a
verified-head clear fails, record the distrust in the store's own
persistent state (a meta row), checked by every instance's cached
verification before trusting the cache; a successful clear, seed,
or full-walk verification removes it. The in-instance flag may stay
as a fast path, but the durable record is the contract. Acceptance:
instance A sabotaged-clear → flag; instance B (fresh, same file)
cached check falls back to the full walk and its hook line renders
the honest line; after a successful walk, B (or C) trusts again.

## L4 — Refusal recovery stops fabricating deaths (attack F4; security F2)

When a final answer EXISTS, validation precedes detector recording:
presenting a decision over a state whose answer fails L1/K1
validation refuses via the mismatch path with **no** interruption
(gate_failure) record appended by that presentation — there was no
completion to interrupt. When the answer validates, the post-answer
detector case records as today (a real interruption) and converges.
The no-answer detector case is unchanged (its record is the standing
death-vs-life residual, out of scope). The append-before-flip
ordering and its binding rationale are unchanged. Acceptance: the
attack's construction — present over a refused plant, resolve
(remove the plant), present again to completion — leaves ZERO
gate_failure records; the genuine post-answer death still leaves
exactly one.

## L5 — The result never manufactures a git claim (attack F5)

In `_finish_from_existing_answer` (and any sibling result
construction), the completion's commit is reported AS STORED on the
answer row — never re-prefixed. With L1 refusing non-git forms the
plant path is closed; this item removes the re-wrap construction
itself (`merged_sha` prefix-stripping/re-prefixing asymmetry), so
no code path can announce `git:<free text>` from an unvalidated
string. Acceptance: a code-level pin — the convergence result's
commit field byte-equals the stored row's commit for a genuine row.

## L6 — Pins for the integration edits (test-audit F1/F2)

Muse's round-7 integration commit (5f30ea5) shipped two correct,
untested edits; both mutations survived the full suite. Add the
audit's specified pins:

- a raw canonical-shape contract whose declared metric carries the
  round-4 attack string, driven through `write_lesson` directly:
  the stored rule carries `(unrecorded)` and no payload (the
  declared-shortcut charset guard);
- `ledger_report` over the K9 mixed-form book renders the
  older-form line, not "broken" (the store argument).

## L7 — `ledger_report`'s header learns the discrimination (upgrade-walk observation)

`ledger_report` prints its own `chain state: broken at row N` line
above the unavailable line; for an older-form book the two lines
contradict. The header's chain-state line uses the same form-aware
wording as `_unavailable_line` with the store passed (older form vs
broken), so a report never contradicts itself line-to-line. Pin it
in the L6 ledger_report test.

## Acceptance for the pass

Parent's harness re-run (plant with snapshot hash now REFUSES
naming the leg; legacy-prefix construction lands repair, counts
nothing; round-6/7 harnesses stay green), full suite green with the
new tests, demo exit 0, PC copy synced.
