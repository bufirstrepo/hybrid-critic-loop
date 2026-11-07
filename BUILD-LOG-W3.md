# BUILD-LOG-W3 — Executor panel adapters + judge-output v2 (Hybrid Critic Loop v0.3)

Workstream W3. Owned files only (all edits confined to these three, plus this log):
- `hybrid_critic_loop/executors.py`
- `tools/dev_solver.py`
- `tests/test_executors.py`

No commits made. No sibling files modified. No model run is claimed anywhere
below: every CLI adapter in this environment reported unavailable except the
pre-existing codex seat, and no live generate/judge call was made.

## What was implemented

1. **JUDGE_INSTRUCTION_V2** (`executors.py`): judges return one key per
   candidate label shown in the packet, each mapping to
   `{"items": {<rubric row>: {"score": <0-10>, "confidence": <0-1>,
   "evidence": "<pointer>", "rationale": "<one line>"}}}`. The instruction
   text says "one key per candidate label shown in the packet" (not just
   A/B). `CodexExecutor.judge()` and `GrokExecutor.judge()` now use V2 and
   still return the parsed dict unchanged (validation stays in the judge
   layer). `JUDGE_INSTRUCTION` is kept as an alias of the V2 text — a grep
   over the repo before editing showed no test referenced the old name, but
   the alias keeps any external reference working.
2. **New CLI adapters** behind the same `Executor` interface, sharing
   `_cli_generate` / `_cli_judge` helpers that mirror the Codex/Grok
   patterns (JSONL usage parse, refusal-marker detection, timeout/exit
   handling; `judge()` uses JUDGE_INSTRUCTION_V2 and `_extract_first_json_object`):
   - `AgyExecutor` — expert_id `agy`, family `google`, binary `agy`.
     `available()` is the honest `shutil.which` result, but generate/judge
     never invent flags: generate returns `unavailable` with detail
     `"agy headless invocation not verified"` even when the binary is
     present; judge returns None (the protocol's unavailable form).
   - `ClaudeExecutor` — `claude`, family `anthropic`:
     `claude -p <prompt> --output-format json` (documented-only, see table).
   - `QwenExecutor` — `qwen-local`, family `alibaba`: `qwen -p <prompt>`
     (documented-only).
   - `KimiExecutor` — `kimi`, family `moonshot`: `kimi -p <prompt>`
     (documented-only).
   - `BeamExecutor` — `beam`, family `reflection`: `available()` ALWAYS
     returns `(False, "hosted via Nous portal; requires the operator's
     Nous login (hermes auth add nous); no local CLI")`, even if a `beam`
     binary appears on PATH; generate returns unavailable with that detail;
     judge returns None. Optional seat, never a requirement.
3. **`default_executors()`** returns all seven adapters keyed by expert_id:
   codex, grok, agy, claude, qwen-local, kimi, beam. Availability is each
   adapter's own report; nothing is faked.
4. **`LocalJudgeExecutor(Executor)`** — constructed with
   `(expert_id, family, style)`, style in
   `{"lenient", "strict", "evidence-first"}` (other styles raise
   `ValueError`). Its docstring states plainly that these are
   deterministic demo judges over packet text, not models. `judge()`:
   - parses `=== CANDIDATE X ===` sections and `Candidate X:` measured
     summary lines from the packet;
   - returns every rubric row (from the rubric's `items`, or `rows` if a
     v0.3 rubric uses that key) for every label;
   - `test_agreement`: 9.5 when the label's summary shows hard PASS +
     measured PASS, 9.0 on a PASS summary, **2.0 when the summary contains
     a FAIL** — so a dissent citing a missed measurement arises naturally;
   - `evidence`: 3 + 2 per `file:line`-shaped citation token
     (`\w+\.\w+:\d+`) in the label's diff, capped, with the evidence-first
     style weighting citations ±1 and the other styles ±0.5;
   - all other rows (question_fidelity/completeness/…): baseline 6,
     strict −1 / lenient +1 / evidence-first 0, plus a diff-size nudge;
   - confidence is per style: lenient 0.6, evidence-first 0.8, strict 0.9;
   - evidence strings point at packet content (e.g. `"packet: measured
     summary line for candidate A"`); rationales are single lines;
   - fully deterministic: two calls over the same packet return equal dicts.
   Its `generate()` returns unavailable (it is a judge, not a generator).
5. **`dev_solver.py` "balanced" strategy**: a third, distinct code shape —
   Python: `collections.deque(maxlen=capacity)`-backed ring with an
   explicit accumulation loop for the average; C#: `Queue<float>`-backed
   window with dequeue-on-overflow. Argparse choices and template selection
   updated for both languages. `LocalSolverExecutor` profile `"balanced"`
   maps to strategy `"balanced"`; the LESSON_KEYWORDS → ring
   lesson-conditioning is untouched and still wins for every profile
   (tested for the balanced profile too).

## CLI probes — exact commands and REAL outputs (this build environment)

```
$ for b in codex grok agy claude qwen kimi beam; do which $b; done
codex -> /opt/hatch-image/bin/codex
grok, agy, claude, qwen, kimi, beam -> NOT FOUND

$ agy --help    -> timeout: failed to run command 'agy': No such file or directory  [exit 127]
$ claude --help -> timeout: failed to run command 'claude': No such file or directory  [exit 127]
$ qwen --help   -> timeout: failed to run command 'qwen': No such file or directory  [exit 127]
$ kimi --help   -> timeout: failed to run command 'kimi': No such file or directory  [exit 127]
$ codex --help / codex exec --help -> ran (pre-existing adapter, unchanged;
  confirms `codex exec` non-interactive mode exists; not a W3 adapter)
```

### Adapter verification table

| Adapter | expert_id / family | Binary present here? | Command line help-verified here? | Behaviour when absent |
|---|---|---|---|---|
| AgyExecutor | agy / google | No | **No — not verifiable** (binary absent; decipher material documents no headless print mode) | available() False; generate unavailable `"agy headless invocation not verified"` (same detail even if the binary appears, until a headless mode is verified) |
| ClaudeExecutor | claude / anthropic | No | **No — documented-only**: `claude -p <prompt> --output-format json` | available() False via which(); generate unavailable; judge None |
| QwenExecutor | qwen-local / alibaba | No | **No — documented-only**: `qwen -p <prompt>` (cwd = worktree) | available() False via which(); generate unavailable; judge None |
| KimiExecutor | kimi / moonshot | No | **No — documented-only**: `kimi -p <prompt>` (cwd = worktree) | available() False via which(); generate unavailable; judge None |
| BeamExecutor | beam / reflection | n/a (no local CLI by design) | n/a | available() ALWAYS False with the Nous-portal detail |

Live availability snapshot in this environment (real output):

```
codex      openai     (True, '/opt/hatch-image/bin/codex')
grok       xai        (False, 'grok binary not found on PATH')
agy        google     (False, 'agy binary not found on PATH')
claude     anthropic  (False, 'claude binary not found on PATH')
qwen-local alibaba    (False, 'qwen binary not found on PATH')
kimi       moonshot   (False, 'kimi binary not found on PATH')
beam       reflection (False, "hosted via Nous portal; requires the operator's Nous login (hermes auth add nous); no local CLI")
```

## Tests

Extended `tests/test_executors.py` (19 new tests; 28 total in the file):
judge-v2 instruction (confidence/rationale/"one key per candidate label",
alias equality); per-adapter `available()` under monkeypatched
`shutil.which` (present and missing); generate-unavailable and judge-None
when missing; Agy's unverified detail even when the binary is "present";
Beam always unavailable with the exact Nous detail; `default_executors()`
seven ids + families; LocalJudgeExecutor (all rows for every label in a
packet built with the real `build_packet`, score/conf
...[truncated 3072 chars]