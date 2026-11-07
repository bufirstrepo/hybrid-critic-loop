# BUILD-LOG-D — Worker D (lessons, router, reports, hooks, plugin registration, CLI)

Scope: "Hybrid Critic Loop" Hermes plugin, worker D deliverables only.
Worked only in `/home/hatch/workspace/hypercritic-build/`. No git commits.
Stdlib only. Did not modify state.py / executors.py / gates.py / judges.py.

## Files written

| File | Contents |
|---|---|
| `hybrid_critic_loop/router.py` | `DEFAULT_ROUTES` (exact brief values), `ensure_routes`, `route_for`, `category_of`, `apply_lesson_route_change` |
| `hybrid_critic_loop/lessons.py` | `derive_outcome_cause` (cause/rules derived only from gate evidence), `write_lesson` (store row + `lessons.md` mirror), `lessons_context` (10,000-char cap, lesson-boundary truncation with count note) |
| `hybrid_critic_loop/reports.py` | `ledger_report`, `verdict_report`, `lessons_report` — DB rows only; `"ledger: not run"` / `"verdict: not run"` / `"lessons: not run"` when absent; `"not reported"` for None tokens/cost, `"not run"` for None win_rate, `3W-1L (0.75)` format otherwise |
| `hybrid_critic_loop/hooks.py` | `on_session_start` (writes `session.json`, never raises), `pre_llm_call` (returns `{"context": ...}` or None, swallows errors), `post_tool_call`, `post_llm_call` (append observations to latest open contract, never raise) |
| `hybrid_critic_loop/plugin.py` | `register(ctx)`: defensive data-dir chain (`ctx.plugin_data_dir()` → `plugins.plugin_storage` → `HCL_DATA_DIR`/`HERMES_HOME` env chain), tools `critic_run`/`critic_lessons`/`critic_ledger` with described JSON schemas, `/critic` command (run/lessons/ledger), `hermes critic` CLI registration (getattr-guarded, noted in `REGISTRATION_NOTES` when unavailable), four hooks bound. loop/contracts/executors imported lazily inside handlers only |
| `hybrid_critic_loop/cli.py` | Standalone argparse CLI (`run`/`lessons`/`ledger`/`session-start`), shared `add_parser`/`dispatch` for the Hermes CLI handler; exit 0 completed/awaiting, 2 escalated/refused-baseline, 1 error; interactive human-gate prompt only when stdin is a tty |
| `__init__.py` (repo root) | sys.path shim + `from hybrid_critic_loop.plugin import register` |
| `plugin.yaml` | manifest_version 2, api_version 1, name/version/description/author/license/kind/requires_hermes, provides_tools, provides_hooks, `python_dependencies: []` with stdlib-only comment, tags |
| `tests/test_lessons.py` | 27 tests with an in-memory FakeStore (no dependency on state.py) |
| `tests/test_plugin.py` | 12 tests with recording FakeCtx / FakeCtxNoCli |

## Real test results (commands actually run)

- `python3 -m unittest tests.test_lessons tests.test_plugin -v` → **Ran 39 tests, OK** (27 + 12).
- `python3 -m unittest discover -s tests` (whole tree, all workers) → **Ran 117 tests, OK**.
- `python3 -m py_compile` on all Worker D files → OK.
- `plugin.yaml` parsed with PyYAML → name `hybrid-critic-loop`, manifest 2, api 1, tools list as specified, `python_dependencies == []`.

## Integration checks beyond the required tests

1. **Real-Store check** (throwaway script in /tmp, since removed): Worker D modules
   against Worker A's real `state.Store` — routes seeded, a measured-failure lesson
   written (`lsn_0001`), `apply_lesson_route_change` flipped primary codex→grok and
   recorded an `observed` ledger row with the exact note text, `lessons_context` and
   all three reports rendered real rows. All assertions passed.
2. **End-to-end smoke**: `critic_run` tool handler (FakeCtx) with
   `contracts/demo1.json`, fixture `python-sampler` repo, `executors="local"`,
   `human_gate="approved"` → loop ran and returned
   `{"contract_id": "ctr_demo_1", "status": "escalated", "detail": "retry cap 3 reached", "verdict_id": "vrd_0001", "lesson_id": "lsn_0001"}`; verdict and lesson are in the store.
   The escalation is loop.py's own outcome for that run — recorded here as observed,
   not as a Worker D defect; the wiring (contract load → executors → loop → store)
   is what this smoke verifies.

## Deviations / adaptations (all defensive, brief behaviour preserved)

1. **Real Store signatures differ from the brief's subset in two places** (Worker A's
   state.py landed during this build): `upsert_route(category, primary, compete_with,
   fallback, judges, rule)` — route seeding tries `(category, route_dict)` then
   `(category, **route_dict)`, the latter matching; `add_ledger(target_id, contract_id,
   candidate_id, stage, expert_id, tokens_in, tokens_out, cost_usd, outcome, note=None)`
   — `apply_lesson_route_change` tries the brief's kwarg form first, then the real
   positional form. Both paths verified (FakeStore in tests, real Store in check 1).
   `"observed"` is a valid ledger outcome in the real store.
2. **Route seeding**: the brief's Store subset has no setter, so `ensure_routes`
   probes `set_route`/`upsert_route`/`ensure_route`/`save_route`/`put_route` and a
   plain `routes` dict before giving up — a Store whose `get_route` self-seeds needs
   none of it.
3. **Measurement rows**: brief says `pass == False`; state.py rows carry `pass`,
   gates.py rows carry `passed`. Derivation accepts either explicit False.
4. **plugin.py `_FallbackStore`**: a minimal in-memory Store subset used only when
   state.py cannot be imported (partial checkout); the real Store is always
   preferred. `register()` returns the store and exposes it as
   `plugin.ACTIVE_STORE` (used by the plugin tests to seed a lesson).
5. `hybrid_critic_loop/__init__.py` was created by another worker (`__version__`)
   and was left untouched.

## Principal Engineer checkup (self-review)

- **Correctness**: derivation outcome strings match the brief's f-string shapes
  verbatim and are asserted exactly in tests (alloc case); metric cause/rule maps
  cover alloc/ops/tick + default; route-change promote rule (primary if demote ≠
  primary, else compete_with) tested in both branches.
- **Evidence**: every result above cites a command run in this session; no numbers
  were invented. Reports print only store rows (tested `"not run"` strings).
- **Safety/invariants**: state is never written into the plugin directory (data-dir
  resolution is tested against `HCL_DATA_DIR`); hooks are observers that swallow
  exceptions; `pre_llm_call` returns None on any error; no registration-time import
  of loop/contracts/executors (registration tests pass on a bare FakeCtx).
- **Deletion rule**: only tier-(a) files were deleted (my own /tmp check scripts).
  `__pycache__/` dirs from test runs remain — tier (b) regenerable artifacts, noted
  here per the rule.
