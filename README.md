# The Millionaire Matrix

Eight models walk in. The field steps down round by round. One answer walks out.

The Millionaire Matrix — a Hermes plugin (Python package `hybrid_critic_loop`, v0.4.0, formerly presented as the Hybrid Critic Loop) — makes one task compete across every model family the operator has a seat for. Inside Hermes, eight vendor-family seats (`mm-openai` through `mm-zai`) execute in parallel over `ctx.llm` with runtime model-family verification; standalone outside Hermes, local CLI, Ollama, and API adapters field what is installed. Each seat generates one candidate in its own git worktree; hard and measured gates anchored in real tests eliminate what they eliminate; the survivors are blind-scored by their peers on a validity matrix; the field steps down by recorded score, round after round, until one candidate remains for the human gate. Live progress, deduplicated per-pool plan usage, and bracket state stream to `<data_dir>/run_status.json`, `/mm`, `hermes critic`, and the Hermes Desktop status-bar chip and side pane. The deciding evidence is written back as a Lesson the agent loads on the first turn of its next session — with the expert of the first gate-failing candidate demoted in the route table (a loss on matrix score alone changes no route; on a degraded verdict the demotion is recorded and held for the operator), so the same failure is not bought twice.

## Disclosures (read first)

Catalog Rule 13 disclosure — this plugin, by design:

- **Executes shell commands.** It runs the model CLIs (`codex exec`, `grok -p`, and the other family adapters below), read-only CLI quota probes (`codex app-server`, `grok agent stdio`, `agy -p "/usage"`, `claude auth status`), the acceptance commands written into each contract (unit tests, build commands), the sensor commands that produce measured-gate metrics, and `git` (worktree, diff, merge, branch). Contract acceptance commands are operator-authored; the plugin runs them as written, in the candidate's worktree.
- **Makes network calls through the operator's own credentials.** Inside Hermes, model traffic routes through Hermes's configured `ctx.llm` auxiliary task slots (`mm_openai` through `mm_zai`); outside Hermes (or in `--executors cli` mode), traffic goes through the operator's logged-in model CLIs, the operator's local ollama server (alibaba seat), or API adapters keyed by the operator's own environment variables (DeepSeek), plus optional read-only plan-usage checks against those same operator-configured accounts. The plugin holds no keys of its own and sends no telemetry anywhere.
- **Runs candidate code.** Acceptance tests execute the candidates' implementations. Run contests in a disposable repository, never in a tree you cannot afford to have code executed in.
- **Writes local state.** A SQLite database (WAL mode), `cert_key`, a human-readable `lessons.md`, `session.json`, live progress snapshots (`run_status.json`), and cached quota readings (`usage_feed_cache.json`, plus optional passive Claude statusline/stream harvest files) under the plugin's data directory — the one Hermes assigns to this plugin, or `HCL_DATA_DIR` outside Hermes (default `<HERMES_HOME>/plugin-data/hybrid-critic-loop/`) — and, inside the operator's target repository, one git worktree per candidate (`wt/<contract>/<slot>`) with `critic/<contract>/<slot>` branches (losers are kept as branches after a merge). Nothing is written inside the plugin's own directory.
- **No hosted control plane.** The queue, the gates, the usage feed, and the matrix all run locally. In standalone CLI mode, the legacy Beam seat (Reflection) is reserved for a Nous-portal-hosted standalone path: this build ships no standalone Beam adapter, that CLI seat always reports unavailable with that note, and inside Hermes the 8-family panel runs directly over `ctx.llm` (`mm-openai` through `mm-zai`).

## The panel — eight families (Hermes `ctx.llm` + standalone adapters)

The target panel in `router.py` (`PANEL_FAMILIES`) is eight distinct model families: `anthropic`, `openai`, `xai`, `google`, `alibaba`, `moonshot`, `deepseek`, and `zai` (`panel` + `scoring: "peer_matrix; no self-scoring; blind"`). Each seat is an adapter behind one interface; each reports available or unavailable honestly from the environment it runs in.

### 1. In-Hermes 8-family `ctx.llm` panel (default when running inside Hermes)

When `plugin.register(ctx)` runs inside Hermes, `executors_hermes.py` registers eight auxiliary task slots (`mm_openai`, `mm_anthropic`, `mm_xai`, `mm_google`, `mm_alibaba`, `mm_moonshot`, `mm_deepseek`, `mm_zai`) and dispatches generation and blind peer judging in 8-worker parallel waves via `ctx.llm.complete(task=...)`:

| Family | Hermes Seat | Auxiliary Task Slot | Default Model |
|---|---|---|---|
| openai | `mm-openai` | `mm_openai` | `openai/gpt-5.4-mini` |
| anthropic | `mm-anthropic` | `mm_anthropic` | `anthropic/claude-sonnet-5` |
| xai | `mm-xai` | `mm_xai` | `x-ai/grok-4.6` |
| google | `mm-google` | `mm_google` | `google/gemini-3.8-flash` |
| alibaba | `mm-alibaba` | `mm_alibaba` | `qwen/qwen3.8-flash` |
| moonshot | `mm-moonshot` | `mm_moonshot` | `moonshotai/kimi-k3` |
| deepseek | `mm-deepseek` | `mm_deepseek` | `deepseek/deepseek-v4.1-flash` |
| zai | `mm-zai` | `mm_zai` | `z-ai/glm-5.3-flash` |

**Runtime family verification (zero self-scoring under fallback).** On every `generate()` and `judge()` response, `infer_model_family(result.model)` inspects the actual model string returned by the provider at runtime. If a single-model endpoint or provider fallback resolves a slot onto a different lab's model, the seat refuses the response (`unavailable` / `None` in `judge()`) rather than impersonating another family. Prompt files embedded for non-CLI seats pass through secret redaction (`file_edits.redact_for_llm`), and multi-file replies (`=== FILE: <path> ===`) are path-confined inside the candidate worktree (`file_edits.apply_reply_edits`), blocking `..`, symlinks, and `.git` writes.

### 2. Standalone CLI / Ollama / API adapters (`--executors cli` or outside Hermes)

Outside Hermes (or when `executors="cli"` is requested), `default_executors()` fields nine standalone adapters across the CLI/API surface. Every seat handed to the run is dispatched, route order first — one candidate per seat (a family with two seats, alibaba's `qwen-local` and `qwen-cli`, can field two); a seat that reports unavailable (or refuses) produces no artifact: its candidate row stays `unavailable`/`refused`, never enters the gate stack, and the ledger records the outcome, so a contest runs on what exists. Nothing is faked: an absent CLI reports unavailable, and in standalone CLI mode two of the nine seats (`agy`, `beam`) decline every dispatch and say so (standalone mode has no `zai` CLI adapter; `beam` retains the legacy `reflection` placeholder family). For a CLI seat the exit code decides between completed and refused — a refusal marker in the output explains a failure and never overrides a successful exit — and a timeout is recorded as a timeout (a seat that printed a refusal marker and then hung is recorded as refused); a prompt or packet too large for a single argument is not run — the generation is recorded as an `error` carrying the reason, and an oversize judge packet yields no cells for that peer. An API or `ctx.llm` file-editing seat (`qwen-local`, `deepseek`, `mm-*`) writes the reply's fenced code block(s) to the contract's `target_file` or `target_files` and declines a contract without a `target_file` (or `target_files`) before any request is made.

| Family | Standalone Seat | How it runs |
|---|---|---|
| anthropic | `claude` | Claude Code CLI (opportunistically harvests `rate_limit_event` stream lines for quota telemetry) |
| openai | `codex` | Codex CLI |
| xai | `grok` | Grok CLI |
| google | `agy` | agy CLI — availability is a PATH check only; a headless generate/judge adapter is not wired in this build, so the seat declines every dispatch and says so (read-only `agy -p "/usage"` is used only by `usage_feed.py`) |
| reflection (legacy slot) | `beam` | reserved standalone seat — no standalone adapter path in this build; always reports unavailable |
| alibaba | `qwen-local` | local ollama server (any `qwen2.5-coder*` tag it lists; `OLLAMA_HOST` / `OLLAMA_MODEL` override); `qwen-cli` is an alternate CLI seat |
| moonshot | `kimi` | Kimi CLI |
| deepseek | `deepseek` | DeepSeek API (`DEEPSEEK_API_KEY` from the environment) |

Panel floor: a candidate scored by fewer than 2 distinct peer families has its verdict marked **degraded** (`judged.panel_status: full|degraded`), and a degraded matrix can never produce the Phase-1 lesson-loop exit on its own: the Lesson is still written from the gate evidence, but its route change is **held, not applied** — recorded on the verdict row (`route_hold`), as a ledger row for the expert concerned, and as `route_change_hold` on the run result — for the operator to act on. Two-family contests keep working — the documented exception — but their verdicts say degraded and their route changes are held. (A verdict the anchor decided alone, with no matrix run, is not degraded; its demotion applies.)

## Two contest modes (`fix` and `improve`) + natural chat invocation

1. **`mode: "fix"` (default on full contract files — strict TDD baseline failure).** The acceptance suite must execute and **fail on the unmodified baseline** (`fail`) before any candidate is dispatched; if the baseline already passes, the contract is refused as vacuous.
2. **`mode: "improve"` (default on shorthand `critic_run` — improve already-passing code).** `verify_baseline` requires the existing test and measured-gate suite to **pass** on the unmodified baseline (`pass_improve`; a failing baseline is refused so broken code is not refactored blind), and `loop.py` hard-eliminates any candidate that produces an empty diff against baseline, forcing every surviving seat to make a substantive change that keeps all gates green.
3. **Shorthand `critic_run` + `millionaire_matrix` system-prompt guidance.** `plugin.register(ctx)` registers a `millionaire_matrix` system-prompt section so natural prompts (*"look at / improve my code using Millionaire Matrix"*) invoke `critic_run` directly with shorthand parameters (`repo_path`, `task`, `mode="improve"|"fix"`, single-executable `test_cmd` run with `shell=False` or auto-inferred from standard project markers, `target_file` / `target_files`, optional `min_usage_fraction`, defaulting to `judges=8` and `spread_threshold=3.5` on `validity_matrix_v1`) without hand-authoring a JSON file first.

## Live visibility, per-pool usage feed, and Desktop UI

### 1. Live progress & Desktop surfaces (`progress.py`, `dashboard/plugin_api.py`, `desktop/plugin.js`)

The Millionaire Matrix is an in-process Hermes plugin, not a background daemon, so live visibility flows through atomic snapshots and Hermes's plugin event bus:

- **`RunProgressTracker` (`<data_dir>/run_status.json`).** Atomically writes live run state (`phase`, per-seat `status`, `activity`, `being_prompted`, `pool_id`, `usage`, `active_seat_ids`, monotonic `agent_cursor`, `matrix_progress`, `bracket` rounds/ladder, `winner`, `certificate_stamp`) and broadcasts `plugin.the-millionaire-matrix.run.progress`. `read_run_status` checks the recorded PID and cleans up stale `active` / `being_prompted` flags if a prior process exited unexpectedly.
- **CLI & slash status.** `/mm status` (`hermes critic status`) reports `IDLE`, `RUNNING` (live wave/round/seat counts), or `AWAITING_HUMAN_GATE`; `/mm panel` (`hermes critic panel`) checks live 8-family readiness (`8/8 families ready -> 1 winner`); `critic_ledger` returns `active_run` and passive `usage` alongside ledger rows.
- **Read-only FastAPI backend (`dashboard/plugin_api.py`) & Desktop UI (`desktop/plugin.js`).** Exposes `GET /status` (passive/cached by default so UI polling spawns zero subprocesses), `GET /usage`, `GET /matrix`, and `GET /standing` — strictly read-only on fresh directories (never conjuring `data.db`). `desktop/plugin.js` renders a status-bar chip (`MM · IDLE` / `MM · RUNNING` / `MM · GATE READY`), command-palette toggle, and right-side pane (`UsagePoolsCard`, 8-seat grid with active-prompting badges, bracket ladder, matrix scores, standing, and lessons).

### 2. Per-pool plan-usage feed & storefront backend (`usage_feed.py` — data layer only)

`hybrid_critic_loop/usage_feed.py` (100% stdlib) normalizes remaining plan quota across operator subscriptions, API balances, and local compute so both the CLI (`/mm usage`, `hermes critic usage [--json] [--passive]`) and UI (`GET /usage`, `UsagePoolsCard`) show real headroom:

- **Pools, not seats.** Seats sharing one credential or portal deduplicate onto a single `pool_id` (`codex:acct-<8hex>`, `grok:acct-<8hex>`, `agy:os-keyring`, `claude:acct-<8hex>`, `kimi:acct-<8hex>`, `api:<vendor>:acct-<8hex>`, `local:ollama:127.0.0.1:11434`, `hermes:<provider>`, `local:solver`). Account identifiers are SHA-256 pseudonymized (`acct-<8hex>`); raw emails, tokens, and UUIDs never enter logs or JSON.
- **Strict polarity normalization.** Codex `usedPercent`, Grok `creditUsagePercent`, Claude `statusLine` `used_percentage` (`0–100`) / stream `rate_limit_event` `utilization` (`0–1`), and Kimi `used_ratio` (`0–1`) report **USED** (`fraction_left = 1 - used`). Only Codex enterprise `individualLimit.remainingPercent` (`0–100`) and Antigravity `remaining_fraction` (`0–1`, including proto3 omitted-zero with future `reset_time`) report **LEFT** (never inverted). Every window retains `used_fraction_reported: {value, unit, convention}`.
- **Fail-closed staleness, poll floor, and credential safety.** A window is fresh iff `now < min(resets_at, fetched_at + ttl_s)`. Expired or missing readings collapse to `status = "unknown"`, `fraction_left = None` (never fabricated `0.0` or `1.0`). `UsageCache` enforces a `300s` active-poll floor per pool, preserves original `fetched_at` across transient transport errors, invalidates immediately on `401`/auth errors, never refreshes another CLI's OAuth token, never mutates credential files, and surfaces Codex `reset_credits` strictly read-only (`consume_policy = "human_only"`). Optional contract `min_usage_fraction` (`0.0..1.0`) skips seats whose verified pool is exhausted/below threshold before worktree creation while never blocking `unknown` or `unmetered_local` pools.
- **Storefront seat-activity data layer (presentation NOT built).** `run_status.json` and `GET /usage` carry `active_seat_ids`, per-seat `being_prompted`, and a monotonic `agent_cursor` (`{seat_id, action, round, seq, updated_at}`) across generation and blind peer matrix judging (`matrix.py` `on_judge_start` / `finally: on_judge_done`). **Only this backend data layer and `UsagePoolsCard` are built in this release — the animated storefront front end (an animated agent walking seat-to-seat) is NOT built.**

**Honest usage-feed limits & verification register (at `c911fe5`):**
- **Claude pool (`claude:acct-<8hex>`):** Reports `status = "unknown"` (`plan=max`) until the operator opts in by wiring `hermes critic claude-statusline-tee` into `~/.claude/settings.json` (`statusLine.command`) or until a `ClaudeExecutor` stream turn opportunistically harvests a `rate_limit_event`.
- **Hermes portal pool (`hermes:nous`):** All eight `mm-*` seats deduplicate onto `hermes:nous`, which reports `status = "unknown"` because Hermes exposes no public plugin `ctx` quota seam yet (private `agent.*` imports are forbidden under Catalog Rule 9).
- **Verified live on pinned host CLIs (`2026-11-10`, zero spend, credential SHA-256 unchanged):** `codex 0.166.0` (`codex app-server` `account/rateLimits/read`), `grok 1.0.77` (`grok agent stdio` `_x.ai/billing` with `params: {}` after expired `unified.jsonl` rollover), `agy 1.5.5` (`agy -p "/usage" --output-format json` in graphical keyring session, `num_turns=0`), `claude 2.1.310` (`claude auth status`), and local `ollama` (`/api/ps`).
- **Explicit `NOT RUN` register:** `P3` Windows Credential Manager live probe (Linux host only; Windows `%USERPROFILE%`/`%APPDATA%` path resolution unit-tested via synthetic fixtures); `P1` Step 2 (`grok -p`) and `P7` Step 1/3 (`claude -p` Haiku) paid turns (not run per zero-spend verification policy; covered by synthetic parser tests); `P4` live DeepSeek/Moonshot/OpenRouter balance HTTP calls and `P5` live Kimi `/usages` HTTP call (API keys and `kimi` binary absent on host; covered by synthetic positive/negative/variant fixtures in `tests/test_usage_feed.py`).

## The peer validity matrix

The judged gate is a blind peer review, not a judge panel. Every seat that can judge and reports available — contestants included, up to the contract's judge count — scores every *other* family's candidate; the queue strips self-cells **in code** before dispatch, so no family can score its own work even by construction. Seats that cannot judge or are unavailable are skipped and named in the ledger. Candidates are renamed per peer and identity terms are scrubbed from the packet.

Four rows (`validity_matrix_v1`):

- **question_fidelity** — scored against the original contract task text, which the packet carries as written apart from the blind's path scrubbing (home and worktree paths rewritten, commit-metadata-looking lines dropped), never a candidate's restatement.
- **completeness** — every acceptance item addressed.
- **evidence** — every claim or API cited points at a file:line, a test, or a measurement.
- **test_agreement** — the peer is shown the hard/measured results; it cannot pass what the tests failed.

Every cell carries a score (0–10), a confidence (0–1), an evidence pointer, and a one-line rationale; a cell missing any of them is discarded whole — no rationale, no score. A candidate that no peer produced a valid cell for is recorded as **unscored** — no median, no rows, no validity points, and it cannot pass — and the bracket round names the peers that produced no cell. Aggregation is per-row weighted medians across peers, where a cell's effective weight is **judge calibration × confidence**. The pass rule is unchanged from the design: the (calibration × confidence weighted) median of per-judge means must reach the contract minimum, and no row may fall below minimum − 2.

**Dissent.** Two numbers, named and separate (this closes the design document's open question, with defaults): `spread_threshold` (default `2.0` on full contracts; `3.5` on shorthand `critic_run` contracts) bounds per-judge mean spread and escalates (when 5 or more peer families score a candidate, `matrix.py` computes the 1-trimmed inner spread — dropping the single highest and single lowest judge mean — so one harsh or generous outlier model lands in `dissent` rather than deadlocking the panel, while genuine multi-judge splits still escalate); `dissent_threshold` (default 3.0 on the 0–10 scale) flags a *single cell* further than that from its row median. A flagged dissent is logged with its rationale and shown in the verdict, per candidate (`verdict.judged[<candidate>].dissent`). If a dissent cites a test or measurement the majority may have missed, the queue **re-runs that candidate's full hard/measured gate stack, once per attempt, before anything merges**. If the mean confidence across a candidate's cells falls below 0.4, the run escalates instead of passing.

**Calibration.** Each family's `judge_calibration` is computed from the store's own history: how often its scores agreed with decidable outcomes (verdict winners vs gate/verdict eliminations), Laplace-smoothed toward the neutral prior 0.5. Families start at 0.5; calibration weighting activates only after a family has ≥ 5 scored cells (before that the calibration factor is the 0.5 prior for every family, so only confidence weights the cells), and the count `n` is recorded alongside the value. One part of the design is a named gap, not a silent omission: the blind style-preference penalty is **not** applied, because in a peer matrix the proxy the design sketches reduces to raw leniency (self-scores are exactly the stripped cells), which agreement calibration already judges. The bias quantity is computed in `matrix.py` but not yet surfaced in any report; the reasoning is in `matrix.py` and the build log (`GAP-STYLE-PREFERENCE-PENALTY`).

## The step-down bracket

The funnel is a scored step-down bracket. The whole panel generates once — one candidate per seat (26 slots; the Hermes panel fields 8 `mm-*` seats and the standalone CLI/API panel has nine seats, seven of which can generate; the ladder below is drawn from 16 for illustration). Then, every round, the surviving field is re-scored by the peer matrix — the hard and measured results from the gate pass stand (a gate is re-run only when a dissent cites it) — and the field narrows by recorded validity score:

- **Default ladder:** eliminate 2 per round — 16 → 14 → 12 → 10 → 8 → 6 → 4 → 2 → a final scored round → 1. `bracket.cut_mode: "half"` selects the halving ladder instead (top half advances, rounding up: 9 → 5 → 3 → 2 → 1). Either way the ladder always lands on a final two, and a cut never jumps past two — but matrix failers are out before the cut: a round that passes only one candidate ends the bracket without a final two, and a round that passes none empties the field (`passed=0`) and the run escalates. The cut size is configurable per contract (`bracket.cut_per_round`, default 2) because every extra round is a full matrix pass — **the step-down rate is a cost dial**, and the ledger's cost-per-validity-point is what makes that cost visible.
- **The anchor never bends:** a hard or measured failure eliminates at *any* round, regardless of matrix score. The matrix never overrules the gates.
- **Cuts are pure functions of recorded scores.** Nothing is regenerated between rounds; a candidate's artifact never changes (no revision round in this entry). An elimination is an entry on the verdict row — the `eliminated` list plus the round's `scores` record — not a deletion.
- **Tie at the cut line:** fewer tokens wins, then shorter generation duration (the Phase-0 tie rule).
- **Auditable:** every round records round number, field in, field out, and the cut-line score in the verdict's `judged.bracket` block and in the ledger.

## Cost per validity point

A **validity point** is one point of final validity score from the matrix (the candidate's `median_mean`, 0–10). Each expert's ledger line reports dispatches (with completed, refused, timeout, unavailable and error counts — a seat that refused and a seat whose binary was missing are different facts), tokens, cost, win rate, accumulated validity points, and **cost per validity point** (cost reported on matrix-scored candidates ÷ their validity points). The undefined-cost rule is strict: CPVP is computed only when both cost and validity are measured numbers. Where an executor does not report cost, CPVP prints **"not reported"** and the ledger line shows the expert's win rate beside it — the plugin never divides by an unreported cost and never estimates one. An expert the matrix never scored — an anchor-only winner, a gate-eliminated candidate, a seat that produced nothing — has no validity points to report, and nothing is invented for it. Cost is read only where a provider reports it: the claude seat's JSON envelope carries `total_cost_usd` (and Hermes `ctx.llm` seats record cost when the provider's usage block supplies one); codex, grok, the ollama seat and DeepSeek report tokens only (as did Nous Portal in the recorded `ctx.llm` proof run), so their lines print `cpvp=not reported`.

## Standing — the client book

You want to prove you're rich? Show us you're running the Millionaire Matrix. A store's **standing** is its client book: *lifetime* measured spend — every measured dollar the ledger has ever recorded, summed across all runs, the way a boutique tracks a client by lifetime spend rather than one visit. Tokens are counted alongside, and never convert to dollars; where no provider reported cost, the spend reads **"not reported"** and the tier is taken from what was actually measured — nothing is estimated, here as everywhere in this plugin.

| Tier | Lifetime measured spend |
|---|---|
| WINDOW SHOPPER | no measured spend |
| CLIENT | $0.01–$999.99 |
| HIGH ROLLER | $1,000–$9,999.99 |
| PLAYER | $10,000–$99,999.99 |
| BALLER | $100,000–$999,999.99 |
| MILLIONAIRE | $1,000,000+ |

Standing appears wherever the operator already looks:

- **Session start.** The injected first-turn context opens with the standing line — `Millionaire Matrix standing: <TIER> — $<spend> lifetime measured spend (N of M dispatches reported cost) · <tokens> tokens · backing unverified` (segments read "not reported" when unmeasured; the cost-coverage suffix appears only when some counted dispatches reported no cost, and the "backing unverified" suffix only when the standing's referential backing could not be evaluated) — above the lessons, under the same 10,000-character cap. A store with dispatches but no lessons injects the line alone. (`session-start` on the CLI prints that same opening context — the standing line and the lessons `pre_llm_call` injects — not the full board.)
- **`/critic standing` or `/mm standing`** (also `hermes critic standing`, `hcl standing`) prints the full board: the standing line, the next tier and the additional measured spend required to reach it — exact when cost coverage is complete; with partial coverage the report states a floor ("at least $X more") instead (nothing above MILLIONAIRE), and one row per expert — the ledger report's columns plus that expert's own tier, taken from the expert's measured validity cost where present.
- **Every verdict report** carries the store's current tier and the run's own recorded contribution (the tokens and cost on that contract's ledger rows, or "not reported"), and closes with the run's certificate.

## Certificates and the chain

A standing is only worth flexing if it cannot be rewritten after the fact, so the book seals itself. Every ledger row carries a hash chain: `row_hash = sha256(prev_hash + the row's canonical form)`, each row sealing the one before it back to a genesis hash. Rows written before the chain existed are backfilled in id order on first open (a store meta flag records that the backfill has run); every new row is hashed as it is appended, under the store's lock. Edit any stored row — inflate a token count, invent spend — and the chain no longer recomputes: `chain.verify_chain` names the first row that fails to seal. The chain detects any edit *that does not also recompute the chain* — whoever holds the store's files can recompute it — so verification gains its meaning against a chain head or certificate exported earlier, not against the store alone. And pre-chain builds must not append to a chained store: their unhashed rows break the seal for every reader (an unsealed suffix is re-sealed on open and reported as a repair; an unsealed row in the middle is corruption).

A run that reached a verdict with a winner is issued a **certificate**, printed in its run result and at the foot of its verdict report. The demo's own stamp — the local solvers report neither tokens nor cost — is the honest shape of it:

```
MM CERTIFIED · TOKENS NOT REPORTED · ONE ANSWER
cert: mm1.<base64url(payload)>.<base64url(signature)>
```

The stamp follows the book's discipline: the totals are the run's own ledger rows summed over reported values only, and where no provider reported the run's cost the dollar segment is omitted — never estimated. The payload names the contract, the verdict, those totals, the operator's tier at issue time, and the chain head (the `row_hash` of the contract's last ledger row). The signature is HMAC-SHA256 over the payload with the store's local key (`cert_key` in the plugin data directory, generated once, file perms 600). `hermes critic verify-cert <token>` (or `verify-cert` on the standalone CLI) recomputes the signature, the whole chain, the run totals and the chain head against the store and reports VALID or the first mismatch it finds.

**Upgrading to 0.4.0.** Certificates issued by pre-0.4 builds stop verifying under 0.4.0 — `verify-cert` answers that the certificate predates the current format, and the certificate is re-issued from the store (the run's verdict report carries the fresh token). A re-issued certificate may show different totals than the original it replaces: the totals definition was tightened, and bookkeeping rows no longer count toward a run's totals. The book itself is untouched by the upgrade; only the certificate format changed.

**Backing up the book.** The book is four files in the data directory — `data.db`, `data.db-wal`, `data.db-shm` and `cert_key` — and they travel together. The database runs in WAL mode, so the newest committed rows may live only in `data.db-wal`: copying `data.db` alone silently restores an older book. Copy the whole data directory, or take the database with sqlite's `.backup`. And keep `cert_key` beside the database: a book restored without its `cert_key` still reads and reports standing, but its certificates no longer verify — the key id does not match.

**What the local signature does and does not prove.** It proves the certificate was issued by this store and has not been altered since — a store-integrity seal. It does *not* make a public claim checkable: whoever holds the store's files holds the key, so a certificate posted on its own is only as trustworthy as the store behind it. Making a posted standing publicly verifiable is the designed next step and is **not built**: a hosted verification service the certificate ID resolves against, live receipts streamed to it as runs finish (so a rich history cannot be backfilled), and provider-record audit for the top tiers. Until that service exists, no certificate from this build proves anything to anyone who does not already trust the store.

## What it does, end to end

1. **Contract.** A task is written as a contract (or synthesized via shorthand `critic_run` arguments) with *structured* acceptance: hard gates (shell command + expected exit code), measured gates (a sensor command emitting JSON + metric/operator/threshold), and a judged gate naming a rubric — `validity_matrix_v1` for the peer matrix — (+ minimum, judge count, the two thresholds); a top-level `bracket` block sets the cut mode and size, `mode` selects `fix` or `improve`, and optional `min_usage_fraction` sets a verified per-pool quota pre-flight floor. Prose acceptance criteria and judged-only contracts are refused.
2. **Baseline rule.** In `fix` mode, the acceptance suite must execute and **fail on the unmodified baseline** before any candidate is dispatched (a contract whose tests already pass is refused as vacuous). In `improve` mode, the acceptance suite must **pass** on the unmodified baseline (`pass_improve`), and zero-diff candidates are hard-eliminated after generation. A contract id is one run: a contract that already finished (completed, awaiting its human gate, escalated or rejected) is never dispatched again — `critic_run` answers `already_run` and names the gate when one is pending — and a refused contract re-verifies its baseline on its next run instead of skipping the rule. The baseline run is also the moment a contract's commands first execute: a contract's gate commands run on the operator's machine, starting with the baseline run, before any candidate exists — author contracts only from parties trusted to run code on this machine. Gate commands run *as the operator*, with the operator's authority: the gate runner strips known secret-bearing names from the command's own environment, but a command can still read the parent process's environment (through `/proc`, for example), so that scrub is hygiene, not a boundary — the control is this disclosure, and it is the same one: only run contracts whose authors you trust to run code as you.
3. **Panel generation.** One candidate per seat handed to the run (after optional pool-deduped `min_usage_fraction` pre-flight), each in its own git worktree (`wt/<contract>/<slot>[<attempt>]`, branch `critic/<contract>/<slot>[<attempt>]`; the baseline check uses `wt/<contract>/baseline`), each carrying the provenance its adapter reported (`generated` for a model's or program's own output) and evidence (run-log hash, provider request/model ids where reported). A candidate whose provenance is anything else — a mock, a human-supplied artifact — can never pass a gate.
4. **Gates, in rank order.** Hard, then measured, then the matrix. When the anchor leaves exactly one survivor there is no contest to score and the anchor's decision stands; fields of two or more go to the bracket. A field that falls at the hard gate retries with the error log attached (attempt cap 3, then escalate); a measured-only failure retries the same way and escalates at the cap — owner ruling 2026-11-09: a measured regression is not a valid contract, and no waiver is offered for one. A seat that refuses or is unavailable is simply out of that contest — every seat is already dispatched, so there is nobody left to re-dispatch to — and the ledger records it.
5. **Events.** Exactly four event types ever reach the operator: spread over threshold, budget exhaustion, retry cap, and the human gate. They are surfaced in the run result and the events log — a durable events table readable via `critic_ledger` — alongside live UI progress over `hermes_cli.plugin_events` (`run.progress`).
6. **Human gate.** The one surviving answer is presented for approval; a run that ends `awaiting_human_gate` is completed later through the `critic_gate` tool, `/critic gate <contract_id> approve|reject|waive` (or `/mm gate`), or `hermes critic gate`. Approval merges its worktree into the main tree and keeps the losers as branches for inspection. Rejection starts a new contract (`<id>_rej<attempt>`) with the rejection recorded as an observation on the new contract row. A human may waive a *measured* failure only — never a hard one. When a verdict carries an *offered* waiver and the surviving answer failed the measured gate, a plain approval is refused: only an explicit waive merges it, and the waiver is recorded on the verdict row as granted by the human. (Owner ruling 2026-11-09: runs no longer offer new waivers — a measured failure now fails the run; the waive path above remains for verdicts that already carry an offered waiver.) The decision is recorded on the verdict row, so a gate cannot be decided twice.
7. **Lesson writeback.** At the verdict, a Lesson is written from the deciding gate's own evidence — its outcome and evidence pointers are the failing measurement or hard-gate rows; its cause and rule are fixed per-metric guidance strings, not derived from the candidate — and its route change is applied (the expert of the first gate-failing candidate is demoted for that task category; a loss on matrix score alone changes no route) unless the verdict's panel is degraded, in which case the change is recorded and held for the operator (see Panel floor). On the next session's first turn, lessons for the session's domain family are injected into context under the real `pre_llm_call` cap (10,000 characters): ranked by domain match (the exact domain first, then sub-domains and ancestors along the dotted path — siblings are excluded), then tag overlap, then newest; the block is filled to the cap on lesson boundaries and states how many lessons the cap dropped. Hermes passes the hook no domain, so the default `code` family applies and every `code.*` lesson is eligible. **Named residual — lesson lookup cost:** the lookup reads and ranks the whole lessons table on every call (the limit applies after the rank and sort), so the hook's lesson leg is priced by total lesson count rather than by matches — measured medians on the grading machine: 4,000 lessons ≈ 36 ms (limit 5) / 38 ms (no limit); 20,000 lessons ≈ 254–274 ms / 328–337 ms; 50,000 lessons ≈ 745–809 ms / 1,244–1,537 ms, and a domain that matches nothing pays the same scan (≈30 ms at 4,000 lessons, ≈524–586 ms at 50,000). At the graded book scale that is ≈8% of the warm hook and needs nothing; the trajectory is unbounded because lessons are append-only, and pushing the ranking (dotted-path domain family, tag overlap, newest) into SQL is the designed fix when a real book approaches five figures of lessons — not built.

## Install

Requires Hermes **>= 0.21.0** and `git`. Inside Hermes, the 8-family panel uses Hermes's configured `ctx.llm` providers/auxiliary model slots; standalone CLI runs use whatever operator CLIs/Ollama/API keys are installed and logged in (contests compose from whatever seats are available; they never fake a result).

```sh
hermes plugins install <owner>/hybrid-critic-loop
hermes plugins enable the-millionaire-matrix
# restart Hermes (or start a fresh session so tools, /mm, and desktop surfaces load)
```

Standalone, without Hermes (the same loop, driven from a shell):

```sh
pip install .
hcl run contract.json --repo /path/to/repo --approve --data-dir ./hcl-data
```

## Usage

In Hermes:

- Slash commands (`/critic` and `/mm` share the same handler): `/critic run <contract.json> [repo_path]`, `/mm status`, `/mm panel`, `/mm usage`, `/critic gate <contract_id> approve|reject|waive [reason]`, `/critic lessons [domain]`, `/critic ledger`, `/critic standing`
- CLI: `hermes critic run|status|panel|usage|claude-statusline-tee|gate|lessons|ledger|standing|verify-cert|session-start`
- Tools (for the agent itself): `critic_run` (full contract JSON/path OR shorthand `repo_path` + `task` + `mode` + `test_cmd` + `target_files`), `critic_gate` (complete a pending human gate: approve, reject or waive), `critic_matrix` (the validity matrix for a contract: every peer's score per criterion, confidence, dissent flags), `critic_lessons`, `critic_ledger` (dispatches, tokens, cost, win rate, cost per validity point, `active_run` progress, passive `usage`, and the events log). Standing and certificate verification are operator surfaces (slash/CLI), not agent tools.
- Desktop UI: status-bar chip (`MM · IDLE` / `MM · RUNNING` / `MM · GATE READY`) and right-hand **Millionaire Matrix** pane backed by `dashboard/plugin_api.py` (`GET /status`, `GET /usage`, `GET /matrix`, `GET /standing`).

`hermes critic run` prints the run result and the verdict report; `critic_matrix` returns the matrix and the bracket block for a contract; the demo script prints all of it, round by round. Lessons are retrievable with `critic_lessons` and are injected automatically on a session's first turn for the session's domain family (Hermes passes no domain to the hook, so the default `code` family applies).

## Recorded live 8-family proof run & deterministic demo

### 1. Recorded live 8-family `ctx.llm` proof run (`url_slug.py`, 2026-11-10)

Executed via automated harness driving `plugin.register(ctx)` against live Nous Portal models on an isolated git repository (`url_slug.py` + `test_url_slug.py`, shorthand `mode="improve"`). *(Claims discipline: this proof was recorded through an automated Python harness invoking the registered plugin handlers; the operator's own manual interactive smoke test from a fresh Hermes chat/Desktop window has not yet been run.)*

| Seat | Family | Runtime Model Verified | Generation & Hard Gate (`unittest`) | Peer Judge Families | Validity Median | Inner Spread | Outcome |
|---|---|---|---|---|---:|---:|---|
| `mm-anthropic` | `anthropic` | `nous:anthropic/claude-sonnet-5` | Passed | 6 (`alibaba,deepseek,google,moonshot,openai,zai`) | **9.75** | `1.25` | **WINNER** |
| `mm-deepseek` | `deepseek` | `nous:deepseek/deepseek-v4.1-flash` | Passed | 6 (`alibaba,anthropic,google,moonshot,openai,zai`) | **9.50** | `0.50` | Finalist (cut Rd 3) |
| `mm-openai` | `openai` | `nous:openai/gpt-5.4-mini` | Passed | 6 (`alibaba,anthropic,deepseek,google,moonshot,zai`) | **9.00** | `1.25` | Cut Rd 2 |
| `mm-xai` | `xai` | `nous:x-ai/grok-4.6` | Passed | 5 (`alibaba,anthropic,deepseek,google,openai`) | **9.50** | `0.625` | Cut Rd 1 |
| `mm-google` | `google` | `nous:google/gemini-3.8-flash` | Passed | 4 (`alibaba,anthropic,deepseek,openai`) | **8.50** | `1.25` | Cut Rd 1 |
| `mm-moonshot` | `moonshot` | `nous:moonshotai/kimi-k3` | Eliminated (`hard`) | — (`hard > judged` enforced) | — | — | Hard-eliminated |
| `mm-alibaba` | `alibaba` | `nous:qwen/qwen3.8-flash` | Gen 90s timeout; **active judge Rds 1–3** | Scored all non-Alibaba survivors | — | — | Active peer judge |
| `mm-zai` | `zai` | `nous:z-ai/glm-5.3-flash` | Gen 90s timeout; **active judge Rds 1–3** | Scored all non-Z.ai survivors | — | — | Active peer judge |

- **Step-down bracket:** `Round 1 (5 → 3)` → `Round 2 (3 → 2)` → `Round 3 (2 → 1)` (`panel_status: full`, **0 self-scores** across every matrix cell).
- **Human gate & signed stamp:** `critic_gate(..., decision="approved")` merged `mm-anthropic` and issued:
  `MM CERTIFIED · 24,222 TOKENS SPENT · ONE ANSWER`.

### 2. Deterministic offline demo (`demo/run_demo.py`)

`demo/run_demo.py` runs the whole loop end-to-end in this repository against a small fixture (`fixture/python-sampler/`): rolling-average samplers of the kind a physics loop reads every tick. The measured gate is the interesting one — the natural implementation recomputes the average over the whole buffer every tick; the sensor counts the arithmetic and the contract caps it.

The demo panel is three deterministic local solver families (`local-naive`, `local-ring`, `local-balanced`) plus three deterministic local demo judges — packet-text scorers, labelled as such everywhere, **not models**. The script first attempts the live contest with the real `codex`/`grok` CLIs and prints whatever genuinely happens — refusals and unavailability are recorded in the ledger like any other outcome.

- **Run 1 (contract `ctr_demo_1`, SpeedSampler):** baseline tests fail (the fixture ships as stubs) → the panel generates → the naive candidate *and* the balanced candidate (whose deque average still rescans the window) die on the measured gate (ops_per_tick 120.0 vs threshold <= 8; the ring candidate scores 1.0) → a single survivor stands, so the anchor alone decides: verdict, human approval, winner merged → a Lesson is written from the measurement row and the naive expert is demoted in the route table.
- **Fresh session:** the session-start path injects the standing line and the Lesson into context before any new work happens.
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
| Contest | `executors_hermes.py`, `executors.py`, `executors_api.py`, `file_edits.py`, `worktrees.py`, `shellrun.py` | 8-family Hermes `ctx.llm` seats with runtime family verification + 9 standalone CLI/Ollama/API adapters; secret-redacted multi-file prompt building and worktree-confined file extraction; one git worktree per candidate; every subprocess run without a shell and captured (`evidence.log_hash`) |
| Gates | `contracts.py`, `gates.py`, `judges.py`, `rubrics.py` | Contract validation (`fix` & `improve` modes, `min_usage_fraction`), the baseline rule, gate evaluation in rank order, the legacy blinded judge protocol (non-matrix rubrics) |
| Matrix | `matrix.py`, `bracket.py` | The peer validity matrix (self-cells stripped in code, 8-worker parallel peer calls, 1-trimmed spread for 5+ judges, calibration × confidence weighting, dissent + gate re-run requests, `on_judge_start`/`on_judge_done` hooks) and the step-down bracket |
| Visibility & Usage | `progress.py`, `usage_feed.py`, `dashboard/plugin_api.py`, `desktop/plugin.js` | `RunProgressTracker` (`run_status.json` + `run.progress` events + storefront `agent_cursor`/`active_seat_ids`/`being_prompted`), normalized per-pool plan-usage feed (`UsageCache`, `300s` floor, fail-closed `unknown`), read-only FastAPI `/status` `/usage` `/matrix` `/standing`, Desktop status-bar chip & pane (`UsagePoolsCard`) |
| State | `state.py`, `chain.py` | SQLite store with the twelve domain objects: targets, contracts, candidates, measurements, scores, verdicts, final_answers, experts, routes, ledger, lessons, events; the hash-chained ledger seal and the signed per-run certificates |
| Memory | `lessons.py`, `router.py`, `standing.py` | Evidence-derived Lesson writing and the capped lesson context block; route-table updates (`PANEL_FAMILIES` incl. `zai`); lifetime standing and tiers. Lesson ranking lives in `state.py` (`lessons_for`), injection in `hooks.py` (`pre_llm_call`) |
| Surface | `plugin.py`, `hooks.py`, `cli.py`, `loop.py`, `reports.py` | Hermes registration (5 tools, `/critic` & `/mm`, `hermes critic`, `millionaire_matrix` system-prompt guidance, 2 hooks: `on_session_start`, `pre_llm_call`), the queue/orchestration loop, DB-only reporting |

Reports print only database rows (and live `run_status.json` / `usage_feed` readings labeled with their source and freshness); anything absent prints as `not run`, `not reported`, or `unknown` — token, cost, and quota totals are never estimated.

## Scope, verification status, and roadmap

Shipped in this entry (`v0.4.0`, branch `feat/live-8x1-and-ui`, code at `c911fe5` — local repository, nothing pushed yet):
- **Contest & panel:** the 8-family Hermes `ctx.llm` panel (`mm-openai` through `mm-zai` with runtime family verification and 8-worker parallelism) plus standalone CLI/API adapters (adapters for nine seats across the eight families — in standalone CLI mode the `agy` and `beam` seats decline every dispatch and say so), `fix` and `improve` modes, shorthand `critic_run`, `millionaire_matrix` system-prompt guidance, and `/mm` commands.
- **Scoring, memory & integrity:** the peer validity matrix with dissent, 1-trimmed multi-judge spread, and calibration; the scored step-down bracket; cost per validity point; the durable events log; Lesson writeback with route-table demotion/promotion; first-turn lesson injection; lifetime standing and tiers (the client book); the hash-chained ledger; and signed per-run certificates.
- **Visibility & usage data layer:** `RunProgressTracker` (`run_status.json`), read-only `dashboard/plugin_api.py` (`GET /status`, `GET /usage`, `GET /matrix`, `GET /standing`), Desktop status-bar chip + side pane (`UsagePoolsCard`), and `usage_feed.py` per-pool quota normalization + storefront seat-activity cursor (`agent_cursor`, `active_seat_ids`, `being_prompted`).
- **Verification:** `867/867` unit tests passing (`skipped=1`) under both Python 3.14.7 and Hermes venv Python 3.11.15; `hermes plugins validate` (`16/16` checks, `0` warnings, `0` errors) and `hermes plugins doctor` (`5 tool(s), 2 hook(s)`) clean.

Deliberately not built yet: the **animated storefront front end** (walking-agent visualization over `agent_cursor` / `/usage`), a headless `agy` candidate/judge dispatch adapter, hosted public certificate verification, and the original design's Phase 2 items (decomposition and stability matrix rows, a revision round, an attacker seat, and the spec wave).
