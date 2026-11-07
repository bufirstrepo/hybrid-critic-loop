# BUILD LOG — Workstream W1: State layer extensions (Hybrid Critic Loop v0.3)

Date: 2026-11-07 (America/New_York)
Scope owned: `hybrid_critic_loop/state.py`, `tests/test_state.py` only. No commit made.

## Files changed

- `hybrid_critic_loop/state.py` (+169/−7 region of the diff; see `git diff --stat` below)
  - Scores: `confidence REAL` + `details_json TEXT` added to CREATE TABLE; `add_score(..., confidence=None, details=None)` appended AFTER existing params (positional v1 callers unchanged); `_score_dict` returns `"confidence"` and `"details"` (parsed JSON, default `{}`).
  - Migration: `Store.__init__` now calls `_migrate()`, driven by `_MIGRATION_COLUMNS` — PRAGMA table_info check + `ALTER TABLE ADD COLUMN` for any missing column (scores: confidence/details_json; experts: validity_points/validity_cost_usd/calibration_n). Idempotent (second call verified no-op).
  - Events: new `events` table in `_SCHEMA` (`CREATE TABLE IF NOT EXISTS`, id INTEGER PK, contract_id/type/detail/created_at) — table count 11 → 12, module docstring updated. Methods `add_event(contract_id, type, detail) -> rowid` (str/None detail stored as-is, non-str JSON-dumped) and `events_for(contract_id=None, limit=100)` newest-first (ORDER BY id DESC), filtered by contract_id when given.
  - Experts: `validity_points REAL`, `validity_cost_usd REAL`, `calibration_n INTEGER` added to CREATE TABLE + migration. `record_validity(expert_id, validity_points, cost_usd=None)` accumulates points (NULL→0) and adds cost only when `cost_usd is not None` (NULL preserved when never reported). `update_expert_calibration(expert_id, value, n)` sets `judge_calibration` + `calibration_n`. `_expert_dict` gains `"calibration_n"` (default None), `"validity_points"`, `"validity_cost_usd"`, `"cost_per_validity_point"` (computed via shared `_cost_per_validity_point` helper: cost/points only when cost is not None and points not in (None, 0), else None). `upsert_expert` verified non-clobbering: its UPDATE touches only family/access/cost_tier/cost_tier_note/updated_at (also covered by a dedicated test).
  - `ledger_summary()`: each per-expert entry gains `"validity_points"`, `"validity_cost_usd"`, `"cost_per_validity_point"` pulled from `get_expert` (None ×3 when the expert row is missing). All existing keys unchanged.
  - Defensive `_col(row, name)` helper so dict builders tolerate pre-migration rows.
- `tests/test_state.py`
  - `EXPECTED_TABLES` gains `"events"`; `test_exactly_eleven_tables` renamed to `test_exactly_twelve_tables` (12 tables). This was required: the pre-existing exact-11 assertion is incompatible with the mandated events table.
  - New tests (14): `TestScoresV03` (2: confidence/details round-trip with the {row: {score, confidence, evidence, rationale}} shape; positional-call defaults None/`{}`), `TestMigrationV03` (1: hand-built v1-shaped scores+experts tables in a temp file → Store migrates both tables, old row reads back with defaults, new add_score round-trips), `TestEvents` (3: round-trip newest-first, per-contract filter + unfiltered, limit), `TestExpertValidity` (7: None defaults, cost-None leaves cost NULL + CPVP None, CPVP math exact 20.0 pts / $1.0 → 0.05, accumulation incl. cost-less call, unknown-expert KeyError ×2 paths, calibration value+n, upsert non-clobber), `TestLedgerSummaryValidity` (1: summary carries the three keys for reported / unreported / missing experts).
- `BUILD-LOG-W1.md` (this file).

Deletion rule compliance: nothing was deleted. Running the tests/py_compile regenerated `__pycache__` (tier (b) regenerable build artifact) — noted here per the rule; no tier (c) deletion was needed or performed.

## Commands run and REAL output

Baseline (before edits):

```
$ cd ~/workspace/hypercritic-build && python3 -m unittest tests.test_state
.................................
----------------------------------------------------------------------
Ran 33 tests in 0.081s

OK
```

After implementation:

```
$ cd ~/workspace/hypercritic-build && python3 -m unittest tests.test_state
...............................................
----------------------------------------------------------------------
Ran 47 tests in 0.135s

OK
```

Verbose re-run (final):

```
$ python3 -m unittest tests.test_state -v
... (all 47 tests listed "... ok", including test_v1_db_migrates,
     test_cost_per_validity_point_math_exact,
     test_record_validity_cost_none_leaves_cost_null,
     test_exactly_twelve_tables, test_summary_carries_validity_keys)
----------------------------------------------------------------------
Ran 47 tests in 0.112s

OK
```

47 = 33 pre-existing (one renamed, assertions updated for the 12th table) + 14 new.

Compile + diff scope:

```
$ python3 -m py_compile hybrid_critic_loop/state.py tests/test_state.py
PY_COMPILE_OK
$ git diff --stat -- hybrid_critic_loop/state.py tests/test_state.py
 hybrid_critic_loop/state.py | 169 +++++++++++++++++++++++++++-
 tests/test_state.py         | 264 +++++++++++++++++++++++++++++++++++++++++++-
 2 files changed, 426 insertions(+), 7 deletions(-)
```

Manual edge checks (python snippet against `Store(":memory:")`, real output):

```
add_score params: ['self', 'candidate_id', 'gate', 'rubric', 'judge_id', 'judge_family', 'blind', 'position_shown', 'items', 'mean', 'confidence', 'details']
add_event params: ['self', 'contract_id', 'type', 'detail']
events_for params: ['self', 'contract_id', 'limit']
record_validity params: ['self', 'expert_id', 'validity_points', 'cost_usd']
update_expert_calibration params: ['self', 'expert_id', 'value', 'n']
zero-points expert: {'validity_points': 0.0, 'validity_cost_usd': 0.5, 'cost_per_validity_point': None}
second _migrate(): ok, expert still readable: 0.0
positional score: None {}
event None contract: {'id': 1, 'contract_id': None, 'type': 'operator', 'detail': 'global event', 'created_at': '2026-11-07T14:56:00.544435+00:00'}
```

## Principal Engineer checkup pass

Performed as a manual review pass on this change (the checkup framework directory `/home/dino/Desktop/instructions/muse-code-checkup/` is on Dino's PC and does not exist on this VM — `ls` returned "No such file or directory" — so the formal `tools/checkup_gate.py` gate could not be executed here; this is recorded as a gap below, not papered over).

- Correctness: every spec item traced to code + a test: schema columns (scores/experts/events), migration via PRAGMA+ALTER, signature ordering (confidence/details appended — verified by inspect output above), NULL semantics (cost never invented; CPVP None when cost unreported or points None/0), calibration_n exposure, ledger keys, upsert non-clobber.
- Evidence: all numbers in this log are copied from actual command output in this session (33 baseline → 47 final, 0 failures). CPVP 0.05 is 1.0/20.0, asserted with assertAlmostEqual, not asserted from memory.
- No fabricated results: no other test module was run (per brief — sibling workstreams are mid-flight), and no claim is made about the full 122-test suite.
- Scope discipline: `git diff` for W1 covers only the two owned files; other modified paths in `git status` (contracts.py, executors.py, test_executors.py, tools/dev_solver.py, README.md, plugin.yaml, etc.) belong to sibling workstreams and were not touched by W1.
- Risks noted: (1) `_score_dict`/`_expert_dict` rely on migrated columns being present — guaranteed because `__init__` migrates before any query, plus `_col` tolerates absence. (2) `events.detail` for non-string input is JSON-dumped on write and returned as the stored TEXT (strings — the expected operator-event shape — round-trip exactly).

Result: PASS (manual pass; formal gate not runnable on this VM — see gaps).

## Gaps

- Formal Principal Engineer checkup gate (`tools/checkup_gate.py` against the PC-side framework) not run: framework path does not exist on this VM. Manual pass above stands in; a gate run can be repeated on the PC if Dino wants it.
- Full test suite not run, per brief (other workstreams mid-flight; imports may be temporarily inconsistent). Only `tests.test_state` was executed — it imports only `hybrid_critic_loop.state`, which no sibling edits, and it is fully green.
- Not committed, per brief.
