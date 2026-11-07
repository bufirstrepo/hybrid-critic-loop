# BUILD-LOG-W4 — Hybrid Critic Loop v0.3, Workstream W4

Scope owned by this worker (only these files touched):
`hybrid_critic_loop/contracts.py`, `hybrid_critic_loop/plugin.py`,
`hybrid_critic_loop/reports.py`, `plugin.yaml`,
`hybrid-critic-loop.yaml`, `tests/test_plugin.py`, and the NEW
`tests/test_reports.py`. No sibling files edited; nothing committed.

Standing rules carried in: the three-tier deletion rule — "(a)
Worker's own per-task temp dir: delete freely, no approval. (b)
Regenerable build artifacts (bin/, obj/, node_modules/.cache,
__pycache__, etc.): worker may clear without waiting, but must note
it in the cycle log. (c) Everything else: list exactly what would be
deleted and wait for explicit OK first." Deletions this cycle: none.
Test runs regenerated `__pycache__` (tier b, left in place, gitignored).

## What was implemented

### 1. contracts.py — OWNER REVIEW item 1 (two dissent numbers)
- Judged acceptance items now normalize with BOTH thresholds:
  `spread_threshold` (existing, default 2.0) and the new
  `dissent_threshold`: `float(item.get("dissent_threshold", 3.0))`
  semantics via a shared `_validated_threshold` helper.
- Both are validated as real numbers >= 0 (bools and non-numbers
  rejected); violations raise `ContractError` naming the item index,
  key, and value. Previously `spread_threshold` went through a bare
  `float(...)` that would have leaked ValueError/TypeError.
- The module docstring records the resolution: spread = judge-mean
  spread bound; dissent = per-row deviation-from-median bound; the
  spec's open question is closed with defaults 2.0 / 3.0.

Verification (throwaway script, real output):
```
defaults: 2.0 3.0
explicit: 1.5 0.5
ContractError ok: {'dissent_threshold': -1} -> acceptance[1] judged item 'dissent_threshold' must be >= 0, got -1.0
ContractError ok: {'dissent_threshold': 'high'} -> acceptance[1] judged item 'dissent_threshold' must be a number, got 'high'
ContractError ok: {'spread_threshold': -0.1} -> acceptance[1] judged item 'spread_threshold' must be >= 0, got -0.1
ContractError ok: {'spread_threshold': None} -> acceptance[1] judged item 'spread_threshold' must be a number, got None
ContractError ok: {'dissent_threshold': True} -> acceptance[1] judged item 'dissent_threshold' must be a number, got True
```

### 2. plugin.py — critic_matrix tool + events surface
- NEW TOOL `critic_matrix`: schema `{contract_id: string (required),
  attempt: integer optional}`; handler delegates to
  `reports.build_matrix_view(store, contract_id, attempt=...)` and
  returns its JSON: `contract_id`, `rubric="validity_matrix_v1"`,
  `rows`, `candidates` (per candidate: `expert_id`, `family`,
  `cells` keyed by judge with `judge_family` / `items` / `confidence`
  / `details` / `mean`, `row_results` from the verdict's judged block
  when present, `dissent`), `panel_status`, and `note="not run"` when
  no `gate="matrix"` score rows exist. Registered via
  `ctx.register_tool` alongside the existing three tools.
- `_FallbackStore`: it already had `latest_verdict` + `get_contract`;
  added minimal `candidates_for(contract_id, attempt=None) -> []` and
  `scores_for(candidate_id) -> []` stubs so the matrix handler cannot
  crash on a bare checkout.
- EVENTS SURFACE (owner review item 8): `critic_ledger` handler adds
  `"events": store.events_for(limit=50)` ONLY when the store has
  `events_for` (hasattr guard, TypeError/Exception fallbacks) — no key
  is promised for older stores. The CLI report gains the same section
  through `ledger_report` (see below), which is what `cli.py` prints.
  No phone/ping surface is promised anywhere in the owned files.

### 3. reports.py
- `build_matrix_view(store, contract_id, attempt=None)` (new, shared
  by the tool and the text report): reads candidates via
  `candidates_for`, score rows via `scores_for` filtered to
  `gate="matrix"`, verdict via `latest_verdict`. Row names come from
  the `validity_matrix_v1` rubric when a sibling module registers it,
  else from the recorded score items (first-seen order), else the
  canonical fallback `["correctness", "completeness", "evidence",
  "reproducibility"]`. Cell parsing tolerates bare per-row numbers,
  per-row dicts (score/confidence/evidence spellings), score-column
  confidence/details, and meta keys inside `items`. Dissent merges
  verdict-recorded entries with entries computed from the recorded
  scores: a judge whose row score deviates from the row median by
  more than the contract's `dissent_threshold` (from `get_contract`,
  default 3.0) is flagged with score/median/deviation/rationale.
- `matrix_report(store, contract_id)` (new): `"matrix: not run"` when
  empty; otherwise a header, a `panel_status:` line, and per candidate
  one line per row (`row: judge=score, ...`) with dissent cells
  suffixed ` DISSENT`, plus one `dissent:` line per entry. DB rows
  only.
- `ledger_report`: every expert line gains `cpvp=<value>` —
  `cost_per_validity_point` formatted `f"{v:.4f}"`, or
  `cpvp=not reported` when None/missing. `validity_points=<value>` is
  appended only when the summary row carries it, and
  `cal=<value>(n=<n>)` only when the row carries calibration
  (`calibration`/`judge_calibration` keys, dict or scalar, with
  `calibration_n`/`calibration_count`/`calibration_samples`/`n`
  aliases) — all read defensively with `.get`; absent keys print
  nothing extra. Appends an `events:` section (one line per event, or
  `events: not run`) when the store has `events_for`.
- `verdict_report`: when the verdict's judged dict contains dissent
  entries (top-level `dissent` list, per-candidate sub-blocks, or
  `candidates`/`per_candidate` containers), prints one line each —
  `dissent: candidate=… row=… judge=… score=… vs median=… — rationale`;
  prints `panel_status: …` when the judged block (or verdict) carries
  it.

### 4. Manifests
- `plugin.yaml` `provides_tools` and `hybrid-critic-loop.yaml`
  `capabilities.provides_tools` both now list, identically and in
  this order: `critic_run, critic_matrix, critic_lessons,
  critic_ledger`; hooks unchanged (the 4 lifecycle hooks);
  `requires_hermes: ">=0.21.0"` in both (pre-existing uncommitted
  change in the tree, preserved).

Manifest check (PyYAML parse, real output):
```
plugin tools: ['critic_run', 'critic_matrix', 'critic_lessons', 'critic_ledger'] | hooks: ['on_session_start', 'pre_llm_call', 'post_tool_call', 'post_llm_call'] | requires: >=0.21.0
catalog tools: ['critic_run', 'critic_matrix', 'critic_lessons', 'critic_ledger'] | hooks: ['on_session_start', 'pre_llm_call', 'post_tool_call', 'post_llm_call'] | requires: >=0.21.0
tools match: True
hooks match: True
```

## Tests

- `tests/test_plugin.py`: the two registration assertions updated for
  the new tool (tool set is now 4 — noted here per brief); new tests:
  ledger `events` key present iff the store has `events_for`;
  `critic_matrix` requires contract_id; `critic_matrix` on an empty
  store returns `note="not run"` with 4 rows and empty candidates;
  populated shape on the real Store seeded with plain positional
  `add_score` calls only, including proof that a `gate="judged"` row
  for the same candidate does not leak into the matrix.
- `tests/test_reports.py` (NEW): ledger cpvp=not reported on the real
  Store; numeric cpvp after `record_validity` — skip-guarded with
  `unittest.skipUnless(hasattr(Store, "record_validity"))` and the
  call itself fills arguments by inspected parameter name, skipping
  honestly if the sibling signature/semantics are unsettled; numeric
  cpvp + `cal=0.8700(n=14)` from a fake summary row; no-calibration
  rows print no `cal=`; events section present only with `events_for`;
  `matrix_report` "not run" and a seeded fake-store grid rendering
  `judge-3=2.0 DISSENT` (deviation 7.0 from median 9.0 > default 3.0);
  panel_status from a verdict; `verdict_report` dissent + panel
  status lines and quiet behaviour without them.

### Parallel-landing note
W1 landed in the shared tree DURING this cycle: `state.py` gained
`record_validity(expert_id, validity_points, cost_usd=None)`,
`events_for(contract_id=None, limit=100)`, `cost_per_validity_point`
+ `validity_points` in `ledger_summary`, and expert calibration
fields (`judge_calibration`, `calibration_n` — on the expert dict,
not the ledger summary). Consequences, all verified after the landing:
the skip-guarded cpvp test initially skipped on a test-side bug (the
expert row must exist first — `record_validity` raises KeyError for
unknown experts); fixed by upserting the expert in the test, after
which it RUNS and passes (cpvp = 5.0 / 10.0 = 0.5000). The guarded
`events_for(limit=50)` call is compatible with W1's signature. W1's
ledger summary carries no calibration keys, so real ledger lines
correctly print no `cal=` — the display path is proven by the
fake-summary test.

### Commands and real outputs
```
$ cd ~/workspace/hypercritic-build && python3 -m unittest tests.test_plugin tests.test_reports
Ran 27 tests in 0.052s
OK            (final run: zero skips)

$ cd ~/workspace/hypercritic-build && python3 -m unittest discover -s tests
Ran 189 tests in 5.566s
OK            (whole tree, incl. siblings' in-flight W1/W3 tests)
```

End-to-end smoke against the real (W1-landed) Store, real output:
```
view rows: ['correctness', 'evidence'] | note: None | dissent count: 1
matrix for ctr-smoke (validity_matrix_v1):
panel_status: not reported
candidate ctr-smoke_a (expert=exp-a, family=fam-a):
correctness: judge-1=9.0, judge-2=9.0, judge-3=2.0 DISSENT
evidence: judge-1=9.0, judge-2=8.0, judge-3=9.0
dissent: candidate=ctr-smoke_a row=correctness judge=judge-3 score=2.0 vs median=9.0 — score 2.0 deviates from row median 9.0 by 7.00 (> dissent_threshold 3.0)
ledger:
exp-a: dispatches=1 completed=1 refused=0 timeout=0 tokens_in=10 tokens_out=5 cost_usd=1.5 win_rate=not run cpvp=not reported
events: not run
(after record_validity("exp-a", 10.0, 5.0):)
exp-a: dispatches=1 completed=1 refused=0 timeout=0 tokens_in=10 tokens_out=5 cost_usd=1.5 win_rate=not run cpvp=0.5000 validity_points=10.0
events: not run
```

## Principal Engineer checkup (self-review, S0–S8 condensed)

- Correctness: every new surface is exercised by a test against the
  real Store or a duck-typed fake — registration set, matrix
  empty/populated, gate filtering (a judged row provably excluded),
  dissent math (median 9.0, deviation 7.0 > 3.0), cpvp formatting and
  its not-reported fallback, threshold validation (5 ContractError
  cases run by hand, output above).
- Evidence: all numbers quoted here are from runs in this session
  (27 targeted tests OK with zero skips; 189 full-suite tests OK).
  Nothing is asserted from a sibling's log; W1's shapes were read
  from the landed `state.py` source and re-verified by rerunning.
- Failure honesty: absent data renders as "not run" / "not reported"
  / absent key — the matrix note, cpvp fallback, events absence, and
  panel_status "not reported" all follow the reports module's rule.
- Defensive seams: every sibling-owned surface is consumed through
  hasattr/.get/try guards (events_for, add_score kwargs untouched —
  tests seed positionally only, candidates_for/scores_for TypeError
  fallbacks, rubric lookup failure falls back to observed rows), so
  W4 works against the pre-W1 tree, the landed W1 tree, and partial
  checkouts (fallback store) alike — the pre-landing run passed too
  (27 tests, 1 honest skip at that moment).
- Manifest discipline: registrations, plugin.yaml, and the catalog
  YAML were parsed and compared exactly (tools + hooks +
  requires_hermes) — match confirmed above.
- Scope: no sibling file edited, no commit made, no deletions; the
  only pre-existing uncommitted manifest change (requires_hermes
  floor) was preserved, not reverted.

## Gaps / open items

- Row names: `validity_matrix_v1` is not yet registered in
  `rubrics.py` in this tree (sibling workstream). Until it lands, row
  names derive from recorded score items, with the canonical
  4-row fallback for the empty view. If the sibling rubric names
  different rows, the rubric's names win automatically once it lands
  (rubric items take precedence, observed extras appended).
- Calibration display on real ledger lines awaits the summary row
  carrying calibration: W1 put `judge_calibration`/`calibration_n` on
  the expert dict but not in `ledger_summary`, so real rows print no
  `cal=` yet; the defensive reader accepts the keys the moment a
  summary provides them.
- `panel_status` likewise renders only when a verdict's judged block
  records it (the matrix gate that writes it is a sibling
  workstream); otherwise the JSON carries `panel_status: null` and
  the text report prints "not reported".
- The CLI `ledger` subcommand itself (cli.py, not owned here) needed
  no change: it prints `ledger_report`, which now carries the events
  section.
