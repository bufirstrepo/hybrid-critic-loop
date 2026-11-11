# AI Brief — The Millionaire Matrix

*Read this first if you are an AI working with, operating, or extending this
program. It states what the program does in the current tree, how its surfaces
behave, the honest limits of what is and is not built, and the invariants you
must not redesign. For the full human-facing reference see `README.md`; for the
submission checklist see `SUBMISSION.md`; the code is the ground truth whenever
any document disagrees with it.*

## What it is

The Millionaire Matrix is a Hermes Agent plugin (Python package
`hybrid_critic_loop`, v0.4.0) that turns one coding or refactoring task into a
test-anchored, blind-peer-judged 8-to-1 contest. Every available model family
generates its own candidate implementation in an isolated git worktree; hard
and measured gates anchored in real tests eliminate what they eliminate; the
survivors blind-score each other on a validity matrix; the field steps down
round by round until one answer remains behind a human approval gate; and the
deciding evidence is written back as a Lesson injected on the next session's
first turn, with gate-failing seats demoted in the route table so the same
failure is not bought twice.

Tagline: "You want to prove you're rich? Show us you're running the
Millionaire Matrix."

## A contest, end to end

1. **Contract (`fix` or `improve` mode).** Accepts either a full contract JSON
   or shorthand `critic_run` arguments (`repo_path`, `task`, `mode`,
   `test_cmd`, `target_file` / `target_files`, optional `min_usage_fraction`).
   Acceptance is always structured: hard gates (command + expected exit code,
   executed with `shell=False`), measured gates (JSON sensor command +
   metric/operator/threshold), and/or a judged gate naming
   `validity_matrix_v1`. Prose-only and judged-only contracts are refused.
2. **Baseline rule (mode-specific).**
   - `mode="fix"` (default on full contract files): acceptance must **fail** on
     the unmodified baseline (`fail`); a passing baseline is refused as vacuous.
   - `mode="improve"` (default on shorthand `critic_run`): acceptance must
     **pass** on the unmodified baseline (`pass_improve`; broken baselines are
     refused), and `loop.py` hard-eliminates any candidate that produces zero
     git diff against baseline.
   - A contract id is one run: finished contracts (`completed`,
     `awaiting_human_gate`, `escalated`, `rejected`) are never re-dispatched;
     refused contracts re-verify baseline on retry.
3. **Pre-flight & parallel generation.** Optional `min_usage_fraction`
   (`0.0..1.0`) checks deduplicated quota pools before creating worktrees —
   skipping only verified exhausted/below-threshold pools (`unknown` and
   `unmetered_local` pools are never blocked). Available seats generate in
   isolated git worktrees (`wt/<contract>/<slot>`, branch
   `critic/<contract>/<slot>`; parallel-safe seats run in up to 8 concurrent
   workers). Only `provenance == "generated"` can pass a gate.
4. **Gates, in rank order.** Hard, then measured, then the blind peer matrix.
   Anchor gates always outrank the matrix. If one candidate survives the anchor
   gates, the anchor decides without running the matrix; two or more enter the
   bracket. Gate-falling fields retry with error logs attached up to attempt
   cap `3`, then escalate.
5. **The blind peer matrix.** Every judge-capable available seat (`contestants`
   included, up to `judges`) scores every *other* family's candidate across
   `question_fidelity`, `completeness`, `evidence`, and `test_agreement`.
   Self-cells are stripped in code before dispatch, and `HermesAuxExecutor`
   additionally checks `infer_model_family(result.model)` at runtime so
   provider fallbacks can never cause cross-family impersonation or
   self-scoring. Scores weight by `judge_calibration × confidence`. For panels
   with 5+ peer families, `matrix.py` computes 1-trimmed inner spread so a
   single outlier judge triggers `dissent` (which can force a one-time full
   hard/measured gate re-run) while genuine panel splits above
   `spread_threshold` (`2.0` full contract default; `3.5` shorthand default)
   escalate.
6. **The step-down bracket.** Pure cuts over recorded scores (`cut_per_round`,
   default `2`, or `cut_mode: "half"`) down to a final two, then head-to-head
   to `1`. Matrix failers drop before the cut. Tie-break: fewer tokens wins,
   then shorter generation duration. Never synthesizes or blends candidates.
7. **Human gate.** Stops at `awaiting_human_gate`. Completing via `critic_gate`,
   `/mm gate` / `/critic gate`, or `hermes critic gate` merges the winner's
   snapshot (`approved` / `waived`) or opens `<id>_rej<attempt>` (`rejected`).
   Decisions are one-shot.
8. **Lesson writeback.** Writes an evidence-derived Lesson (`written_to:
   plugin_store`) and demotes the first gate-failing expert behind gate-passers
   (held on `degraded` panels with `< 2` peer judge families). Injected via
   `pre_llm_call` on the next session's first turn under a 10,000-character cap.

## The surfaces

- **Agent tools (5):** `critic_run` (full contract OR shorthand `improve`/`fix`),
  `critic_matrix`, `critic_lessons`, `critic_ledger` (includes `active_run` and
  passive `usage`), `critic_gate`.
- **Hooks (2) & System Prompt:** `on_session_start`, `pre_llm_call`, plus
  `register_system_prompt_section("millionaire_matrix", ...)` teaching natural
  chat invocation (*"improve / look at code with Millionaire Matrix"*).
- **Slash & CLI:** `/mm` and `/critic` (`run | status | panel | usage | gate |
  lessons | ledger | standing`); `hermes critic` / `hcl` (`run | status |
  panel | usage | claude-statusline-tee | gate | lessons | ledger | standing |
  verify-cert | session-start`).
- **Live progress & Desktop UI:** `RunProgressTracker` writes atomic snapshots
  to `<data_dir>/run_status.json` (with dead-PID cleanup) and broadcasts
  `plugin.the-millionaire-matrix.run.progress`. `dashboard/plugin_api.py`
  serves read-only `GET /status`, `GET /usage`, `GET /matrix`, and
  `GET /standing` (never creating `data.db` on fresh installs);
  `desktop/plugin.js` renders the status-bar chip, command-palette action, and
  side pane (`UsagePoolsCard`, seat grid, bracket, matrix, standing, lessons).
- **Per-pool usage feed & storefront backend (`usage_feed.py`):**
  - Deduplicates shared-credential seats onto `pool_id` (`pools`, not seats)
    with SHA-256 `acct-<8hex>` pseudonymization.
  - Enforces strict polarity (`USED` inverted via `1 - used` for Codex
    `usedPercent`, Grok `creditUsagePercent`, Claude `used_percentage` `0–100`
    & `utilization` `0–1`, Kimi `used_ratio` `0–1`; `LEFT` kept un-inverted for
    Codex enterprise `individualLimit.remainingPercent` and Antigravity
    `remaining_fraction`), fail-closed `status = "unknown"` (`fraction_left =
    None`) past `min(resets_at, fetched_at + ttl_s)`, `300s` per-pool active
    poll floor, transport-vs-auth cache failure asymmetry, zero OAuth token
    refreshes, zero credential writes, and read-only Codex `reset_credits`
    (`consume_policy = "human_only"`).
  - Publishes live storefront seat-activity state (`active_seat_ids`,
    `being_prompted`, monotonic `agent_cursor`) in `run_status.json` and
    `GET /usage`. **Data layer only — the animated walking-agent storefront UI
    is NOT built.**

## The panel (two execution modes)

`router.PANEL_FAMILIES` names the eight target families: `anthropic`, `openai`,
`xai`, `google`, `alibaba`, `moonshot`, `deepseek`, `zai`.

1. **Inside Hermes (`ctx.llm` available — default `live` mode):**
   `executors_hermes.py` registers and fields 8 auxiliary task seats
   (`mm-openai`, `mm-anthropic`, `mm-xai`, `mm-google`, `mm-alibaba`,
   `mm-moonshot`, `mm-deepseek`, `mm-zai`), editing confined `target_file` /
   `target_files` blocks in isolated worktrees with secret redaction and
   runtime family verification.
2. **Outside Hermes / `--executors cli`:** `executors.py` + `executors_api.py`
   field 9 standalone seats: `claude` (CLI + opportunistic `rate_limit_event`
   harvest), `codex` (CLI), `grok` (CLI), `agy` (CLI — declines generate/judge
   dispatch; read-only `/usage` used only by `usage_feed.py`), `beam`
   (`reflection` placeholder, always unavailable), `qwen-local` (local Ollama),
   `qwen-cli` (CLI), `kimi` (CLI), and `deepseek` (`DEEPSEEK_API_KEY`).
   Standalone mode has no `zai` CLI adapter.

## Recorded live proof & verification state

- **Recorded live 8-family proof (`2026-11-10`, automated harness over
  `plugin.register(ctx)` on Nous Portal):** Shorthand `mode="improve"` run on
  isolated git repo (`url_slug.py` + `test_url_slug.py`). All 8 `mm-*` seats
  dispatched; 5 passed `unittest`, `mm-moonshot` failed the hard gate, and
  `mm-alibaba` / `mm-zai` timed out on 90s generation but served as active peer
  judges across all 3 rounds. Bracket stepped `5 → 3 → 2 → 1` (`0`
  self-scores); `mm-anthropic` (`nous:anthropic/claude-sonnet-5`) won with
  validity median `9.75` over `mm-deepseek` (`9.50`); gate approval minted
  `MM CERTIFIED · 24,222 TOKENS SPENT · ONE ANSWER`. *(Dino's manual
  interactive smoke test from a fresh Hermes window has NOT yet happened — do
  not claim or imply that it has.)*
- **Current test & validator counts (`c911fe5`):** `867/867` unit tests passing
  (`skipped=1`) on Python `3.14.7` and Hermes venv Python `3.11.15`; `hermes
  plugins validate` (`16/16` checks, `0` warnings, `0` errors) and `hermes
  plugins doctor` (`5 tool(s), 2 hook(s)`) clean.

## State, standing, and the stamp

One SQLite store (WAL) with twelve domain objects: `targets`, `contracts`,
`candidates`, `measurements`, `scores`, `verdicts`, `final_answers`, `experts`,
`routes`, `ledger`, `lessons`, `events`. The ledger is SHA-256 hash-chained and
each completed run's `mm1.*` certificate is HMAC-SHA256 signed with `cert_key`
(`0600`). Book backups require `data.db`, `data.db-wal`, `data.db-shm`, and
`cert_key` together. Lifetime measured spend sets standing tiers (`WINDOW
SHOPPER` through `MILLIONAIRE`); unmeasured cost or token segments are never
estimated.

## Invariants — do not redesign

- Hard and measured gates outrank the blind peer matrix, always.
- No self-scoring, ever (stripped in code + runtime `infer_model_family` guard
  on `ctx.llm`); no synthesis/blending of candidates, ever — one answer from
  one seat behind one human gate.
- Baseline discipline is strict per mode: `fix` requires baseline failure;
  `improve` requires baseline pass plus a non-empty candidate diff.
- Measured regressions are never offered new waivers (owner ruling 2026-11-09);
  hard-gate failures can never be waived.
- Route demotion: first gate-failing seat is demoted behind gate-passers;
  matrix-score losses change no route; degraded panels (`< 2` peer families)
  hold route changes instead of applying them; bracket-cut gate-passers are not
  calibration negatives.
- Usage-feed safety: pools not seats; never flip `USED` vs `LEFT` polarity;
  stale/missing = `unknown` (`None`), never fabricated `0.0`/`1.0`; never
  refresh another CLI's OAuth token or mutate credential files; never invoke
  Codex's reset-credit consume RPC (`human_only`).

## Safety posture (Catalog Rule 13 summary)

Executes shell commands (model CLIs, read-only quota probes, contract gates,
sensors, `git`) and candidate test suites as the operator using the operator's
own credentials; writes only under `<data_dir>` and target repo worktrees; has
no hosted control plane and sends no telemetry. **Only run contracts whose
authors you trust to run code as you**, in disposable repositories.

## File map

| Layer | Modules | Responsibility |
|---|---|---|
| Contest | `executors_hermes.py`, `executors.py`, `executors_api.py`, `file_edits.py`, `worktrees.py`, `shellrun.py` | 8 Hermes `ctx.llm` seats + 9 standalone adapters; redacted prompts & confined multi-file edits; git worktrees; `shell=False` capture |
| Gates | `contracts.py`, `gates.py`, `judges.py`, `rubrics.py` | Contract validation (`fix`/`improve`, `min_usage_fraction`), baseline verification, ranked gate evaluation |
| Matrix | `matrix.py`, `bracket.py` | Parallel blind peer validity matrix (1-trimmed spread for 5+ judges, judge start/done hooks); step-down bracket |
| Visibility & Usage | `progress.py`, `usage_feed.py`, `dashboard/plugin_api.py`, `desktop/plugin.js` | `RunProgressTracker` (`run_status.json`, `agent_cursor`), normalized pool quota feed (`UsageCache`), read-only FastAPI routes, Desktop chip & pane |
| State | `state.py`, `chain.py` | WAL SQLite store; hash-chained ledger; `mm1` certificates |
| Memory | `lessons.py`, `router.py`, `standing.py` | Evidence-derived Lessons; `PANEL_FAMILIES` routing; standing tiers |
| Surface | `plugin.py`, `hooks.py`, `cli.py`, `loop.py`, `reports.py` | Hermes registration (5 tools, 2 hooks, `/mm` & `/critic`, `hermes critic`, system-prompt section), orchestration loop, DB-only reports |

## Honest limits & operating notes

- **Claude & Hermes-Nous usage pools:** `claude` reports `unknown` (`plan=max`)
  until the operator wires `hermes critic claude-statusline-tee` or runs a
  Claude CLI stream turn; `hermes:nous` reports `unknown` (no public `ctx`
  usage seam under Catalog Rule 9).
- **`NOT RUN` verification items:** `P3` Windows Credential Manager (Linux host
  only); `P1`/`P7` paid CLI turns (`grok -p`, `claude -p` Haiku — skipped under
  no-spend rule); `P4`/`P5` live API/Kimi endpoints (keys/`kimi` binary absent
  on host; synthetic fixtures tested in `tests/test_usage_feed.py`).
- **Not built:** Animated storefront UI, headless `agy` generate/judge adapter,
  hosted public certificate verification, SQL-pushed lesson ranking (`Named
  residual — lesson lookup cost` in `README.md` §7).
- **Tree anchor:** Describes `v0.4.0` on branch `feat/live-8x1-and-ui` (code
  anchored at `c911fe5`, 3 feature commits ahead of `master` pin `1483c10`;
  unpushed; catalog `sha:` placeholder pending push).
