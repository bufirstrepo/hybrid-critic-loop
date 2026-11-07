# BUILD LOG — Worker C (gates, rubrics, judged-gate protocol)

Scope: `hybrid_critic_loop/rubrics.py`, `hybrid_critic_loop/gates.py`,
`hybrid_critic_loop/judges.py`, `tests/test_gates.py`,
`tests/test_judges.py`, `tests/__init__.py`. Nothing else written.
No git commits. stdlib only.

## Files written

- `hybrid_critic_loop/rubrics.py` — `RUBRICS` exactly as briefed
  (`code_quality_v1`, `intent_v1`; version 1, scale 10, the specified
  item lists). `get_rubric(rubric_id)` returns a defensive copy and
  raises `GateError` on an unknown id.
- `hybrid_critic_loop/gates.py` — `compare` (six ops, `GateError` on
  unknown op); `HardResult` / `MeasureResult` / `CandidateGateResult`
  dataclasses with the briefed fields; `check_provenance` (only
  `provenance == "generated"` with a dict `evidence` carrying a truthy
  `log_hash` passes — mock/human artifacts can never pass a gate);
  `run_hard_items`; `run_measured_items` (sensor stdout parsed as a
  JSON object; unparseable / non-object / missing metric → `GateError`
  carrying the raw tail; every reading stored via
  `store.add_measurement` with the briefed argument order);
  `evaluate_candidate` with sticky rank order provenance → hard →
  measured (provenance failure runs no commands; hard failure never
  runs the sensors; `measured_pass is None` when no measured items
  exist or the stage was not reached).
- `hybrid_critic_loop/judges.py` — `select_panel` (route order first,
  then all experts sorted by id; family-exclusion + optional
  availability filter; `PanelError` naming the shortfall; final assert
  that every seated judge's family ∉ contest families);
  `recompute_panel` (documented fallback re-dispatch helper =
  select_panel over a route dict's "judges" or a bare list);
  `blind_text` (drops `diff --git` / `index ` / `commit ` / `Author:` /
  `Date:` lines, rewrites `wt/\S+` → `wt/<candidate>`, scrubs
  `/(home|Users)/\S+` → `<path>`, case-insensitively replaces strip
  terms with `<expert>`); `build_packet` (rubric header + measured
  summary + `=== CANDIDATE <label> ===` sections; re-runs `blind_text`
  with `[]` strip terms so path scrubbing cannot be skipped);
  `JudgedOutcome`; `run_judged_gate` (re-verifies panel family
  exclusion against live `judge_executors[j].family` and the
  candidates' families → `PanelError`; per-judge
  `random.Random(hash((seed, judge_id)) & 0xffffffff)` shuffle with
  A/B labels; per-judge measured summary labelled generically so
  judges see hard results and measurement values, never identities;
  score validation — every rubric item numeric AND non-empty evidence
  or that judge's score for that candidate is discarded; judges with
  no valid scores dropped; valid judges < needed → escalate with
  "judge panel fell below N after validation"; aggregation median of
  judge means ≥ min AND min item ≥ min − 2; any candidate spread >
  threshold → escalate with "judge spread {spread} above threshold
  {threshold}"; valid scores stored via `store.add_score(... "judged"
  ... blind=True, position_shown=1-based ...)`).
- `tests/test_gates.py`, `tests/test_judges.py` — unittest, temp dirs,
  an in-memory `FakeStore` stub (add_measurement / add_score /
  measurements_for / scores_for / get_expert) and stub judge
  executors; no dependency on Worker A's `state.py`.
- `tests/__init__.py` — empty package marker so
  `python3 -m unittest tests.test_gates tests.test_judges` resolves.

## Test results (real output)

Command: `cd /home/hatch/workspace/hypercritic-build && python3 -m unittest tests.test_gates tests.test_judges -v`

Result: **Ran 36 tests in 0.413s — OK** (21 in test_gates, 15 in
test_judges). Coverage includes every case the brief named: all six
compare ops + unknown op; provenance refusal for `mock`, `human`, and
missing/empty `log_hash`; hard exit mismatch; measured rows stored +
threshold failure; rank order proven by marker files (hard-fail →
sensor command never runs, file absent; provenance-fail → hard
command never runs); select_panel family exclusion + sorted extension
+ availability filter + PanelError shortfall; blind_text stripping
expert id / family / `wt/` path / commit metadata / absolute paths;
judged gate median pass, min-item below min−2 failing despite median
8.0 ≥ 7, spread 4.0 > 2.0 escalating, evidence-less scores discarded
(judge dropped; panel collapse escalates), contest-family judge
raising PanelError with zero scores stored; packets asserted to
contain no expert id/family while containing the measurement values.

Additional verification (not a deliverable test): a throwaway smoke
script ran `evaluate_candidate` and `run_judged_gate` against Worker
A's real `Store` (state.py, landed mid-task) on a temp DB — gate
passed, 1 measurement row and 1 score row landed in the real tables
with the expected values. Package-wide import check
(`contracts`, `gates`, `judges`, `rubrics`) passes — contracts.py's
lazy `from .gates import compare` resolves.

## Deviations / decisions

- `errors.py` already existed when I started (Worker A landed it
  during this task) with exactly the briefed classes
  (HCLError, ContractError, BaselinePassedError, PanelError,
  GateError, ExecutorUnavailable) — not created or modified by me.
  Likewise `shellrun.run_command` was used as-is (shlex-split, no
  shell; `exit_code` may be `None` on timeout/spawn failure, which
  `HardResult` accommodates).
- `get_rubric` returns the stored fields plus an `"id"` key so packet
  headers and judge calls can name the rubric; `RUBRICS` itself is
  byte-for-byte the briefed table. `build_packet` also resolves the
  id by reverse lookup when handed a bare rubric dict without one.
- Candidate id is read from `candidate["id"]`, falling back to
  `candidate["candidate_id"]`; contracts are read from
  `contract["acceptance"]` (a bare item list is also accepted).
- Score-shape tolerance in validation: an item may be
  `{"score": n, "evidence": s}` (also `value`/`rationale` spellings)
  or a bare number backed by entry-level evidence (per-item dict or
  single string). Anything else is discarded per "no rationale, no
  score". The brief's example shape is the dict form; the tolerance
  only widens what counts as evidence-bearing, never what counts as
  evidence-free.
- On panel-collapse escalation, `per_candidate` still reports the
  partial aggregates from any valid scores (zeros where none) rather
  than an empty dict, so the escalation is auditable.
- Spread escalation reports the worst (max) spread across candidates
  in the reason string.
- Per the deletion rule: only regenerable `__pycache__` directories
  from my own test runs were cleared (tier b, noted here). Nothing
  else was deleted; no file outside my deliverables was modified.

## Principal Engineer checkup (self-review, S0–S8 condensed)

- Correctness: every public function was exercised by a failing-first
  mental spec and a passing test; the two order-sensitive properties
  (no commands after provenance failure, no sensors after hard
  failure) are proven by filesystem side effects, not mocks of my own
  code. The family-exclusion rule has three layers: select_panel
  filter, select_panel assert, run_judged_gate re-verification.
- Evidence: test counts and the smoke results above are from actual
  runs in this session (36 tests, 0.413s, OK; real-Store smoke output
  quoted in my worker report). No result is asserted that was not
  observed.
- Failure honesty: sensor garbage and missing metrics are GateErrors
  with the raw tail attached, never silent passes or fabricated
  zeros; a judge returning None/raising is dropped, never scored.
- Known limits: `hash((seed, judge_id))` follows the brief literally;
  Python's string hashing is per-process randomized, so the A/B
  shuffle is seeded but not reproducible across processes. Panel
  judges beyond 26 candidates would exhaust A–Z labels (contest sizes
  here are 2). `blind_text` strip-term replacement is substring-based
  by design (case-insensitive), matching the brief.
