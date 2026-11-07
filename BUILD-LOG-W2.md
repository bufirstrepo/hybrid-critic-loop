# BUILD LOG — Workstream W2 (peer validity matrix engine, Hybrid Critic Loop v0.3)

Scope: `hybrid_critic_loop/matrix.py` (NEW) and `tests/test_matrix.py`
(NEW) only, plus this log. No other file written, nothing committed.
Read first, per brief: `judges.py` (blind_text / build_packet /
_measured_summary_for_labels / _validate_candidate_score patterns),
`rubrics.py`, `errors.py`, `gates.py` (CandidateGateResult shape),
`tests/test_judges.py` (FakeStore pattern), `state.py`. W1's parallel
store changes landed during this cycle and were re-read before
integration testing: `add_score(..., confidence=None, details=None)`
and `update_expert_calibration(expert_id, value, n)` are present in
the tree and are exactly the signatures matrix.py calls.

## Files written

- `hybrid_critic_loop/matrix.py` — stdlib only.
  - `VALIDITY_RUBRIC` = {id "validity_matrix_v1", version 1, scale 10,
    items [question_fidelity, completeness, evidence, test_agreement]}
    defined locally; `validity_rubric()` returns a defensive copy.
    `rubrics.py` / `judges.get_rubric` untouched.
  - `build_matrix_cells(panel_families_in_order, candidates)` — the
    queue strips self-cells in code: one dict
    {judge_family_slot, judge_family, candidate_id, candidate_family}
    per (panel family, candidate) pair with judge family != candidate
    family, panel order then candidate order. Panel entries may be
    bare family names, dicts, or objects with `.family`.
  - `weighted_median(values_weights, weights=None)` — lower weighted
    median (sort by value; first value whose cumulative weight >= half
    the total). Accepts (value, weight) pairs, a {value: weight}
    dict, or parallel lists. None on empty / non-positive total weight;
    aggregation falls back to the plain median only if every weight is
    degenerate-zero.
  - `run_matrix(store, contract, candidates, gate_results, diffs,
    peer_executors, *, min_score, dissent_threshold=3.0,
    spread_threshold=2.0, calibrations=None, seed=0)` -> MatrixOutcome
    {per_candidate, dissent, rerun_requests, panel_status, escalated,
    escalation_reason, peers_used, effective_calibrations}.
    - Peers are only shown eligible (non-self) candidates: per-peer
      candidate set comes from build_matrix_cells, labels assigned
      per peer from a seeded shuffle in the run_judged_gate pattern
      (`random.Random(hash((seed, judge_id)) & 0xffffffff)`).
    - Packet (build_matrix_packet, style of judges.build_packet):
      rubric header, ORIGINAL contract["task"] verbatim plus a compact
      acceptance listing (question_fidelity is scored against this,
      never a candidate restatement), the labelled hard/measured
      summary via judges._measured_summary_for_labels, then labelled
      diffs pre-blinded with judges.blind_text using strip terms =
      every candidate/peer expert id and family name.
    - Cell validation: every row present, score numeric in [0,10],
      confidence numeric in [0,1], evidence AND rationale non-empty
      strings; otherwise the whole cell is discarded.
    - Persistence per valid cell:
      `store.add_score(cid, "matrix", "validity_matrix_v1", judge_id,
      judge_family, True, position, items={row: score}, mean=mean,
      confidence=<mean row confidence>, details={row: {score,
      confidence, evidence, rationale}})`.
    - Aggregation per candidate: per row, weighted median across peers
      (weight = effective calibration x that row's confidence);
      median_mean = weighted median of per-peer unweighted means
      (weight = effective calibration x mean cell confidence);
      min_row = min row median; spread = max-min per-peer mean;
      mean_confidence over all cells' row confidences; peers =
      distinct scoring families in scoring order;
      pass = has cells AND median_mean >= min AND min_row >= min - 2.
    - Panel floor: candidate panel_status "degraded" if < 2 distinct
      peer families scored it OR < 2 cells; overall panel_status
      "degraded" if any candidate is degraded, else "full".
    - Dissent: |score - row weighted median| > dissent_threshold ->
      entry {candidate_id, row, judge_id, judge_family, score,
      row_median, evidence, rationale, cites_gate}. cites_gate =
      "measured" if the evidence+rationale contains a measurement
      metric name from that candidate's gate_results; "hard" if it
      contains a full hard cmd string (contract acceptance or the
      candidate's HardResult cmds) or a distinctive token of one
      (test-file path / runner token, len >= 4, stopwords excluded);
      case-insensitive substring matching, measured checked first.
      rerun_requests[cid] = first cited gate class per candidate
      (candidate order, rubric row order, peer order). Detection and
      reporting only — this module never re-runs a gate.
    - Escalation: any candidate with cells whose mean confidence
      < 0.4, or any candidate spread > spread_threshold, sets
      escalated with a reason naming the candidate and the value.
      A candidate with zero valid cells gets pass=False, rows 0.0 and
      a degraded panel, but no low-confidence escalation (there is no
      confidence to average) — decision recorded here.
    - Effective calibration: calibrations=None -> every family at the
      0.5 prior. A dict entry {calibration, n, active} (the
      compute_calibration shape) yields the prior whenever n < 5 or
      active is False, regardless of the stored value; a bare numeric
      entry carries no count and is taken as already active, clamped
      to [0, 1]. The loop is expected to pass compute_calibration's
      result in.
  - `compute_calibration(store, experts)` ->
    {family: {calibration, n, active, bias}}.
    - Outcomes: positive = winner of a non-escalated verdict;
      negative = in a verdict's eliminated list, OR eliminated_at in
      hard/measured/provenance on the candidate record, OR in the
      verdict's judged summary with pass False. Cells = score rows
      with gate in ("matrix", "judged") on decidable candidates;
      a cell agrees if (cell score >= 5) == positive, where the cell
      score is the row's stored mean (falling back to the mean of its
      items). calibration = (agree + 2*0.5) / (n + 2); active = n >= 5.
    - Enumeration is duck-typed in order: bulk accessors
      (all_candidates / list_candidates / all_scores / ...), the
      FakeStore attribute pattern (`.candidates`, `.scores`),
      contracts + candidates_for, score ids + get_candidate /
      latest_verdict, and finally — because the real Store's public
      API has no bulk candidate listing — read-only id SELECTs over
      the Store's own sqlite connection (`store._conn`), after which
      all data still comes through scores_for / latest_verdict /
      get_candidate. This path is exercised against the real Store in
      the tests (see below).
    - Write-back: when the store has update_expert_calibration
      (hasattr-guarded), it is called per expert of each family with
      (expert_id, calibration, n).
- `tests/test_matrix.py` — 16 tests, FakeStore pattern from
  test_judges.py extended with confidence/details add_score kwargs.
  Stub peers locate candidates inside the blinded packet by a unique
  marker token planted in each diff, so per-peer label shuffles
  cannot change results. Coverage maps 1:1 onto the brief's list:
  self-cell stripping (3 families -> 6 cells, exact pair set) and the
  N x (peers-1) formula at N=4; weighted-median unit cases; a full
  3-peer run with hand-computed weighted medians (see below);
  calibration weighting changing a result vs unweighted; a peer with
  n=3 stored calibration 0.95 forced to the 0.5 prior (median flips
  from 8 to 4 — arithmetic in the test comment); pass-rule boundary
  (row at exactly min-2 passes, min-2.1 fails, both with median
  >= min); dissent on an outlier cell with a measurement citation ->
  rerun_requests "measured", and a second dissent citing the full
  hard test command -> "hard"; uniform confidence 0.3 -> escalation
  naming candidate and mean; 2-family contest -> degraded per
  candidate and overall; spread 4.0 > 2.0 -> escalation; a cell with
  an empty rationale discarded (that candidate keeps only its other
  peer's cell, the peer's other cells still count); packet contents
  (no self marker, no expert id/family strings, contract task text,
  acceptance cmd, coverage=90 all asserted); compute_calibration
  agreement/Laplace/active/write-back against the FakeStore, the bias
  test below, and compute_calibration against the REAL Store
  (tempdir sqlite: fam-x (2+1)/(2+2)=0.75 over a winner + a
  hard/measured-survivor loser, write-back verified via get_expert).

## Named gap — GAP-STYLE-PREFERENCE-PENALTY (penalty NOT implemented)

The style-preference penalty is implemented as agreement-only
calibration, with the proxy quantity still computed and returned as
`bias`. Reasoning, as required by the brief ("never fake it"):

1. The primary definition is impossible by construction: a family's
   scores to candidates of its own family are exactly the self-cells
   the matrix strips, so no such score exists to average.
2. The substituted proxy — F's mean score to candidates in contests
   where F also had a candidate, minus the panel median for those
   candidates — carries no style information. In a peer matrix every
   scoring family is a contestant, so the conditioning set is *all*
   matrix contests; the quantity reduces to F's overall leniency
   relative to the panel on candidates F happened to score. Nothing
   in it measures similarity between F's own output style and the
   candidates F scores highly.
3. Applying it would penalise calibration for leniency per se:
   a generous-but-accurate family (scores high, agrees with
   decidable outcomes) would be down-weighted by a factor derived
   from the same scores that its agreement calibration already
   judged correct. That double-counts one signal and corrupts the
   other.
4. A coherent version needs data the matrix alone never produces:
   either a style-similarity feature between candidate diffs, or a
   difference-in-differences of F's relative generosity between
   contest judging (matrix) and non-contest judging (the old judged
   gate, where judges are non-contestants). Either is a design
   decision for the owner, not a formula to improvise in this module.

The test `test_bias_measured_but_penalty_not_applied_named_gap`
pins the behaviour: bias is measured as exactly 2.0 (score 9 vs panel
median 7 on the contested candidate) while calibration stays at the
agreement-only (1+1)/(1+2) = 0.6667 — a 0.9-per-full-point penalty
would have produced 0.54, so the test would catch a silent penalty.

## Commands run and REAL outputs

- `cd ~/workspace/hypercritic-build && python3 -m unittest tests.test_matrix -v`
  Final run: **Ran 16 tests in 0.013s — OK** (all 16 listed ok:
  4 rubric/cells, 9 run-matrix, 3 calibration).
  One intermediate failure was a test-design error, not a code error:
  the first aggregation test forgot that peer means 4 vs 8 give
  spread 4.0 > the 2.0 default, so the outcome correctly escalated;
  the test now passes spread_threshold=99.0 to isolate aggregation
  (spread escalation has its own dedicated test).
- `cd ~/workspace/hypercritic-build && python3 -m unittest discover -s tests`
  **Ran 205 tests in 5.686s — OK.** (Whole tree at the time of the
  run, including sibling workstreams' in-flight files; matrix.py is
  purely additive — a new module nothing else imports yet — so this
  is context that nothing regressed, not a claim about siblings'
  work.)
- Signature/rubric smoke check via `python3 -c` importing run_matrix,
  compute_calibration, build_matrix_cells, validity_rubric: printed
  the briefed signatures verbatim and the rubric dict
  {id validity_matrix_v1, version 1, scale 10, items [the four rows]}.

## Principal Engineer checkup (self-review, S0–S8 condensed)

- S0 Scope: change checkup on exactly two new files + this log.
  Inventory verified with `git status --short`: matrix.py and
  test_matrix.py are the only new files from this workstream; other
  modified/untracked entries are sibling workstreams (W1/W3/W4/W5),
  untouched.
- S1 Baseline: siblings' contracts read before writing; judges.py's
  aggregation, blinding and store-call shapes are the baseline
  semantics and are reused (blind_text, _measured_summary_for_labels)
  rather than re-implemented.
- S2 Design trace: dispatch (build_matrix_cells) -> per-peer packet
  (eligible candidates only) -> validation -> persistence -> per-row
  and per-peer weighted medians -> pass rule -> dissent/rerun ->
  escalation -> outcome. Calibration is a separate read-only pass
  over store history plus an optional write-back.
- S3/S4 Falsification built into the tests: each headline property
  has a case that fails under the obvious wrong implementation —
  unweighted median (would give 6.0, test expects 4.0), stored
  calibration below 5 cells (would give median 8, test expects 4),
  a silent style penalty (would give 0.54, test expects 0.6667),
  self-cells dispatched (packet marker assertion), row at min-2.1
  passing (boundary test), a rationale-less cell counting
  (discard test).
- S5 Implementation: as above; no sibling file edited, no commit.
- S6 Re-verification: after a final cosmetic cleanup of the
  enumeration helper (two expression-style fixes, no behaviour
  change), tests.test_matrix was re-run in full: 16 OK.
- S7 Failure honesty: a failing/raising peer executor yields no
  cells and is dropped, never scored; a candidate with no valid
  cells reports zeros + pass False + degraded, never a fabricated
  pass; dissent detection reports only, it does not re-run gates;
  unknown-outcome candidates contribute no calibration cells.
- S8 Evidence: every number above is from a command actually run in
  this session and quoted from its output. Remaining open items are
  the named gap and the known limits below — recorded, not hidden.

## Known limits / decisions for the coordinator

- `hash((seed, judge_id))` shuffle seeding inherits judges.py's
  known limit: Python string hashing is per-process randomised, so
  label assignment is seeded but not reproducible across processes.
  Aggregation is label-independent, so no result depends on it.
- `calibrations=None` in run_matrix means "all peers at the prior";
  the loop should call compute_calibration and pass its result for
  weighting to mean anything across contests.
- If dissents on one candidate cite both gate classes, rerun_requests
  keeps the first cited class (candidate / row / peer order); the
  full dissent list preserves both citations for the loop to inspect.
- compute_calibration's sqlite id fallback reads `store._conn`
  (read-only SELECTs of candidate ids only). If a future Store hides
  the connection without adding a bulk candidate listing,
  enumeration degrades to the accessor/attribute paths and families
  with no discoverable candidates report n=0 at the prior.
- Deletion rule: only regenerable `__pycache__` bytecode for the two
  new modules was cleared after the final test run (tier b, noted
  here). Nothing else deleted; nothing outside the two deliverables
  and this log was modified.
