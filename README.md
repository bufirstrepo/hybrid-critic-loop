# The Millionaire Matrix

Eight models walk in. The field steps down round by round. One answer walks out.

The Millionaire Matrix — a Hermes plugin (Python package `hybrid_critic_loop`, formerly presented as the Hybrid Critic Loop) — makes one task compete across every model family the operator has a seat for. Each seat generates one candidate in its own git worktree; hard and measured gates anchored in real tests eliminate what they eliminate; the survivors are blind-scored by their peers on a validity matrix; the field steps down by recorded score, round after round, until one candidate remains for the human gate. The deciding evidence is written back as a Lesson the agent loads at the start of its next session — with the expert of the first gate-failing candidate demoted in the route table (a loss on matrix score alone changes no route; on a degraded verdict the demotion is recorded and held for the operator), so the same failure is not bought twice.

## Disclosures (read first)

Catalog Rule 13 disclosure — this plugin, by design:

- **Executes shell commands.** It runs the model CLIs (`codex exec`, `grok -p`, and the other family adapters below), the acceptance commands written into each contract (unit tests, build commands), the sensor commands that produce measured-gate metrics, and `git` (worktree, diff, merge, branch). Contract acceptance commands are operator-authored; the plugin runs them as written, in the candidate's worktree.
- **Makes network calls through the operator's own credentials.** Model traffic goes through the operator's logged-in model CLIs, the operator's local ollama server (alibaba seat), or API adapters keyed by the operator's own environment variables (DeepSeek). The plugin holds no keys of its own and sends no telemetry anywhere.
- **Runs candidate code.** Acceptance tests execute the candidates' implementations. Run contests in a disposable repository, never in a tree you cannot afford to have code executed in.
- **Writes local state.** A SQLite database (WAL mode), a human-readable `lessons.md` and a `session.json` under the plugin's data directory — the one Hermes assigns to this plugin, or `HCL_DATA_DIR` outside Hermes (default `<HERMES_HOME>/plugin-data/hybrid-critic-loop/`) — and, inside the operator's target repository, one git worktree per candidate (`wt/<contract>/<slot>`) with `critic/<contract>/<slot>` branches (losers are kept as branches after a merge). Nothing is written inside the plugin's own directory.
- **No hosted control plane.** The queue, the gates, and the matrix all run locally. The Beam seat (Reflection) is reserved for the Nous-portal-hosted model: this build ships no Beam adapter path, the seat always reports unavailable with that note, and the entry never depends on it.

## The panel — eight families

The target panel is eight model families, expressed in the route table (`panel` + `scoring: "peer_matrix; no self-scoring; blind"`). Each seat is an adapter behind one interface; each reports available or unavailable honestly from the environment it runs in. Every seat handed to the run is dispatched, route order first — one candidate per seat (a family with two seats, alibaba's `qwen-local` and `qwen-cli`, can field two); a seat that reports unavailable (or refuses) produces no artifact: its candidate row stays `unavailable`/`refused`, never enters the gate stack, and the ledger records the outcome, so a contest runs on what exists. Nothing is faked: an absent CLI reports unavailable, and in this build two of the eight seats (agy, Beam) decline every dispatch and say so. For a CLI seat the exit code decides between completed and refused — a refusal marker in the output explains a failure and never overrides a successful exit — and a timeout is recorded as a timeout (a seat that printed a refusal marker and then hung is recorded as refused); a prompt or packet too large for a single argument is not run — the generation is recorded as an `error` carrying the reason, and an oversize judge packet yields no cells for that peer. An API seat (`qwen-local`, `deepseek`) writes the reply's fenced code block to the contract's `target_file` and declines a contract without a `target_file` before any request is made.

| Family | Seat | How it runs |
|---|---|---|
| anthropic | `claude` | Claude Code CLI |
| openai | `codex` | Codex CLI |
| xai | `grok` | Grok CLI |
| google | `agy` | agy CLI — availability is a PATH check only; a headless generate/judge mode is unverified, so the seat declines every dispatch and says so |
| reflection | `beam` | reserved seat for the Nous-portal-hosted model — no adapter path in this build; always reports unavailable |
| alibaba | `qwen-local` | local ollama server (any `qwen2.5-coder*` tag it lists; `OLLAMA_HOST` / `OLLAMA_MODEL` override); `qwen-cli` is an alternate CLI seat |
| moonshot | `kimi` | Kimi CLI |
| deepseek | `deepseek` | DeepSeek API (`DEEPSEEK_API_KEY` from the environment) |

Panel floor: a candidate scored by fewer than 2 distinct peer families has its verdict marked **degraded** (`judged.panel_status: full|degraded`), and a degraded matrix can never produce the Phase-1 lesson-loop exit on its own: the Lesson is still written from the gate evidence, but its route change is **held, not applied** — recorded on the verdict row (`route_hold`), as a ledger row for the expert concerned, and as `route_change_hold` on the run result — for the operator to act on. Two-family contests keep working — the documented exception — but their verdicts say degraded and their route changes are held. (A verdict the anchor decided alone, with no matrix run, is not degraded; its demotion applies.)

## The peer validity matrix

The judged gate is a blind peer review, not a judge panel. Every seat that can judge and reports available — contestants included, up to the contract's judge count — scores every *other* family's candidate; the queue strips self-cells **in code** before dispatch, so no family can score its own work even by construction. Seats that cannot judge or are unavailable are skipped and named in the ledger. Candidates are renamed per peer and identity terms are scrubbed from the packet.

Four rows (`validity_matrix_v1`):

- **question_fidelity** — scored against the original contract task text, which the packet carries as written apart from the blind's path scrubbing (home and worktree paths rewritten, commit-metadata-looking lines dropped), never a candidate's restatement.
- **completeness** — every acceptance item addressed.
- **evidence** — every claim or API cited points at a file:line, a test, or a measurement.
- **test_agreement** — the peer is shown the hard/measured results; it cannot pass what the tests failed.

Every cell carries a score (0–10), a confidence (0–1), an evidence pointer, and a one-line rationale; a cell missing any of them is discarded whole — no rationale, no score. A candidate that no peer produced a valid cell for is recorded as **unscored** — no median, no rows, no validity points, and it cannot pass — and the bracket round names the peers that produced no cell. Aggregation is per-row weighted medians across peers, where a cell's effective weight is **judge calibration × confidence**. The pass rule is unchanged from the design: the (calibration × confidence weighted) median of per-judge means must reach the contract minimum, and no row may fall below minimum − 2.

**Dissent.** Two numbers, named and separate (this closes the design document's open question, with defaults): `spread_threshold` (default 2.0) bounds the max−min spread of per-judge means and escalates; `dissent_threshold` (default 3.0 on the 0–10 scale) flags a *single cell* further than that from its row median. A flagged dissent is logged with its rationale and shown in the verdict, per candidate (`verdict.judged[<candidate>].dissent`). If a dissent cites a test or measurement the majority may have missed, the queue **re-runs that candidate's full hard/measured gate stack, once per attempt, before anything merges**. If the mean confidence across a candidate's cells falls below 0.4, the run escalates instead of passing.

**Calibration.** Each family's `judge_calibration` is computed from the store's own history: how often its scores agreed with decidable outcomes (verdict winners vs gate/verdict eliminations), Laplace-smoothed toward the neutral prior 0.5. Families start at 0.5; calibration weighting activates only after a family has ≥ 5 scored cells (before that the calibration factor is the 0.5 prior for every family, so only confidence weights the cells), and the count `n` is recorded alongside the value. One part of the design is a named gap, not a silent omission: the blind style-preference penalty is **not** applied, because in a peer matrix the proxy the design sketches reduces to raw leniency (self-scores are exactly the stripped cells), which agreement calibration already judges. The bias quantity is computed in `matrix.py` but not yet surfaced in any report; the reasoning is in `matrix.py` and the build log (`GAP-STYLE-PREFERENCE-PENALTY`).

## The step-down bracket

The funnel is a scored step-down bracket. The whole panel generates once — one candidate per seat (26 slots; the shipped panel has nine seats, seven of which can generate; the ladder below is drawn from 16 for illustration). Then, every round, the surviving field is re-scored by the peer matrix — the hard and measured results from the gate pass stand (a gate is re-run only when a dissent cites it) — and the field narrows by recorded validity score:

- **Default ladder:** eliminate 2 per round — 16 → 14 → 12 → 10 → 8 → 6 → 4 → 2 → a final scored round → 1. `bracket.cut_mode: "half"` selects the halving ladder instead (top half advances, rounding up: 9 → 5 → 3 → 2 → 1). Either way the ladder always lands on a final two, and a cut never jumps past two — but matrix failers are out before the cut: a round that passes only one candidate ends the bracket without a final two, and a round that passes none empties the field (`passed=0`) and the run escalates or offers a measured waiver. The cut size is configurable per contract (`bracket.cut_per_round`, default 2) because every extra round is a full matrix pass — **the step-down rate is a cost dial**, and the ledger's cost-per-validity-point is what makes that cost visible.
- **The anchor never bends:** a hard or measured failure eliminates at *any* round, regardless of matrix score. The matrix never overrules the gates.
- **Cuts are pure functions of recorded scores.** Nothing is regenerated between rounds; a candidate's artifact never changes (no revision round in this entry). An elimination is an entry on the verdict row — the `eliminated` list plus the round's `scores` record — not a deletion.
- **Tie at the cut line:** fewer tokens wins, then earlier completion (the Phase-0 tie rule).
- **Auditable:** every round records round number, field in, field out, and the cut-line score in the verdict's `judged.bracket` block and in the ledger.

## Cost per validity point

A **validity point** is one point of final validity score from the matrix (the candidate's `median_mean`, 0–10). Each expert's ledger line reports dispatches (with completed, refused, timeout, unavailable and error counts — a seat that refused and a seat whose binary was missing are different facts), tokens, cost, win rate, accumulated validity points, and **cost per validity point** (cost reported on matrix-scored candidates ÷ their validity points). The undefined-cost rule is strict: CPVP is computed only when both cost and validity are measured numbers. Where an executor does not report cost, CPVP prints **"not reported"** and the ledger line shows the expert's win rate beside it — the plugin never divides by an unreported cost and never estimates one. An expert the matrix never scored — an anchor-only winner, a gate-eliminated candidate, a seat that produced nothing — has no validity points to report, and nothing is invented for it. Cost is read only where a provider reports it: the claude seat's JSON envelope carries `total_cost_usd`; codex, grok, the ollama seat and DeepSeek report tokens only, so their lines print `cpvp=not reported`.

## What it does, end to end

1. **Contract.** A task is written as a contract with *structured* acceptance: hard gates (shell command + expected exit code), measured gates (a sensor command emitting JSON + metric/operator/threshold), and a judged gate naming a rubric — `validity_matrix_v1` for the peer matrix — (+ minimum, judge count, the two thresholds); a top-level `bracket` block sets the cut mode and size. Prose acceptance criteria and judged-only contracts are refused.
2. **Baseline rule.** The acceptance suite must execute and **fail on the unmodified baseline** before any candidate is dispatched. A contract whose tests already pass is refused as vacuous. A contract id is one run: a contract that already finished (completed, awaiting its human gate, escalated or rejected) is never dispatched again — `critic_run` answers `already_run` and names the gate when one is pending — and a refused contract re-verifies its baseline on its next run instead of skipping the rule.
3. **Panel generation.** One candidate per seat handed to the run, each in its own git worktree (`wt/<contract>/<slot>[<attempt>]`, branch `critic/<contract>/<slot>[<attempt>]`; the baseline check uses `wt/<contract>/baseline`), each carrying the provenance its adapter reported (`generated` for a model's or program's own output) and evidence (run-log hash, provider request ids where reported). A candidate whose provenance is anything else — a mock, a human-supplied artifact — can never pass a gate.
4. **Gates, in rank order.** Hard, then measured, then the matrix. When the anchor leaves exactly one survivor there is no contest to score and the anchor's decision stands; fields of two or more go to the bracket. A field that falls at the hard gate retries with the error log attached (attempt cap 3, then escalate); a measured-only failure is offered to the human gate as a waiver instead. A seat that refuses or is unavailable is simply out of that contest — every seat is already dispatched, so there is nobody left to re-dispatch to — and the ledger records it.
5. **Events.** Exactly four event types ever reach the operator: spread over threshold, budget exhaustion, retry cap, and the human gate. They are surfaced in the run result and the events log — a durable events table readable via `critic_ledger` — not via any phone or agent-room ping (no such plugin API exists).
6. **Human gate.** The one surviving answer is presented for approval; a run that ends `awaiting_human_gate` is completed later through the `critic_gate` tool, `/critic gate <contract_id> approve|reject|waive`, or `hermes critic gate`. Approval merges its worktree into the main tree and keeps the losers as branches for inspection. Rejection starts a new contract (`<id>_rej<attempt>`) with the rejection recorded as an observation on the new contract row. A human may waive a *measured* failure only — never a hard one. When the surviving answer failed the measured gate the verdict carries an *offered* waiver and a plain approval is refused: only an explicit waive merges it, and the waiver is recorded on the verdict row as granted by the human. The decision is recorded on the verdict row, so a gate cannot be decided twice.
7. **Lesson writeback.** At the verdict, a Lesson is written from the deciding gate's own evidence — its outcome and evidence pointers are the failing measurement or hard-gate rows; its cause and rule are fixed per-metric guidance strings, not derived from the candidate — and its route change is applied (the expert of the first gate-failing candidate is demoted for that task category; a loss on matrix score alone changes no route) unless the verdict's panel is degraded, in which case the change is recorded and held for the operator (see Panel floor). On the next session's first turn, lessons for the session's domain family are injected into context under the real `pre_llm_call` cap (10,000 characters): ranked by domain match (the exact domain first, then sub-domains and ancestors along the dotted path — siblings are excluded), then tag overlap, then newest; the block is filled to the cap on lesson boundaries and states how many lessons the cap dropped. Hermes passes the hook no domain, so the default `code` family applies and every `code.*` lesson is eligible.

## Install

Requires Hermes **>= 0.21.0** and `git`. The model seats are the operator's: install and log into the CLIs for live contests (contests compose from whatever seats are available; they never fake a result).

```sh
hermes plugins install <owner>/hybrid-critic-loop
hermes plugins enable the-millionaire-matrix
# restart Hermes
```

Standalone, without Hermes (the same loop, driven from a shell):

```sh
pip install .
hcl run contract.json --repo /path/to/repo --approve --data-dir ./hcl-data
```

## Usage

In Hermes:

- Slash command: `/critic run <contract.json> [repo_path]`, `/critic gate <contract_id> approve|reject|waive [reason]`, `/critic lessons [domain]`, `/critic ledger`
- CLI: `hermes critic run|gate|lessons|ledger|session-start`
- Tools (for the agent itself): `critic_run`, `critic_gate` (complete a pending human gate: approve, reject or waive), `critic_matrix` (the validity matrix for a contract: every peer's score per criterion, confidence, dissent flags), `critic_lessons`, `critic_ledger` (dispatches, tokens, cost, win rate, cost per validity point, and the events log)

`hermes critic run` prints the run result and the verdict report; `critic_matrix` returns the matrix and the bracket block for a contract; the demo script prints all of it, round by round. Lessons are retrievable with `critic_lessons` and are injected automatically on a session's first turn for the session's domain family (Hermes passes no domain to the hook, so the default `code` family applies).

## The demo

`demo/run_demo.py` runs the whole loop end-to-end in this repository against a small fixture (`fixture/python-sampler/`): rolling-average samplers of the kind a physics loop reads every tick. The measured gate is the interesting one — the natural implementation recomputes the average over the whole buffer every tick; the sensor counts the arithmetic and the contract caps it.

The demo panel is three deterministic local solver families (`local-naive`, `local-ring`, `local-balanced`) plus three deterministic local demo judges — packet-text scorers, labelled as such everywhere, **not models**. The script first attempts the live contest with the real `codex`/`grok` CLIs and prints whatever genuinely happens — refusals and unavailability are recorded in the ledger like any other outcome.

- **Run 1 (contract `ctr_demo_1`, SpeedSampler):** baseline tests fail (the fixture ships as stubs) → the panel generates → the naive candidate *and* the balanced candidate (whose deque average still rescans the window) die on the measured gate (ops_per_tick 120.0 vs threshold <= 8; the ring candidate scores 1.0) → a single survivor stands, so the anchor alone decides: verdict, human approval, winner merged → a Lesson is written from the measurement row and the naive expert is demoted in the route table.
- **Fresh session:** the session-start path injects the Lesson into context before any new work happens.
- **Run 2 (contract `ctr_demo_2`, SteeringSampler):** the route table already shows the demotion; the lesson-conditioned generators now all produce the O(1) implementation and pass the measured gate → the peer matrix fills in over the full field → the bracket steps it down 3 → 2 → 1 with the cut line printed per round → one answer walks out.

A Godot C# version of the fixture (`fixture/godot-csharp/`, contracts `demo1-csharp.json` / `demo2-csharp.json`) carries the same design with the allocation sensor from the original design (alloc_bytes_per_tick, threshold 0). It requires the .NET SDK and was **not run** in the build environment (no `dotnet` there); on the operator's machine it compiles and its tests fail on the stub baseline as the contract requires (checked 2026-11-07) — no contest has been run against it yet. One honest limitation of the Python mirror: its sensor counts arithmetic on the values the sampler stores, so an implementation that converts inputs to plain floats at push time would under-report — the C# allocation sensor has no equivalent blind spot. The sensor's docstring states this.

A real Lesson, as the demo's run 1 writes it (row `lsn_0001` of the local-panel store, 2026-11-07; the row's `created_at` timestamp omitted):

```json
{
  "id": "lsn_0001",
  "contract_id": "ctr_demo_1",
  "domain": "code.game",
  "tags": ["sampler", "physics", "hot-loop"],
  "outcome": "ctr_demo_1_a failed the measured gate: ops_per_tick 120.0, threshold <= 8",
  "cause": "per-tick recomputation in the hot loop (work per push grows with buffer size)",
  "rule": "keep per-tick work O(1): pre-allocate the buffer and maintain a running total instead of recomputing",
  "route_change": {"category": "code", "demote": "solver-naive", "reason": "lost on measured gate"},
  "evidence": ["verdict:ctr_demo_1", "measurement:ctr_demo_1_a:ops_per_tick"],
  "written_to": "plugin_store",
  "provenance": "generated"
}
```

One deviation from the original design document, stated plainly: the design called for lessons to be written to Hermes memory (`written_to: hermes_memory`). Hermes exposes no plugin API for writing its memory — the built-in memory file is written only by the agent's own memory tool. The buildable equivalent, and what this plugin does, is `written_to: plugin_store`: lessons live in the plugin's own SQLite store under the Hermes plugin-data directory, and are consumed through the `pre_llm_call` context-injection hook (10,000-character cap). The loop the design wanted — lesson written at verdict, present in the next session's context — is real; only the storage label differs.

## Architecture

| Layer | Modules | Responsibility |
|---|---|---|
| Contest | `executors.py`, `executors_api.py`, `worktrees.py`, `shellrun.py` | Adapters for all eight panel families (CLI + ollama + API); one git worktree per candidate; every subprocess run without a shell and captured, the generation log hashed as evidence (`evidence.log_hash`) |
| Gates | `contracts.py`, `gates.py`, `judges.py`, `rubrics.py` | Contract validation, the baseline rule, gate evaluation in rank order, the legacy blinded judge protocol (non-matrix rubrics) |
| Matrix | `matrix.py`, `bracket.py` | The peer validity matrix (self-cells stripped in code, calibration × confidence weighting, dissent + gate re-run requests, calibration from history) and the step-down bracket (pure cuts over recorded scores) |
| State | `state.py` | SQLite store with the twelve domain objects: targets, contracts, candidates, measurements, scores, verdicts, final_answers, experts, routes, ledger, lessons, events |
| Memory | `lessons.py`, `router.py` | Evidence-derived Lesson writing and the capped lesson context block; route-table updates from verdicts (panel + scoring carried on the route). Lesson ranking lives in `state.py` (`lessons_for`), injection in `hooks.py` (`pre_llm_call`) |
| Surface | `plugin.py`, `hooks.py`, `cli.py`, `loop.py`, `reports.py` | Hermes registration (5 tools, `/critic`, `hermes critic`, 2 hooks: `on_session_start`, `pre_llm_call`), the queue/orchestration loop, DB-only reporting |

Reports print only database rows; anything absent prints as `not run` or `not reported` — token and cost totals are never estimated.

## Scope and roadmap

Shipped in this entry (v0.3): the panel contest (adapters for eight family seats — in this build the agy and Beam seats decline every dispatch and say so), the peer validity matrix with dissent and calibration, the scored step-down bracket, cost per validity point, the durable events log, Lesson writeback with route-table demotion/promotion, and session-start lesson injection — the design's Phase 0 + Phase 1. Deliberately later (the design's Phase 2, not built): decomposition and stability matrix rows, a revision round, an attacker seat, the spec wave, and registry scoring beyond win rate + calibration.
