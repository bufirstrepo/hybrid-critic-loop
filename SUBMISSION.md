# Submission checklist — The Millionaire Matrix

Deadline: **November 11, 2026, 5:00 PM EST** (verified from Nous's own announcement; no conflicting source).

An entry is THREE steps. A pull request plus a video alone is **not** an entry — both links must also go through Nous's official submission form (step 3).

## Step 0 — before anything (owner)

- [ ] Create the public repository `github.com/dynoshippedit/hybrid-critic-loop` and push this folder's contents (the build was developed locally; nothing has been pushed anywhere). The repository name may stay `hybrid-critic-loop` — the Python package/import name is unchanged; the product's display name is The Millionaire Matrix.
- [ ] Note the full 40-character SHA of the release commit.

## Step 1 — Catalog PR to NousResearch/hermes-agent

- [ ] Add `plugin-catalog/the-millionaire-matrix.yaml` (the file at this repo's root) to the `plugin-catalog/` directory of `NousResearch/hermes-agent`.
- [ ] In that file, replace the `sha:` placeholder with the full 40-character commit SHA from step 0.
- [ ] Validate the entry before opening the PR:

  ```sh
  hermes plugins validate
  ```

  Status: **PASSED on the operator's machine for v0.1.0** (Hermes v0.21.5, all 13 checks — see BUILD-LOG.md). The v0.3 entry adds one tool (`critic_matrix`); capabilities in the YAML mirror `plugin.yaml` exactly (4 tools, 4 hooks) because the validator checks them against what the code registers — re-run validate on the submission machine for the v0.3 tree before opening the PR.
- [ ] PR description must carry the Rule 13 disclosure (the same disclosure that opens the README). Copy verbatim:

  > This plugin executes shell commands: the model CLIs (`codex exec`, `grok -p`), the operator-authored acceptance and sensor commands from each contract, and `git` worktree/merge operations. It makes no network calls of its own and holds no API keys; model traffic goes through the operator's own logged-in model CLIs. It runs candidate code during acceptance testing — use a disposable repository. State (SQLite + lessons.md) is written only under the Hermes plugin-data directory.

## Step 2 — Demo video

- [ ] Record the demo (under 90 seconds). Suggested beats, from `demo/run_demo.py` and the C# fixture on the operator's machine: baseline tests fail on the stubs → the panel generates in separate worktrees → hard gate passes, measured gate kills the allocating candidates (alloc_bytes_per_tick vs threshold 0) → the peer validity matrix fills in on screen (one column marked down by every peer = drift; one lone low score = a dissent flag) → the bracket steps the field down round by round with the cut line visible → the final two are scored head-to-head → verdict + human approval + merge → the Lesson prints and the route table shows the demotion → run 2 starts with the Lesson in context.
- [ ] Post the video on X, tagging **@NousResearch**.

## Step 3 — Official submission form (owner's final action)

- [ ] Submit BOTH links — the catalog PR URL and the X post URL — through Nous's official submission form. This step is what makes it an entry; do not skip it, and do not treat the PR or the post as a substitute.

## Judging criteria (for the final polish pass)

Usefulness, reliability, creativity, **polish**. Nous favors plugins that do more than a prompt or a skill could — the README and the video should show the machinery (contract, baseline rule, gate stack, peer matrix, bracket, judge blinding, lesson writeback, route demotion), not just an outcome.
