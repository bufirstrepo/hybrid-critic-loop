# BUILD-LOG-A — Worker A: State layer

## What I built

All paths under `/home/hatch/workspace/hypercritic-build/`:

1. **`hybrid_critic_loop/__init__.py`** — exactly `__version__ = "0.1.0"`.
   Note: when I started, another worker/scaffold had already placed a
   version of this file with a docstring and `__all__`; since this file
   is my deliverable #1 with spec'd exact content, I replaced it with
   the single version line. No other worker's files were touched.
2. **`hybrid_critic_loop/errors.py`** — `HCLError(Exception)` base with
   subclasses `ContractError`, `BaselinePassedError`, `PanelError`,
   `GateError`, `ExecutorUnavailable`.
3. **`hybrid_critic_loop/state.py`** — `Store`, stdlib `sqlite3` only,
   WAL mode (`PRAGMA journal_mode=WAL`), exactly 11 tables:
   targets, contracts, candidates, measurements, scores, verdicts,
   final_answers, experts, routes, ledger, lessons. JSON-shaped columns
   stored as TEXT (`*_json` column suffix internally) and returned
   parsed. Full API implemented exactly as spec'd: targets, contracts
   (baseline run, status, observations capped at 50, latest open),
   candidates (id format `{contract_id}_{slot}` attempt 1,
   `{contract_id}_{slot}_r{attempt}` on retry; whitelisted
   `update_candidate`), measurements, scores, verdicts, final answers,
   experts (cost_tier enum validation, per-category win rate from a
   `category_record` JSON column, refusal rate from refusals/dispatches
   counters), routes (both demotion branches), ledger (outcome enum
   validation incl. the `escalated`/`observed` local extensions;
   summary sums are `None`, not 0, when every row for an expert is
   unreported), lessons (tag-overlap desc, then newest first, limit
   after ranking). Sequential ids (`tgt_/ctr_/vrd_/lsn_%04d`) derive
   from scanning existing ids for max numeric suffix (`_next_id`) — no
   meta table, so the table count stays exactly 11. Timestamps are UTC
   ISO strings; `last_scored` is the UTC date.
4. **`tests/test_state.py`** — 33 stdlib `unittest` tests, temp-dir DB,
   covering every item in the brief: exact 11-table schema, WAL mode,
   target/contract round-trips with parsed JSON, candidate id formats
   (attempt 1 + retries), measurement/score storage, verdict
   round-trip + latest, cost_tier rejection (`ValueError` on
   `"free (unverified)"`, qualifier accepted via `cost_tier_note`),
   category win-rate, refusal rate, both route-demotion branches,
   ledger summary None-handling, lesson ranking (overlap beats
   recency; newest-first within equal overlap), plus observation cap,
   sequential ids, explicit contract id, unknown-expert errors.

## Test evidence (commands actually run)

`cd /home/hatch/workspace/hypercritic-build && python3 -m unittest tests.test_state -v`

Result: **33 tests ran, all OK** (`Ran 33 tests in 0.070s` / `OK`).
Re-ran non-verbose before writing this log: `Ran 33 tests in 0.071s` / `OK`.

Supporting checks actually run:

- Error hierarchy + version assertion script: printed
  `errors hierarchy + version OK` (all five subclasses are
  `HCLError`/`Exception` subclasses; `__version__ == "0.1.0"`).
- Import audit (`grep` on imports): state.py imports only `json`,
  `sqlite3`, `datetime`, `pathlib` — stdlib only.
- Reopen/persistence check: created a DB, closed, reopened, target
  round-tripped; independent `sqlite3` table listing showed exactly
  the 11 spec tables. Printed `reopen OK; tables: 11 [...]`.

## Self-review (Principal Engineer checkup pass)

- **Correctness:** Re-read state.py after writing. Points verified:
  no `AUTOINCREMENT` anywhere (would create `sqlite_sequence` and
  break the exactly-11-tables invariant — the schema test asserts the
  full table set, not a filtered one); reserved-ish names quoted in
  SQL (`"primary"`, `"rule"`, `"commit"`); bool columns converted back
  to real `bool` on read (`pass`, `blind`, `escalated`); ledger
  aggregates computed in Python so None-handling is exact.
- **Evidence:** Every claim above cites a command run in this session
  and its real output. No results fabricated; test counts are the
  runner's own output.
- **Scope:** Only my four deliverable files written (plus this log).
  `git status --porcelain` shows the whole tree untracked (fresh
  repo); other workers' files (contracts.py, gates.py, BUILD-LOG-B.md,
  tests/__init__.py, etc.) were left untouched.
- **Deletion rule:** `__pycache__` dirs created by test runs were
  cleared without waiting — tier (b) regenerable build artifacts,
  noted here per the rule. Nothing else deleted.

## Deviations / judgment calls (spec silent; flagged for coordinator)

1. **Dispatches counter:** `record_expert_result` and `record_refusal`
   both increment the experts `dispatches` counter, so
   `refusal_rate = refusals / dispatches` counts every recorded
   dispatch outcome. My refusal test asserts 1 refusal after 1 result
   → 0.5. If the coordinator intended dispatches to count only via
   another path, this is the one line to change.
2. **Unknown-id behaviour:** `append_observation`,
   `record_expert_result`, `record_refusal`, and
   `apply_route_demotion` raise `KeyError` for unknown ids rather than
   silently no-oping.
3. **`update_candidate`** raises `ValueError` on fields outside the
   spec's allowed set.
4. **Unspecified return shapes:** `create_final_answer` returns the
   integer row id; `add_measurement`/`add_score`/`add_ledger` return
   row ids. `scores_for`/`ledger_rows` dicts mirror the argument names
   plus `id` and `created_at`. `get_expert` returns exactly the keys
   the spec lists (internal counters `refusals`, `dispatches`,
   `category_record` live in columns but aren't extra dict keys).
5. **Measurement value/threshold** are JSON-encoded in TEXT columns so
   ints/floats/strings round-trip with their original types.
6. **Observation cap 50** keeps the most recent 50.
