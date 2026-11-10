# Submission checklist — The Millionaire Matrix

Deadline: **November 11, 2026, 5:00 PM EST** (verified from Nous's own announcement; no conflicting source).

An entry is THREE steps. A pull request plus a video alone is **not** an entry — both links must also go through Nous's official submission form (step 3).

## Step 0 — before anything (owner)

- [ ] Create the public repository `github.com/dynoshippedit/hybrid-critic-loop` and push this folder's contents (the build was developed locally; nothing has been pushed anywhere). The repository name may stay `hybrid-critic-loop` — the Python package/import name is unchanged; the product's display name is The Millionaire Matrix.
- [ ] Note the full 40-character SHA of the release commit. The release this checklist ships is v0.4.

## Step 1 — Catalog PR to NousResearch/hermes-agent

- [ ] Add `plugin-catalog/the-millionaire-matrix.yaml` (the file at this repo's root) to the `plugin-catalog/` directory of `NousResearch/hermes-agent`.
- [ ] In that file, replace the `sha:` placeholder with the full 40-character commit SHA from step 0.
- [ ] Validate the entry before opening the PR:

  ```sh
  hermes plugins validate
  ```

  Status: **PASSED on the operator's machine for the v0.3 tree** (2026-11-07, Hermes v0.21.5: all 13 checks, security scan safe, both the plain and the `--install-deps` form — see BUILD-LOG.md, parent verification). Capabilities in the YAML mirror `plugin.yaml` exactly (5 tools, 2 hooks) because the validator checks them against what the code registers. The release commit will differ from that validated tree — it stands 51 commits after the validated v0.3 tree (3923dad..e12c59c).
- [ ] Re-run `hermes plugins validate` on the exact release commit and record the result here.
- [ ] PR description must carry the Rule 13 disclosure — the same five bullets that open the README ("Disclosures (read first)"), copied verbatim:

  > - **Executes shell commands.** It runs the model CLIs (`codex exec`, `grok -p`, and the other family adapters below), the acceptance commands written into each contract (unit tests, build commands), the sensor commands that produce measured-gate metrics, and `git` (worktree, diff, merge, branch). Contract acceptance commands are operator-authored; the plugin runs them as written, in the candidate's worktree.
  > - **Makes network calls through the operator's own credentials.** Model traffic goes through the operator's logged-in model CLIs, the operator's local ollama server (alibaba seat), or API adapters keyed by the operator's own environment variables (DeepSeek). The plugin holds no keys of its own and sends no telemetry anywhere.
  > - **Runs candidate code.** Acceptance tests execute the candidates' implementations. Run contests in a disposable repository, never in a tree you cannot afford to have code executed in.
  > - **Writes local state.** A SQLite database (WAL mode), a human-readable `lessons.md` and a `session.json` under the plugin's data directory — the one Hermes assigns to this plugin, or `HCL_DATA_DIR` outside Hermes (default `<HERMES_HOME>/plugin-data/hybrid-critic-loop/`) — and, inside the operator's target repository, one git worktree per candidate (`wt/<contract>/<slot>`) with `critic/<contract>/<slot>` branches (losers are kept as branches after a merge). Nothing is written inside the plugin's own directory.
  > - **No hosted control plane.** The queue, the gates, and the matrix all run locally. The Beam seat (Reflection) is reserved for the Nous-portal-hosted model: this build ships no Beam adapter path, the seat always reports unavailable with that note, and the entry never depends on it.

## Step 2 — Demo video

- [ ] Record the demo (under 90 seconds). Suggested beats, from `demo/run_demo.py` (the Python fixture; the C# fixture is not run by this script and no contest has been run against it): baseline tests fail on the stubs → the panel generates in separate worktrees → hard gate passes, measured gate kills the naive and balanced candidates (ops_per_tick 120.0 vs threshold <= 8; the ring candidate scores 1.0) → the peer validity matrix fills in on screen → the bracket steps the field down round by round with the cut line visible → the final two are scored head-to-head → verdict + human approval + merge → the Lesson prints and the route table shows the demotion → the verdict report prints the MM CERTIFIED stamp → run 2 starts with the standing line and the Lesson in context.
- [ ] Post the video on X, tagging **@NousResearch**.

## Step 3 — Official submission form (owner's final action)

- [ ] Submit BOTH links — the catalog PR URL and the X post URL — through Nous's official submission form. This step is what makes it an entry; do not skip it, and do not treat the PR or the post as a substitute.

## Judging criteria (for the final polish pass)

Usefulness, reliability, creativity, **polish**. Nous favors plugins that do more than a prompt or a skill could — the README and the video should show the machinery (contract, baseline rule, gate stack, peer matrix, bracket, judge blinding, lesson writeback, route demotion), not just an outcome.
