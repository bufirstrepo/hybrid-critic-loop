# Briefing packet for Claude (Fable) — The Millionaire Matrix hardening pass

Prepared by Muse (parent reviewer) from two personal review passes over the
final tree, 2026-11-07. Every claim below carries a file/line anchor Muse
verified by reading the code or running the command. This packet is the data
for a Claude Code `/goal`; the goal text itself is set by the operator.

## What this is

**The Millionaire Matrix** — a Hermes plugin (Python package
`hybrid_critic_loop`) built for the Nous Research Hermes Agent Plugin
Hackathon. **Deadline: Nov 11, 2026, 5:00 PM EST.** Judging: usefulness,
reliability, creativity, polish; Nous favors plugins that do more than a
prompt or a skill could.

The design (operator's, settled — fidelity, not invention):

- A task is a **contract** with structured acceptance: hard gates (command +
  exit code), measured gates (sensor JSON + metric/operator/threshold), and a
  judged gate. Prose-only and judged-only contracts are refused
  (`contracts.py`). The acceptance suite must execute and **fail on the
  unmodified baseline** before any candidate runs, or the contract is refused
  as vacuous (`contracts.py:11` docstring; enforced `loop.py:227-240`,
  status `refused_baseline`).
- **Panel**: one candidate per model family the operator has a seat for;
  target panel is eight families (anthropic, openai, xai, google, reflection,
  alibaba, moonshot, deepseek — `router.py:21-25`). Each candidate generates
  in its own git worktree with provenance `generated`; mock/human artifacts
  can never pass a gate (`gates.py:90-98`, provenance check first).
- **Gates in rank order**: hard > measured > judged. The matrix never
  overrules the anchor.
- **Peer validity matrix** (`matrix.py`, rubric `validity_matrix_v1`): every
  family blind-scores every *other* family's candidate on four rows
  (question_fidelity, completeness, evidence, test_agreement). Self-cells are
  stripped in code before dispatch (`matrix.py:219-242 build_matrix_cells`);
  a peer whose family cannot be verified is skipped entirely
  (`matrix.py:544`); identity terms are scrubbed from packets. Cells require
  score 0-10 + confidence 0-1 + evidence pointer + rationale, else discarded
  whole (`matrix.py:328-371`). Aggregation: per-row weighted medians, cell
  weight = judge_calibration x confidence. Pass: median of per-judge means >=
  contract min, no row below min - 2.
- **Dissent**: `spread_threshold` (default 2.0, escalates) and
  `dissent_threshold` (default 3.0, flags a cell vs its row median) are
  separate contract fields. A dissent citing a test/measurement re-runs that
  gate before merge (`loop.py:480-511`, once per attempt). Mean cell
  confidence < 0.4 escalates instead of passing (`matrix.py:111, 708-720`).
- **Step-down bracket** (`bracket.py`): the field narrows by recorded
  validity score — `bracket.cut_per_round` default 2 (16 -> 14 -> ... -> 2 ->
  final scored round -> 1), `cut_mode: "half"` available; a cut never jumps
  past two; cuts are pure functions of recorded scores (ties: fewer tokens,
  then earlier completion); hard/measured failure eliminates at ANY round;
  a candidate the panel failed is dropped at the cut even if its raw rank
  kept it (`loop.py:526-545`). There is NO synthesis/merge stage — the
  operator corrected that reading explicitly; `bracket.py:3-6` states it.
- **Human gate**: merge happens only after approval (`loop.py:814-828`,
  `record_human_gate` at `loop.py:868ff`). A human may waive a measured
  failure only, never a hard one.
- **Lesson writeback**: at verdict, a Lesson is derived from the deciding
  gate's own evidence (`lessons.py:44ff derive_outcome_cause` — measured
  failure rows first), stored in the plugin's own SQLite store
  (`written_to: plugin_store`), its route_change applied to the routes table
  (`loop.py:851-862` -> `router.py:116ff`). Next session, lessons inject via
  the `pre_llm_call` hook (`plugin.py:534`, `hooks.py:49-60`) under the real
  Hermes 10,000-char cap (`lessons.py:218ff lessons_context`).
- **Cost per validity point**: computed only when both cost and validity are
  measured numbers; otherwise prints `not reported` (`reports.py:66-70`).
  Never estimated.
- **Calibration**: prior 0.5, Laplace-smoothed, weighting activates at
  n >= 5 scored cells, `calibration_n` recorded (`matrix.py:1014ff`).
- **Events**: exactly four types (`loop.py:72`): spread_over_threshold,
  budget_exhaustion (real budget check, `loop.py:138-149, 263-265`),
  retry_cap (`MAX_ATTEMPTS = 3`, `loop.py:47`), human_gate. They persist to
  an events table and surface via `critic_ledger` — no phone/agent-room ping
  exists in the plugin API and none is promised.

## Verified state of the tree (2026-11-07, parent re-run)

- HEAD `31c1450` (v0.3 commit `0e7c173` + parent verification log commit).
  Working tree clean except one untracked file: `lessons.md` (see F3).
- **219/219 tests pass**, re-run module by module by the parent:
  state 47, gates 21, judges 15, executors 28, executors_api 19, lessons 27,
  plugin 16, loop 5, matrix 16, bracket 10, loop_v03 4, reports 11.
  (Known cosmetic: `python3 -m unittest discover` prints the plugin CLI help
  to stdout via test_plugin; per-module runs are clean.)
- **Demo exits 0** (`demo/run_demo.py`, re-run by the parent): run 1 the
  measured gate kills both allocating candidates (ops_per_tick 120.0 vs <= 8),
  anchor decides, Lesson `lsn_0001` written, naive expert demoted; fresh
  session injects the Lesson; run 2 full field passes, matrix fills, bracket
  steps 3 -> 2 -> 1. Transcript: `demo-output-transcript.txt`.
- **`hermes plugins validate` on the operator's PC (Hermes v0.21.5): 13/13
  checks pass, security scan safe**, both plain and `--install-deps` forms
  (re-confirmed 2026-11-07). Manifest floor `requires_hermes: ">=0.21.0"`.
  Catalog file `the-millionaire-matrix.yaml` mirrors `plugin.yaml`
  capabilities exactly (4 tools: critic_run, critic_matrix, critic_lessons,
  critic_ledger; 4 hooks: on_session_start, pre_llm_call, post_tool_call,
  post_llm_call — registrations at `plugin.py:432-444, 533-536`).
- C# fixture (`fixture/godot-csharp/`) builds clean on the operator's PC
  (dotnet 8.0.131, 0 warnings/errors) and its tests fail on the stub baseline
  as the contract requires. NOT compiled in the build sandbox (no dotnet).

## Findings — fix these (ranked)

### F1 (design-rule violation) — degraded verdicts still apply route demotions

The operator's floor rule: a matrix with fewer than 2 distinct peer families
scoring a candidate is **degraded** (`matrix.py` per-candidate
`panel_status`; outcome-level at `matrix.py:722-726`), and a degraded matrix
"can never produce the Phase-1 lesson-loop exit on its own" (README, Panel
section). In the code, NOTHING checks panel_status in the lesson path:
`loop.py:851-862 _write_lesson` applies `lesson["route_change"]` via
`router_mod.apply_lesson_route_change` unconditionally, and
`lessons.py:44ff derive_outcome_cause` never sees panel status.
Partial defense, stated for accuracy: lessons derive from gate evidence
(measured/hard rows), not from matrix opinion, so the Lesson *content* is
anchored even on a thin panel. The **route demotion** is the act the floor
rule was meant to gate.
Required fix shape: when the verdict's panel_status is degraded, the Lesson
may be stored but its route_change must NOT be applied; record the hold in
the ledger/verdict so it is visible, not silent. Add tests: degraded run ->
lesson exists, route unchanged, hold recorded; full run -> demotion applies
(existing behavior).

### F2 (stale doc) — SUBMISSION.md contradicts the verified state

`SUBMISSION.md:22` says v0.1.0 passed validation and instructs re-running
validate "for the v0.3 tree before opening the PR". The v0.3 tree HAS been
validated on the operator's PC: 13/13, security safe, both forms (see
BUILD-LOG parent-verification section, commit `31c1450`). Correct the step
to state the v0.3 result as done, keeping the instruction to re-validate the
final release commit before the PR (that part stays true — the catalog pins
a SHA).

### F3 (housekeeping, pre-publish) — stray `lessons.md` at repo root

An untracked `lessons.md` sits at the repository root, written by a
test/demo run executing in the working directory (lessons append to
`<data_dir>/lessons.md`; a run with data_dir = cwd produced it). `.gitignore`
covers `__pycache__/`, `*.pyc`, `wt/`, `demo-output/`, `*.db*` — not
`lessons.md`. It would be swept into the public repo by a careless
`git add -A`. Fix: add `lessons.md` to `.gitignore` (root-level runtime
artifact), and delete the stray file (it is a runtime artifact, not source).

### F4 (README precision) — lesson ranking description does not match the store

README (Lesson writeback / architecture sections) says injection ranking is
"domain match, tag overlap, then newest" and "filled to ~9k". The code:
`hooks.py:49-60` calls `lessons_context(store, domain, tags)` with default
`limit=5` — the domain filter happens in SQL (`state.py:1032-1049
lessons_for`: exact-domain WHERE, then sort by tag overlap desc, then
created_at/id desc, then `[:limit]`). `lessons_context` fills to the full
10,000-char cap, truncating on lesson boundaries and appending a
"not shown" note that counts only lessons within the fetched window of 5 —
lessons beyond the limit are invisible to the note. So the true behavior is:
exact-domain lessons only, at most 5, tag-overlap then newest, 10k cap.
Either the README text or the limit/counting should change so they agree;
the parent's recommendation is to fix the README wording and make the
"not shown" count honest (count all lessons for the domain, not just the
fetched window) — but the mechanism choice is Claude's to propose in its
fix log with reasoning.

### F5 (README precision) — seat table overstates two seats

README panel table lists google's seat as `agy` and moonshot's as `kimi`
CLI. Code truth (`executors.py:392-418, 458ff`): `AgyExecutor.available()`
is an honest PATH check, but `generate()`/`judge()` ALWAYS return
unavailable (`AGY_UNVERIFIED_DETAIL`) because no headless mode was verified;
`BeamExecutor` (`executors.py:474-486`) is always unavailable by design.
The README discloses agy's status in the table's seat note ("headless mode
unverified — reports so") — keep that honesty, and make sure no other
README/SUBMISSION sentence implies eight *live* seats: the panel composes
from live seats, and in the build environment every real-model adapter
reported unavailable (only local solvers ran). Any sentence Claude finds
that overstates this gets corrected to the code's behavior.

## Named gaps — already logged, do NOT silently "fix"

- **GAP-STYLE-PREFERENCE-PENALTY**: the design's blind style-preference
  penalty is not applied. In a peer matrix the proxy reduces to raw leniency
  (self-scores are exactly the stripped cells); bias is measured and
  reported, calibration stays agreement-only. Reasoned in `matrix.py`
  docstring and BUILD-LOG. Leave as a named gap unless the operator directs
  otherwise.
- **Demo judged min is 4, not 6**: the demo contracts' judged minimum is 4
  because the labelled demo judges' evidence row is citation-based and
  sampler diffs carry few citations. Recorded, not inflated. Do not raise
  demo judge scores to hide it.
- **Judge seats are also dispatched as (unavailable) candidates** — v1 queue
  behavior generalized; noted for Phase 2. Not a defect to redesign in this
  pass.
- **Claude/Kimi/Qwen-CLI command shapes are documented-only** (binaries
  absent in the build sandbox; see BUILD-LOG-W3). Codex and Grok adapters
  were exercised to their real refusal paths. Agy headless is unverified by
  design (F5). Live-seat verification happens on the operator's PC, not in
  the sandbox.

## NOT RUN — never claim otherwise

- Real Hermes **install** (`hermes plugins install` + enable + restart):
  NOT RUN. Validation only.
- **Live multi-family contest** with real model seats: NOT RUN anywhere.
- Demo **video**: not filmed. Demo rehearsals: not run.
- **Publication**: no public repo, no catalog PR, no X post, no submission
  form. The catalog YAML's `sha:` field is a placeholder by design until the
  release commit exists.

## Invariants — never break these

1. Gate rank: hard > measured > judged. The matrix never overrules a gate.
2. No self-scoring, enforced in code (queue strips self-cells; unverifiable
   family = no scoring).
3. Baseline rule: no candidate runs unless acceptance fails on baseline.
4. Provenance: only `generated` artifacts with evidence can pass a gate.
5. Reports print only database rows; absent data prints "not run" /
   "not reported". Costs and token totals are never estimated.
6. Merge only after the human gate. Waivers: measured only, never hard.
7. Every fix lands with tests; the full suite (219 + new) must be green
   before anything is called done.
8. Local commits are authorized. **No push, ever** — publication is the
   operator's explicit word, not part of this goal.
9. Scope: this repository only (`hypercritic-build`). The operator's game
   tree and all other projects are out of scope.
10. Anything not run is logged NOT RUN, in those words.

## End goal (for the /goal text)

The plugin that ships to the Nous catalog is one where **every claim in the
README is true in the code**, the operator's floor rules hold in the
implementation (F1 above all), the docs state the verified state of the
tree as it actually is (F2, F4, F5), the repo is clean enough to publish
without a sweep (F3), and the full test suite proves it — so that the only
remaining steps are the ones that genuinely need the operator at his desk:
install, the live contest, filming, and his go to publish.
