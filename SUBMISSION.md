# Submission checklist — The Millionaire Matrix

Deadline: **November 11, 2026, 5:00 PM EST** (verified from Nous's own announcement; no conflicting source).

An entry is THREE steps. A pull request plus a video alone is **not** an entry — both links must also go through Nous's official submission form (step 3).

## Current local build state (pre-push)

- **Branch & commits:** `feat/live-8x1-and-ui` (`8` commits ahead of `master` at `1483c10` and `66` commits after the earlier validated v0.3 tree `3923dad`). Push the final Rule-11-clean commit to `github.com/bufirstrepo/hybrid-critic-loop` before opening the catalog PR.
- **Release version:** `v0.4` (`0.4.0` across `pyproject.toml`, `plugin.yaml`, and `the-millionaire-matrix.yaml`).
- **Automated verification (`2026-11-11`):**
  - `869/869` unit tests passing (`skipped=1`) under both Python `3.14.7` and the Hermes venv Python `3.11.15`.
  - `hermes plugins validate . --install-deps` — **PASSED** (`16/16` checks green, `0` errors, `0` warnings, security scan safe, desktop surface inside SDK bounds).
  - `hermes plugins doctor` — **PASSED** (`manifest: the-millionaire-matrix 0.4.0 (standalone)`, `registrations: 5 tool(s), 2 hook(s)`).
  - `tools/check_repo_hygiene.py` — **PASSED** (zero untracked files, zero tracked runtime artifacts).
- **Pending owner actions before opening the PR:**
  1. Run your own manual interactive smoke test from a fresh Hermes chat/Desktop window (the recorded `url_slug.py` live 8-family proof run below executed through an automated Python harness over `plugin.register(ctx)`, not a manual interactive window).
  2. Push the final release branch to `github.com/bufirstrepo/hybrid-critic-loop` and copy the exact 40-character release commit SHA into `the-millionaire-matrix.yaml` (`sha:` is intentionally still `"REPLACE-WITH-FULL-40-CHAR-COMMIT-SHA"` until that push exists).

## Step 0 — before anything (owner)

- [ ] Run a quick interactive smoke check from a fresh Hermes session (`/mm panel`, `/mm usage`, and an `improve` or `fix` run with the Desktop pane open).
- [ ] Push `feat/live-8x1-and-ui` to the public repository `github.com/bufirstrepo/hybrid-critic-loop`. The repository name may stay `hybrid-critic-loop` — the Python package/import name is unchanged; the product's display name is The Millionaire Matrix.
- [ ] Note the full 40-character SHA of the pushed release commit. The release this checklist ships is v0.4 (`0.4.0`).

## Step 1 — Catalog PR to NousResearch/hermes-agent

- [ ] Copy `the-millionaire-matrix.yaml` (at this repo's root) to `plugin-catalog/the-millionaire-matrix.yaml` in `NousResearch/hermes-agent`.
- [ ] Replace `sha: "REPLACE-WITH-FULL-40-CHAR-COMMIT-SHA"` with the exact 40-character commit SHA pushed in Step 0.
- [ ] Validate the entry before opening the PR:

  ```sh
  hermes plugins validate /path/to/hybrid-critic-loop --install-deps
  hermes plugins doctor /path/to/hybrid-critic-loop
  ```

  Status: **PASSED on the operator's machine on `feat/live-8x1-and-ui` (2026-11-11)**: all 16 `hermes plugins validate --install-deps` checks (`0` errors, `0` warnings, security scan safe, desktop surface verified) plus `hermes plugins doctor` (`OK: runtime discovery, manifest parsing, import, and registration passed`). Capabilities in `the-millionaire-matrix.yaml` mirror `plugin.yaml` `(5 tools, 2 hooks)` (`plugin.yaml` also declares `emits: [run.progress]` and ships `dashboard/plugin_api.py` + `desktop/plugin.js` for the host UI).
- [ ] Re-run `hermes plugins validate --install-deps` on the exact pushed release SHA and confirm `0` warnings / `0` errors.
- [ ] PR description must carry the Rule 13 disclosure — the same five bullets that open `README.md` ("Disclosures (read first)"), copied verbatim:

  > - **Executes shell commands.** It runs the model CLIs (`codex exec`, `grok -p`, and the other family adapters below), read-only CLI quota probes (`codex app-server`, `grok agent stdio`, `agy -p "/usage"`, `claude auth status`), the acceptance commands written into each contract (unit tests, build commands), the sensor commands that produce measured-gate metrics, and `git` (worktree, diff, merge, branch). Contract acceptance commands are operator-authored; the plugin runs them as written, in the candidate's worktree.
  > - **Makes network calls through the operator's own credentials.** Inside Hermes, model traffic routes through Hermes's configured `ctx.llm` auxiliary task slots (`mm_openai` through `mm_zai`) and the DeepSeek API adapter resolves `DEEPSEEK_API_KEY` exclusively via `agent.secret_scope.get_secret` (no `os.environ` fallback); outside Hermes (or in `--executors cli` mode), traffic goes through the operator's logged-in model CLIs or the operator's local ollama server (alibaba seat), plus optional read-only CLI plan-usage checks via those CLIs' own subcommands (`codex app-server`, `grok agent stdio`, `agy -p "/usage"`, `claude auth status`). Under Catalog Rule 11, the plugin never reads third-party login or credential files (`auth.json`, `.credentials.json`, `kimi-code.json`), holds no keys of its own, and sends no telemetry anywhere.
  > - **Runs candidate code.** Acceptance tests execute the candidates' implementations. Run contests in a disposable repository, never in a tree you cannot afford to have code executed in.
  > - **Writes local state.** A SQLite database (WAL mode), `cert_key`, a human-readable `lessons.md`, `session.json`, live progress snapshots (`run_status.json`), and cached quota readings (`usage_feed_cache.json`, plus optional passive Claude statusline/stream harvest files) under the plugin's data directory — the one Hermes assigns to this plugin, or `HCL_DATA_DIR` outside Hermes (default `<HERMES_HOME>/plugin-data/hybrid-critic-loop/`) — and, inside the operator's target repository, one git worktree per candidate (`wt/<contract>/<slot>`) with `critic/<contract>/<slot>` branches (losers are kept as branches after a merge). Nothing is written inside the plugin's own directory.
  > - **No hosted control plane.** The queue, the gates, the usage feed, and the matrix all run locally. In standalone CLI mode, the legacy Beam seat (Reflection) is reserved for a Nous-portal-hosted standalone path: this build ships no standalone Beam adapter, that CLI seat always reports unavailable with that note, and inside Hermes the 8-family panel runs directly over `ctx.llm` (`mm-openai` through `mm-zai`).

## Step 2 — Demo video

- [ ] Record the demo (under 90 seconds). Two verified demonstration paths exist in this tree:
  1. **Live Hermes 8-family `improve` mode + Desktop pane:** show `/mm panel` (`8/8 families ready -> 1 winner`) and `/mm usage` (`UsagePoolsCard` per-pool quota headroom), trigger `critic_run` in `improve` mode from Hermes chat with the **Millionaire Matrix** right-side pane and status-bar chip open (`run_status.json` live generation/judging badges, blind peer matrix scores, step-down bracket ladder, human gate approval, and `MM CERTIFIED` stamp — matching the harness-recorded `url_slug.py` proof where `mm-anthropic` won at validity median `9.75` through a `5 → 3 → 2 → 1` bracket and minted `MM CERTIFIED · 24,222 TOKENS SPENT · ONE ANSWER`). *(Note: the animated walking-agent storefront UI is not built — only the desktop chip/pane + `UsagePoolsCard` and backend `agent_cursor` data layer exist today.)*
  2. **Deterministic two-run loop (`demo/run_demo.py`, `fixture/python-sampler/`):** run 1 — baseline tests fail on the stubs → the panel generates in separate worktrees → hard gate passes, measured gate kills the naive and balanced candidates (`ops_per_tick 120.0` vs threshold `<= 8`; the ring candidate scores `1.0`) → a single survivor stands, so the anchor alone decides (no matrix in run 1): verdict + human approval + merge → the run prints its `MM CERTIFIED` stamp → the Lesson prints and the route table shows the demotion → a fresh session starts with the standing line and the Lesson in context → run 2 — the lesson-conditioned panel generates again → the peer validity matrix fills in on screen → the bracket steps the field down round by round with the cut line visible → the final two are scored head-to-head → one answer walks out. *(The Godot C# fixture under `fixture/godot-csharp/` is not run by `demo/run_demo.py` and no contest has been run against it.)*
- [ ] Post the video on X, tagging **@NousResearch**.

## Step 3 — Official submission form (owner's final action)

- [ ] Submit BOTH links — the catalog PR URL and the X post URL — through Nous's official submission form. This step is what makes it an entry; do not skip it, and do not treat the PR or the post as a substitute.

## Honest limits & verification notes carried with the submission

- **Claims boundary:** Live 8-family `ctx.llm` behavior is claimed from the recorded `url_slug.py` harness proof (`2026-11-10`); manual interactive smoke testing from a fresh Hermes window awaits Step 0 above.
- **Usage-feed `unknown` pools on default install:** `claude` reports `unknown` (`plan=max`) until the operator wires `hermes critic claude-statusline-tee` in `~/.claude/settings.json` or runs a `ClaudeExecutor` stream turn; `kimi` reports `unknown` by design under Catalog Rule 11 (never reads `kimi-code.json` or makes bearer-token `/usages` HTTP calls); `hermes:nous` reports `unknown` (no public `ctx` quota seam under Catalog Rule 9).
- **Usage-feed `NOT RUN` register:** `P3` Windows Credential Manager (Linux host only; synthetic path tests run); `P1` Step 2 (`grok -p`) & `P7` Step 1/3 (`claude -p` Haiku) paid CLI turns (skipped under zero-spend verification rules); `P4` live DeepSeek/Moonshot/OpenRouter balance HTTP calls & `P5` live Kimi `/usages` HTTP call (Kimi credential-file reads disabled under Catalog Rule 11 and API keys absent on host; synthetic parser fixtures tested).
- **Presentation scope:** `usage_feed.py`, `RunProgressTracker` (`agent_cursor`, `active_seat_ids`, `being_prompted`), `dashboard/plugin_api.py` (`GET /usage`, `GET /status`), and `desktop/plugin.js` (`UsagePoolsCard`, chip, pane) are shipped; the animated storefront visualization is **not built**.

## Judging criteria (for the final polish pass)

Usefulness, reliability, creativity, **polish**. Nous favors plugins that do more than a prompt or a skill could — the README and the video should show the machinery (contract/`improve` mode, baseline rule, gate stack, 8-family `ctx.llm` runtime family check, blind peer matrix, step-down bracket, live Desktop & usage-pool visibility, lesson writeback, route demotion, hash-chained certificates), not just an outcome.
