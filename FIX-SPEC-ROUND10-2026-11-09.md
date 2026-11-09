# FIX SPEC — Round 10 (2026-11-09)

Base: `d668b9c`. Consolidated report:
`~/workspace/goals/hybrid-critic-loop-hackathon-entry/files/mm-review10-consolidated-2026-11-09.md`.
Raw lane reports: `~/workspace/mm-review10-2026-11-09/1–6`.
Every MEDIUM was reproduced by the parent agent personally before
entering this spec (probes `~/workspace/.tmp-r10/probe3.py` and
`m3b.py`; document items spot-verified by grep).

Rules for every unit: work in `~/workspace/mm-work-2026-11-08`; run
tests with `TMPDIR=~/workspace/.tmp-r10fix` (create it); commit ONLY
your unit's files with `git commit -m <msg> -- <paths>`; never push;
check `git show --stat HEAD` after committing for foreign files (a
sibling unit commits concurrently — if you swept one in, report it,
do not reset). Each unit's new tests must FAIL with its fix severed
(mutation-check yourself and say so in the report). Report back: the
commit hash, the exact test-file list run with counts (builders: run
the FULL suite serially and report its real tail, reading the actual
result line, not a piped exit code), and anything the spec got wrong.

---

## Unit N1 — chain: completion binds identity, not a string (chain.py + tests)

Round-10 lanes 1+3 (MEDIUM, convergent). `_contract_completed`
(`chain.py:1203`) currently returns True when (a) the contract status
is terminal-completed — with NO answer row at all, or (b) a
`final_answers` row's recorded decision equals the latest verdict's
`human_gate`. Both break:

- Status flip alone (`set_contract_status(cid,"completed")` on an
  awaiting book, verdict pending, no row) → completed, cert issues.
- Verdict row planted via the store's own `create_verdict`
  (`human_gate='approved'`) + answer row planted with decision
  `approved` and commit `git:deadbeefcafe` → completed, cert issues,
  `verify_certificate` True, stamp minted for a run that never
  happened; the gate refuses the same book. The plant also wedges
  genuine convergence (one-row guard) while certs mint.
- The row read is `LIMIT 1` unordered: with two rows, insertion order
  decides.

The gate's own durable completion record already exists: every
genuine completion appends a `gate_completion` observation to the
contract (`loop.py` ~:741 converge path and ~:2694 fresh path) whose
payload carries `decision` and `commit` (the merged sha, BARE — the
final_answers row stores `git:<sha>`), and the loop's own comment
calls it "the durable merge record". The observation is written
BEFORE `set_contract_status(..., "completed")` on both paths, so a
genuinely completed book always carries one.

New completion rule on the real Store (all surfaces present):

1. **A `final_answers` row is REQUIRED.** No row → not completed,
   whatever the status says. Read the row deterministically: latest
   by insertion (`ORDER BY rowid DESC LIMIT 1`), and read its
   `candidate_id`, `commit`, and `human_gate` columns.
2. **Identity corroboration** — the row counts when EITHER:
   - **Gate-record leg:** a `gate_completion` observation exists on
     the contract whose `decision` equals the row's recorded
     decision AND whose `commit` equals the row's commit with the
     row's `git:` prefix stripped. (This is the gate's own record of
     the merge that produced the answer — the answer's identity, not
     a decision string two plantable rows can agree on.)
   - **Verdict leg (crash window):** status is NOT terminal, no
     gate-record match, and the latest verdict's `human_gate` is an
     answer-producing decision equal to the row's decision AND the
     verdict's `winner` equals the row's `candidate_id` AND the
     row's commit is in `git:` form. This preserves the M3 crash
     window (genuine answer written, process died before status)
     while binding the winner — a plant must now name the verdict's
     actual winner, not any candidate.
   The terminal-status path is subsumed: a genuine completed book
   has the row + the gate record (leg 1). A bare status flip has
   neither.
3. The docstring is rewritten to state exactly these legs and the
   residual honestly: an actor with owner-class store writes who
   forges the row AND the contract's observation log consistently
   still passes — the same posture as the counted-row referential
   exemption (round 3, disclosed); what this fix removes is
   completion-by-agreement between any two plantable rows, and
   completion with no answer at all.
4. Duck-typed degradation is preserved exactly: a store with NO
   completion surface degrades as today (`return not surface`);
   partial surfaces behave per the existing careful fallbacks (keep
   them, adapting to the row-required rule — where the row cannot be
   read at all and no surface exists, today's degradation governs;
   where the status surface exists but errors and no row was found,
   not completed, as today).

Tests (`tests/test_fix_round10_chain.py`): the status-flip-only
plant → False + issuance None; the two-row plant (verdict +
fabricated-commit answer) → False; the same plant WITH the winner
bound but a fabricated commit and no gate record on a non-terminal
book → False (verdict leg's `git:` form passes but… — no: fabricated
`git:deadbeefcafe` IS git:-form; the verdict leg must fail it how?
It cannot, store-locally. So this case is NOT a test — it is the
documented residual IF the plant also sets winner == candidate. Pin
instead: winner mismatch → False; non-`git:` commit on the verdict
leg → False; genuine fresh completion (full loop run or the
existing fixtures' production form) → True; converged completion →
True; crash window (row + decided verdict + winner bound + git:
commit, status non-terminal, NO observation) → True; escalated +
planted row → False (round-9 regression); decision mismatch →
False; two-row book, latest row governs. Re-run
`tests/test_fix_round9_chain.py` and the full suite.

## Unit N2 — documents (README.md + SUBMISSION.md only)

Round-10 lane 2 (4 MEDIUM + 1 LOW; each item verified against its
code anchor by the lane; items 1 and 4 spot-verified by the parent).
Make ONLY these corrections, in the documents' own voice, no other
edits:

1. README.md:19 — "two of the eight seats (agy, Beam) decline every
   dispatch" is wrong: the panel is nine seats (`README.md:53`;
   `default_executors()` returns 9), seven can generate. The "eight"
   counts model families. Rewrite the clause so families and seats
   are not conflated (e.g. "two of the nine seats (agy, Beam)
   decline every dispatch"), keeping the rest of the sentence.
2. README.md:80 — the `session-start` description claims it prints
   "that same opening line". It prints the standing line PLUS the
   full injected lessons block (`cli.py:317-338` prints the whole
   `pre_llm_call` context). Correct the description: it prints the
   opening context `pre_llm_call` injects — the standing line and
   the lessons — not the full board.
3. README.md:81 — the next-tier gap is described as "exact". With
   partial cost coverage `standing_report` prints "at least $X
   more" by design. Correct: the gap is exact when cost coverage is
   complete; with partial coverage the report gives a floor ("at
   least").
4. README.md:80 — the standing-line template omits two real
   suffixes from `format_standing_line`: the coverage suffix
   `(N of M dispatches reported cost)` and the degraded suffix
   `· backing unverified`. Extend the template/description to
   include them.
5. SUBMISSION.md:33 — the Step 2 beats claim the demo shows the
   measured gate killing allocating candidates
   (`alloc_bytes_per_tick vs threshold 0`). The shipped demo
   (`demo/run_demo.py`) is the Python fixture: its measured gate
   kills on `ops_per_tick` (120.0 vs a threshold of ≤ 8, per the
   README demo record). The C# fixture has never had a contest run
   (README.md:150) and is not what `run_demo.py` records. Rewrite
   the beat to what the shipped demo actually shows (the Python
   fixture's measured kill), and drop the implication that the
   recorded demo contains the C# fixture.

No tests (documents), but re-run `demo/run_demo.py` once
(`TMPDIR=~/workspace/.tmp-r10fix`, scratch data dir) and confirm
exit 0 and that the numbers your edited text cites match its actual
output. Commit README.md + SUBMISSION.md only.

## Unit N3+N4 — state/router (state.py + router.py + tests)

Two LOWs, one unit (both touch the route machinery):

- **N3 (lane 6, latent perf):** M1's `candidates_for(contract_id,
  attempt=...)` read full-scans the `candidates` table (no index on
  `contract_id`): ≈0.09 µs/row — 5.16 ms per verdict on a 200k-row
  book, growing with the book. Add an index on
  `candidates(contract_id, attempt)` through the store's normal
  schema path (`CREATE INDEX IF NOT EXISTS`, alongside the existing
  table creation/migration code, so existing books gain it on open).
  Test: the index exists after opening a fresh store AND after
  opening a pre-index book file (create the book with the index
  dropped/absent, reopen, index present); `candidates_for` results
  unchanged.
- **N4 (lane 4, F-R10-1):** on a raw-seeded legacy route with
  `primary == compete_with` (a shape `upsert_route` can no longer
  write), demoting the primary repairs the state correctly (the
  duplicated compete seat is vacated; the demotee stays primary)
  but the returned line claims a false landing: "demoted E within
  the fallback order (primary is now E)". Fix the LINE (and any
  sibling line with the same defect — audit the demotion result
  lines for this shape) so it names what actually happened: the
  duplicate compete seat was vacated and E remains primary; no
  fallback move is claimed. State behavior unchanged. Test: seed
  the corrupted shape at the store level (raw upsert path the lane
  used), demote, assert the stored route AND that the line does not
  claim a fallback landing and does name the vacated duplicate.

Run the full suite serially; commit state.py, router.py, and your
test file(s) only.
