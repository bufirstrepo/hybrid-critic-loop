# BUILD-LOG-W5 — DeepSeek + Ollama-backed Qwen adapters (Hybrid Critic Loop v0.3)

Workstream W5. Owned files only (all work confined to these, plus this log):

- `hybrid_critic_loop/executors_api.py` (NEW)
- `tests/test_executors_api.py` (NEW)

Per the brief, the adapters live in a NEW module instead of
`hybrid_critic_loop/executors.py`, because W3 was concurrently editing
that file (agy/claude/qwen-binary/kimi/beam adapters, JUDGE v2,
LocalJudgeExecutor, dev_solver "balanced"). `executors.py` was read
only, never edited. **No commits made. No sibling files modified.**
`git status --porcelain` at the end of the cycle shows my footprint as
exactly the two untracked files above plus this log; every modified
tracked path in that listing (executors.py, test_executors.py,
state.py, contracts.py, tools/dev_solver.py, …) belongs to sibling
workstreams.

**Honest availability in THIS build environment (stated plainly):**
there is no Ollama server here and `DEEPSEEK_API_KEY` is not set, so
both new adapters report **unavailable** here. No live model call is
claimed anywhere below; all behaviour is proven against a local stub
HTTP server in the tests.

## What was implemented

Both adapters subclass `executors.Executor` and reuse `GenResult`,
`build_prompt`, `_extract_first_json_object` and `_parse_jsonl_usage`
from `.executors`. The JUDGE instruction is imported as
`JUDGE_INSTRUCTION_V2` when present (it is — `executors.py` defines it,
with `JUDGE_INSTRUCTION` as its alias) with a fallback import of
`JUDGE_INSTRUCTION`. Stdlib only (`urllib.request`/`urllib.error`);
no new dependencies.

Shared helpers inside the module:

- `_http_get_json` / `_http_post_json` — urllib wrappers returning
  `(obj, error)`; HTTP errors, URL errors, timeouts and bad JSON all
  become an error string, never an exception.
- `_extract_code_block` — first fenced code block in a model reply.
- `_write_target_file` — shared by both adapters' `generate()`: when
  the contract names `target_file`, the fenced block is written into
  the worktree at that path (parent dirs created; a target that would
  escape the worktree is refused with an error). No code block →
  error detail, file not written. No `target_file` → nothing written,
  the reply itself is the result (logged).
- `_judge_prompt` — packet + `RUBRIC:` JSON + JUDGE instruction,
  mirroring the existing CLI adapters.

### 1. OllamaQwenExecutor — expert_id `qwen-local`, family `alibaba`

- Base URL from env `OLLAMA_HOST`, default `http://127.0.0.1:11434`
  (module constant `OLLAMA_DEFAULT_HOST`).
- `available()`: HTTP GET `{host}/api/tags`, timeout 2s. Available iff
  the server responds AND a listed model's name starts with
  `qwen2.5-coder`; the exact tag found is recorded on `self.model_tag`
  and included in the detail (here it would be `qwen2.5-coder:14b`).
  Any connection error → `(False, "ollama not reachable at <host>")`,
  exactly, and never raises. Server reachable but no matching model →
  `(False, "ollama reachable at <host> but no qwen2.5-coder model listed (models: …)")`.
- `generate(worktree, contract, context)`: POST `{host}/api/generate`
  with `{model, prompt: build_prompt(contract, context), stream: false}`.
  Model = env `OLLAMA_MODEL` if set, else the recorded/found
  qwen2.5-coder tag, else the fallback `qwen2.5-coder:14b`. The reply
  text (`response` field) cannot edit files, so the shared helper
  extracts its fenced code block into the contract's `target_file`;
  extraction failure → status `"error"` with the detail. Tokens map
  `prompt_eval_count` → `tokens_in`, `eval_count` → `tokens_out`.
  `cost_usd` is always `None` (local, unreported — never invented).
  Any HTTP/URL error → `GenResult(status="error", …)`; an outer
  try/except guarantees no exception escapes `generate()`.
- `judge(packet, rubric)`: returns None when unavailable; otherwise
  POSTs `/api/generate` with the judge prompt and returns
  `_extract_first_json_object(response)`; any error → None.

### 2. DeepSeekExecutor — expert_id `deepseek`, family `deepseek`

- `available()`: `(True, "api key present")` iff env `DEEPSEEK_API_KEY`
  is set and non-empty (whitespace-only/empty does not count), else
  `(False, "DEEPSEEK_API_KEY not set")`. No network probe.
- `generate()`: unavailable → `GenResult(status="unavailable")` with
  no request made. Otherwise POST to the module constant
  `DEEPSEEK_API_URL` = `https://api.deepseek.com/chat/completions`
  (OpenAI-compatible), header `Authorization: Bearer <key>`, body
  `{model, messages: [{role: user, content: build_prompt(...)}], stream: false}`;
  model from env `DEEPSEEK_MODEL`, default `deepseek-chat`. Reply text
  is `choices[0].message.content`; the target file is extracted with
  the same shared helper as the Ollama adapter. Tokens map
  `usage.prompt_tokens`/`usage.completion_tokens` (with
  `_parse_jsonl_usage` over the serialised response as fallback);
  response `id` → `request_ids`. `cost_usd` stays `None`: the response
  carries no cost figure and no price table was invented to compute
  one. Any HTTP/URL error → status `"error"`, never an exception.
- `judge()`: same endpoint with the judge prompt; returns the first
  JSON object parsed from the reply content, or None on any
  error/unavailability.

### Integration note for the coordinator (flagged, not silently buried)

W3's CLI `QwenExecutor` in `executors.py` **also** uses expert_id
`qwen-local` / family `alibaba`. A panel dict keyed by expert_id can
hold only one of them: wiring `OllamaQwenExecutor` into
`default_executors()` under `qwen-local` replaces the CLI seat (which
is the owner's stated intent — the panel's Qwen seat is the local
Ollama model). The coordinator must make that replacement explicitly
at integration; this module's docstring says the same.

## Tests — `tests/test_executors_api.py` (19 tests)

A REAL local stub server (`http.server.ThreadingHTTPServer` in a
thread on an ephemeral port) implements:

- GET `/api/tags` → configurable model list;
- POST `/api/generate` → reply text (judge JSON when the prompt
  carries the RUBRIC block, else the canned code reply), with
  `prompt_eval_count: 123`, `eval_count: 45`;
- POST `/chat/completions` → OpenAI shape with
  `usage {prompt_tokens: 111, completion_tokens: 22}` and
  `id "chatcmpl-stub-1"`; requests (path, body, Authorization header)
  are recorded for assertions.

Coverage: Ollama available true with the model listed (tag recorded),
false when the model is absent, false with the exact
`ollama not reachable at <host>` detail and no exception when the port
is closed; generate writes the fenced code into
`src/speed_sampler.py` byte-exactly, reports 123/45 tokens, model and
`stream: false` seen by the stub, error (no file) when the reply has
no code block, error (no exception) when unreachable, completed with
no writes when the contract has no target_file; judge parses the
stub's JSON, None when unreachable. DeepSeek: available toggles on
env presence (unset → False, empty → False, set → True with the exact
spec detail strings); generate against the stub via monkeypatching
`executors_api.DEEPSEEK_API_URL` — file written, 111/22 tokens,
`cost_usd None`, request id recorded, stub saw model `deepseek-chat`
and the Bearer header; generate without a key is `unavailable` and
makes zero requests; closed endpoint → `error`, no exception; judge
parses the stub JSON and is None without a key. A final test class
asserts honest availability with NO stub: in this environment both
adapters report unavailable (it self-skips if the env ever genuinely
provides a server/key).

The stub API key used in tests is the literal dummy string
`test-key-not-real` — not a credential.

## Exact commands + REAL outputs (this session)

```
$ cd ~/workspace/hypercritic-build && python3 -m unittest tests.test_executors_api
...................
----------------------------------------------------------------------
Ran 19 tests in 0.915s

OK
```

(First run took 8.586s — the stub's `serve_forever` default 0.5s
shutdown poll per test; fixed by passing `poll_interval=0.05`, same
19/19 green at 0.915s.)

```
$ python3 -m py_compile hybrid_critic_loop/executors_api.py tests/test_executors_api.py
py_compile OK

$ which ollama
ollama binary: NOT FOUND

$ curl -s -m 2 http://127.0.0.1:11434/api/tags
curl 127.0.0.1:11434/api/tags: no response (exit 7)   # connection refused

$ python3 -c "<probe both adapters' available()>"
OLLAMA_HOST env: None
DEEPSEEK_API_KEY set: False
qwen-local   alibaba    available() -> (False, 'ollama not reachable at http://127.0.0.1:11434')
deepseek     deepseek   available() -> (False, 'DEEPSEEK_API_KEY not set')
```

Deletion-rule note (tier b): running the tests/`py_compile` created
regenerable `__pycache__` entries under `hybrid_critic_loop/` and
`tests/` (directories that already existed in the repo); nothing was
deleted in this cycle, and no tier-(c) deletion was needed or made.

## Principal Engineer checkup pass

Performed as a manual review pass on this change (the checkup
framework directory `/home/dino/Desktop/instructions/muse-code-checkup/`
is on Dino's PC and does not exist on this VM, and no
`tools/checkup_gate.py` exists in this repo — same situation W1/W3
recorded — so the formal gate could not be executed here; recorded as
a gap below, not papered over).

- **S0 Scope:** exactly the two owned new files + this log; the shared
  file under concurrent edit (`executors.py`) was read, not touched —
  verified via `git status` (my files are untracked-new, nothing else
  carries my edits).
- **Correctness:** every brief requirement traced to code + a test:
  env names and defaults (`OLLAMA_HOST`, `DEEPSEEK_API_KEY`,
  `DEEPSEEK_MODEL`, `deepseek-chat`, `qwen2.5-coder` prefix); 2s
  availability timeout; exact unavailable detail strings asserted
  verbatim; ollama token fields (`prompt_eval_count`/`eval_count`)
  asserted as 123/45 from the stub; DeepSeek usage fields asserted as
  111/22; JUDGE v2 symbol preferred with v1 fallback; first-JSON-object
  parsing reused, not re-implemented.
- **Honesty / no fabrication:** `cost_usd` is None on every path of
  both adapters (asserted in tests for the success paths); no price
  table exists in the module; availability in this environment is
  reported unavailable in the log with the probe output above; tests
  prove behaviour against stubs only and say so.
- **Failure behaviour:** closed-port tests for available/generate/
  judge on Ollama and generate on DeepSeek all pass without an
  exception escaping; HTTP error bodies are truncated into the detail,
  not raised.
- **Safety of writes:** `_write_target_file` resolves the destination
  and refuses a `target_file` escaping the worktree; writes happen
  only inside the caller-supplied worktree.
- **Determinism/speed:** suite is offline (loopback stub only) and
  runs in 0.915s; no sleeps except server shutdown joins.

Result: PASS (manual pass; formal gate not runnable on this VM — see
gaps).

## Gaps

- Formal Principal Engineer checkup gate not run: framework lives on
  Dino's PC, absent on this VM (same gap as W1/W3). Manual pass above
  stands in; the gate can be re-run on the PC if Dino wants it.
- `default_executors()` was NOT modified (per brief — the coordinator
  wires these adapters in at integration). Until then the panel does
  not include the DeepSeek seat, and the `qwen-local` expert_id
  collision with W3's CLI `QwenExecutor` (flagged above) must be
  resolved by that wiring.
- No live verification against a real Ollama server or the real
  DeepSeek API was possible in this environment (neither exists/is
  keyed here, by the brief's own premise). The Ollama request/response
  shapes and the DeepSeek chat-completions shape are implemented from
  their documented API shapes and proven against a stub implementing
  those shapes; a first live run on the owner's machine (where ollama
  serves `qwen2.5-coder:14b`) remains the closing artifact for live
  behaviour.
- Full test suite not run: sibling workstreams (W1/W3 and others) are
  concurrently editing the repo, so a whole-suite result would not be
  attributable to this change. Only `tests.test_executors_api` was
  executed — it imports `hybrid_critic_loop.executors` (read-only) and
  its own module, and it is fully green.
- Not committed, per brief.
