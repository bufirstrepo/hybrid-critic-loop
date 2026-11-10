"""Executors for the Hybrid Critic Loop (stdlib only)."""
from __future__ import annotations

import json
import math
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .shellrun import run_command

LESSON_KEYWORDS = ["ops_per_tick", "alloc", "hot-loop", "hot loop", "pre-allocate", "ring"]

JUDGE_INSTRUCTION_V2 = (
    'Return ONLY a JSON object with one key per candidate label shown in the '
    'packet (for example {"A": {"items": {...}}, "B": {"items": {...}}}), '
    'where each candidate maps to {"items": {<rubric row>: '
    '{"score": <0-10>, "confidence": <0-1>, '
    '"evidence": "<pointer to the packet evidence for that row>", '
    '"rationale": "<one line>"}}}. '
    'Score every rubric row for every candidate label shown in the packet.'
)

# Backwards-compatible alias: v0.2 code/tests referenced JUDGE_INSTRUCTION.
JUDGE_INSTRUCTION = JUDGE_INSTRUCTION_V2


@dataclass
class GenResult:
    status: str  # "completed"|"refused"|"timeout"|"unavailable"|"error"
    log: str = ""
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    request_ids: list[str] = field(default_factory=list)
    duration_ms: int = 0
    detail: str = ""
    # What produced the artifact: "generated" for a model's or program's own
    # output. The loop writes it to the candidate row, where the provenance
    # gate reads it (fix pass Wave 2.10, review 1D-18).
    provenance: str = "generated"


def build_prompt(contract: dict, context: str) -> str:
    contract = contract or {}
    task = contract.get("task") or contract.get("description") or contract.get("title") or ""
    parts: list[str] = []
    parts.append("TASK:")
    parts.append(str(task) if task else json.dumps(contract, indent=2, default=str))
    # Acceptance summary
    parts.append("")
    parts.append("ACCEPTANCE:")
    acceptance = contract.get("acceptance") if isinstance(contract, dict) else None
    if acceptance is not None:
        parts.append(json.dumps(acceptance, indent=2, default=str) if not isinstance(acceptance, str) else acceptance)
    # Commands + metrics explicitly (wherever they live)
    commands = None
    metrics = None
    if isinstance(contract, dict):
        commands = contract.get("commands") or contract.get("acceptance_commands")
        metrics = contract.get("metrics")
        if acceptance and isinstance(acceptance, dict):
            commands = commands or acceptance.get("commands")
            metrics = metrics or acceptance.get("metrics")
    if commands is not None:
        parts.append(f"Commands: {json.dumps(commands, default=str) if not isinstance(commands, str) else commands}")
    if metrics is not None:
        parts.append(f"Metrics: {json.dumps(metrics, default=str) if not isinstance(metrics, str) else metrics}")
    if acceptance is None and commands is None and metrics is None:
        # Fallback: dump remaining contract keys for context
        extra = {k: v for k, v in contract.items() if k not in ("task", "description", "title")}
        if extra:
            parts.append(json.dumps(extra, indent=2, default=str))
    if context:
        parts.append("")
        parts.append("LESSONS FROM PRIOR RUNS")
        parts.append(context)
    parts.append("")
    parts.append("Implement in the current directory only. Do not modify files outside the current directory.")
    return "\n".join(parts)


def _clean_token_count(value) -> int | None:
    """A reported token count, or None ("not reported").

    Fix pass round 4 (spec G6, round-4 security F5 / round-3 regression
    F4): a count is a non-negative integer. Negative, NaN/Inf, fractional,
    boolean, or non-numeric values are not reported numbers — they become
    None at this walker, never a value the ledger must reject mid-run.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        value = int(value)
    if value < 0:
        return None
    return int(value)


def _clean_cost(value) -> float | None:
    """A reported USD cost, or None: finite, non-negative, numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0:
        return None
    return result


def _parse_jsonl_usage(stdout: str):
    """Token usage and request ids from a CLI's JSON output. Never raises.

    Whole JSON only (fix pass Wave 2.9, review M8 / 1D-06): one JSON
    document (claude's ``--output-format json`` envelope) or JSONL event
    lines (codex ``--json``). A line that is not entirely JSON — model
    prose quoting a usage-shaped object — counts for nothing; a token
    count is a reported number or None, never a guess (invariant 5).

    Envelope only (fix pass round 4, spec G6, round-4 security F5): the
    only objects read are the CLI's own output objects themselves — the
    whole document, or each JSONL line's object, or (for a top-level
    JSON array) its elements. A ``usage`` / ``total_cost_usd`` key counts
    only at THAT level; anything nested deeper (a ``detail`` / ``result``
    payload the model itself authored) is ignored, so model text cannot
    manufacture the ledger's numbers. Per field, a later envelope's
    value replaces an earlier one's (the final usage line of a stream
    is the cumulative one); an invalid value at the envelope is "not
    reported" and never displaces a valid earlier report. Negative,
    NaN/Inf, or fractional token/cost values likewise become None here
    (round-3 regression F4) instead of detonating the ledger write
    downstream.
    """
    tokens_in = None
    tokens_out = None
    request_ids: list[str] = []
    cost = None
    if not stdout:
        return tokens_in, tokens_out, request_ids, cost
    objects: list = []
    try:
        whole = json.loads(stdout)
    except Exception:
        whole = None
    if isinstance(whole, dict):
        objects.append(whole)
    elif isinstance(whole, list):
        objects.extend(item for item in whole if isinstance(item, dict))
    else:
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                objects.append(obj)
    for obj in objects:
        # The envelope object itself — never anything nested inside it.
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else None
        if usage is not None:
            cleaned = _clean_token_count(usage.get("input_tokens"))
            if cleaned is not None:
                tokens_in = cleaned
            cleaned = _clean_token_count(usage.get("output_tokens"))
            if cleaned is not None:
                tokens_out = cleaned
            if tokens_in is None:
                cleaned = _clean_token_count(usage.get("prompt_tokens"))
                if cleaned is not None:
                    tokens_in = cleaned
            if tokens_out is None:
                cleaned = _clean_token_count(usage.get("completion_tokens"))
                if cleaned is not None:
                    tokens_out = cleaned
        # Provider-reported cost only (fix pass Wave 2.10, review
        # 1D-16): claude's envelope carries total_cost_usd. Never a
        # price table.
        for key in ("total_cost_usd", "cost_usd"):
            cleaned = _clean_cost(obj.get(key))
            if cleaned is not None:
                cost = cleaned
                break
        for key in ("request_id", "requestId"):
            if obj.get(key):
                rid = str(obj[key])
                if rid not in request_ids:
                    request_ids.append(rid)
        # id in error/event objects
        if obj.get("type") in ("error", "event") or "error" in obj:
            if obj.get("id"):
                rid = str(obj["id"])
                if rid not in request_ids:
                    request_ids.append(rid)
    return tokens_in, tokens_out, request_ids, cost


def _extract_first_json_object(text: str):
    if not text:
        return None
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
    return None


def _looks_like_verdict(obj) -> bool:
    """A judge verdict maps candidate labels to {"items": {...}} blocks."""
    return (
        isinstance(obj, dict) and bool(obj)
        and all(isinstance(v, dict) and "items" in v for v in obj.values())
    )


class _DuplicateKeyError(ValueError):
    """A JSON object in a judge reply carried the same key twice."""


def _object_without_duplicate_keys(pairs):
    """object_pairs_hook (fix pass round 4, spec G6, round-4 security F3):
    a verdict object with duplicate keys is ambiguous — stdlib json would
    silently collapse them last-wins, letting one value show to a reader
    and another to the parser — so any object carrying a duplicate key
    fails to decode in the judge path and is never selected."""
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateKeyError(f"duplicate key {key!r}")
        obj[key] = value
    return obj


_JUDGE_DECODER = json.JSONDecoder(object_pairs_hook=_object_without_duplicate_keys)

_FENCED_BLOCK_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)


def _iter_json_objects(text: str):
    """Yield every JSON object decodable from ``text``, scanning from each '{'.

    Decoded with the duplicate-key-rejecting hook, so an object carrying
    a duplicate key raises inside raw_decode and is skipped here like
    any other undecodable span.
    """
    decoder = _JUDGE_DECODER
    i, n = 0, len(text)
    while i < n:
        j = text.find("{", i)
        if j < 0:
            return
        try:
            obj, end = decoder.raw_decode(text[j:])
        except Exception:
            i = j + 1
            continue
        if isinstance(obj, dict):
            yield obj
        i = j + max(end, 1)


def _verdict_in_text(text: str, _depth: int):
    """The LAST verdict-shaped object in ``text``, or None.

    Scans every decodable object in order — plain verdicts and verdicts
    carried in a wrapper's text-bearing fields (or a codex event's
    ``item`` text) all count, at the position where they appear — and
    keeps the last one: a judge's conclusion comes after anything it
    quoted or drafted along the way (round-4 security F3).
    """
    found = None
    for obj in _iter_json_objects(text):
        candidate = None
        if _looks_like_verdict(obj):
            candidate = obj
        else:
            for key in ("text", "result", "content", "response", "output"):
                inner = obj.get(key)
                if isinstance(inner, str) and "{" in inner:
                    inner_found = _extract_judge_verdict(inner, _depth + 1)
                    if inner_found is not None:
                        candidate = inner_found
            item = obj.get("item")
            if isinstance(item, dict):
                inner = item.get("text") or item.get("content")
                if isinstance(inner, str) and "{" in inner:
                    inner_found = _extract_judge_verdict(inner, _depth + 1)
                    if inner_found is not None:
                        candidate = inner_found
        if candidate is not None:
            found = candidate
    return found


def _extract_judge_verdict(text: str, _depth: int = 0):
    """The model's verdict from any judge output (fix pass Wave 2.5, review H2).

    Anchored to the END of the reply (fix pass round 4, spec G6, round-4
    security F3): a fenced block carrying a verdict is the reply's named
    answer and wins; otherwise the LAST verdict-shaped object in the text
    is the verdict — an early object the judge quoted, drafted, or was
    steered to emit no longer beats its own conclusion. Verdict sources:
    plain model text; a codex ``--json`` JSONL stream (the verdict sits
    in ``item.completed`` events' ``agent_message`` text); a claude
    ``--output-format json`` envelope (the verdict sits in ``result``);
    or any wrapper whose text-bearing field carries it. An object with
    duplicate keys never decodes, so it is never the verdict. None when
    absent — the matrix then records no cell for that peer, never a
    wrapper object.
    """
    if not text or _depth > 3:
        return None
    fenced = None
    for match in _FENCED_BLOCK_RE.finditer(text):
        block_found = _verdict_in_text(match.group(1), _depth)
        if block_found is not None:
            fenced = block_found
    if fenced is not None:
        return fenced
    return _verdict_in_text(text, _depth)


def _find_refusal_line(combined: str) -> str | None:
    markers = ["Not logged in", "Reconnecting", "error sending request"]
    for line in combined.splitlines():
        for m in markers:
            if m in line:
                return line.strip()
    return None


def _cli_result(binary_label: str, result, log: str, tokens_in, tokens_out, cost,
                request_ids) -> GenResult:
    """Classify a CLI seat's run (fix pass Wave 2.9, review M8 / 1D-05, 1D-09).

    The exit code decides: 0 is ``completed`` (a refusal marker in the
    output is noted in the detail, never decisive); a non-zero exit with a
    marker is ``refused``, without one ``error``; a run the runner could not
    start (``exit_code is None``: E2BIG, a vanished binary) is ``error``
    carrying the runner's own reason; a timeout is ``timeout`` — unless the
    output carries a refusal marker, in which case the seat refused and
    then hung. One decision for every CLI adapter.
    """
    refusal = _find_refusal_line(log)
    common = dict(log=log, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost,
                  request_ids=request_ids, duration_ms=result.duration_ms)
    if result.timed_out:
        if refusal:
            return GenResult(status="refused",
                             detail=f"{refusal}; process then hung and was killed at the timeout",
                             **common)
        return GenResult(status="timeout", detail=f"{binary_label} timed out", **common)
    if result.exit_code == 0:
        detail = f"completed; output mentions {refusal!r}" if refusal else ""
        return GenResult(status="completed", detail=detail, **common)
    if result.exit_code is None:
        lines = (result.stderr or "").strip().splitlines()
        reason = lines[0] if lines else "the command could not be started"
        return GenResult(status="error", detail=f"{binary_label} could not run: {reason}", **common)
    if refusal:
        return GenResult(status="refused", detail=refusal, **common)
    return GenResult(status="error", detail=f"{binary_label} exited {result.exit_code}", **common)


class Executor:
    expert_id: str = "base"
    family: str = "base"

    def available(self) -> tuple[bool, str]:
        return (False, "base executor not available")

    def can_judge(self) -> bool:
        """Whether this seat can score peers' candidates. Default: the
        adapter overrides ``judge``. Seats whose ``judge`` is a declared
        None (Agy, Beam, the local solvers) say False explicitly, so the
        loop never seats them as matrix peers (fix pass Wave 2.5)."""
        return type(self).judge is not Executor.judge

    def generate(self, worktree: Path, contract: dict, context: str) -> GenResult:
        return GenResult(status="unavailable", detail="base executor", log="")

    def judge(self, packet: str, rubric: dict) -> dict | None:
        return None


class CodexExecutor(Executor):
    expert_id = "codex"
    family = "openai"

    def available(self) -> tuple[bool, str]:
        path = shutil.which("codex")
        if path:
            return (True, path)
        return (False, "codex binary not found on PATH")

    def generate(self, worktree: Path, contract: dict, context: str) -> GenResult:
        ok, detail_avail = self.available()
        if not ok:
            return GenResult(status="unavailable", log="", detail=detail_avail, duration_ms=0)
        prompt = build_prompt(contract, context)
        # codex exec --sandbox workspace-write --cd <worktree> --json <prompt>
        cmd = f"codex exec --sandbox workspace-write --cd {shlex.quote(str(worktree))} --json {shlex.quote(prompt)}"
        result = run_command(cmd, cwd=worktree, timeout_s=300)
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        log = combined
        tokens_in, tokens_out, request_ids, cost = _parse_jsonl_usage(result.stdout or "")
        # Also scan stderr for ids if none
        if not request_ids:
            _, _, rids2, _ = _parse_jsonl_usage(result.stderr or "")
            request_ids = rids2
        return _cli_result("codex", result, log, tokens_in, tokens_out, cost, request_ids)

    def judge(self, packet: str, rubric: dict) -> dict | None:
        ok, _ = self.available()
        if not ok:
            return None
        prompt = packet + "\n\nRUBRIC:\n" + json.dumps(rubric, indent=2, default=str) + "\n\n" + JUDGE_INSTRUCTION_V2
        with tempfile.TemporaryDirectory() as td:
            cmd = f"codex exec --sandbox read-only --cd {shlex.quote(td)} --json {shlex.quote(prompt)}"
            try:
                result = run_command(cmd, cwd=Path(td), timeout_s=300)
            except Exception:
                return None
            if result.timed_out:
                return None
            parsed = _extract_judge_verdict(result.stdout or "")
            return parsed


class GrokExecutor(Executor):
    expert_id = "grok"
    family = "xai"

    def available(self) -> tuple[bool, str]:
        path = shutil.which("grok")
        if path:
            return (True, path)
        return (False, "grok binary not found on PATH")

    def generate(self, worktree: Path, contract: dict, context: str) -> GenResult:
        ok, _ = self.available()
        if not ok:
            return GenResult(status="unavailable", detail="grok binary not found on PATH", log="", duration_ms=0)
        from .file_edits import apply_reply_edits, build_prompt_with_files, contract_target_files

        targets = contract_target_files(contract)
        before_snapshots = _snapshot_targets(Path(worktree), targets) if targets else {}
        prompt = build_prompt_with_files(Path(worktree), contract, context) if targets else build_prompt(contract, context)
        cmd = f"grok -p {shlex.quote(prompt)} --cwd {shlex.quote(str(worktree))}"
        result = run_command(cmd, cwd=worktree, timeout_s=300)
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        tokens_in, tokens_out, request_ids, cost = _parse_jsonl_usage(result.stdout or "")
        gen = _cli_result("grok", result, combined, tokens_in, tokens_out, cost, request_ids)
        if gen.status == "completed" and targets and _targets_unchanged(Path(worktree), before_snapshots):
            _apply_cli_stdout_edits(Path(worktree), contract, result.stdout or "", gen)
        return gen

    def judge(self, packet: str, rubric: dict) -> dict | None:
        ok, _ = self.available()
        if not ok:
            return None
        prompt = packet + "\n\nRUBRIC:\n" + json.dumps(rubric, indent=2, default=str) + "\n\n" + JUDGE_INSTRUCTION_V2
        with tempfile.TemporaryDirectory() as td:
            cmd = f"grok -p {shlex.quote(prompt)} --cwd {shlex.quote(td)}"
            try:
                result = run_command(cmd, cwd=Path(td), timeout_s=300)
            except Exception:
                return None
            if result.timed_out:
                return None
            return _extract_judge_verdict(result.stdout or "")


AGY_UNVERIFIED_DETAIL = "agy headless invocation not verified"

BEAM_DETAIL = (
    "hosted via Nous portal; requires the operator's Nous login "
    "(hermes auth add nous); no local CLI"
)


def _snapshot_targets(worktree: Path, targets: list[str]) -> dict[str, bytes | None]:
    from .file_edits import resolve_confined_path

    snap: dict[str, bytes | None] = {}
    for rel in targets:
        p = resolve_confined_path(worktree, rel)
        if p is not None and p.is_file():
            try:
                snap[rel] = p.read_bytes()
            except Exception:
                snap[rel] = None
        else:
            snap[rel] = None
    return snap


def _targets_unchanged(worktree: Path, before: dict[str, bytes | None]) -> bool:
    return _snapshot_targets(worktree, list(before.keys())) == before


def _unwrap_cli_text(stdout: str) -> str:
    """Extract model text from a JSON envelope (e.g. claude --output-format json) or return stdout."""
    if not stdout:
        return ""
    try:
        obj = json.loads(stdout)
        if isinstance(obj, dict):
            for k in ("result", "text", "content", "response"):
                val = obj.get(k)
                if isinstance(val, str) and val.strip():
                    return val
    except Exception:
        pass
    return stdout


def _apply_cli_stdout_edits(worktree: Path, contract: dict, stdout: str, gen: GenResult) -> None:
    from .file_edits import apply_reply_edits

    reply_text = _unwrap_cli_text(stdout)
    handled, detail = apply_reply_edits(worktree, contract, reply_text)
    if handled and not detail.startswith("error: "):
        gen.detail = f"{gen.detail}; {detail}".strip("; ")


def _cli_generate(binary_label: str, cmd: str, worktree: Path) -> tuple[GenResult, str]:
    """Shared generate() body for simple ``<cli> -p <prompt>`` adapters.

    Mirrors the Codex/Grok adapters: JSON usage parse, then the one
    classification in ``_cli_result`` (exit code decides; markers explain).
    """
    result = run_command(cmd, cwd=worktree, timeout_s=300)
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    tokens_in, tokens_out, request_ids, cost = _parse_jsonl_usage(result.stdout or "")
    if not request_ids:
        _, _, rids2, _ = _parse_jsonl_usage(result.stderr or "")
        request_ids = rids2
    return _cli_result(binary_label, result, combined, tokens_in, tokens_out, cost, request_ids), (result.stdout or "")


def _cli_judge(cmd: str, cwd: Path) -> dict | None:
    """Shared judge() body: run a judge command, extract its verdict."""
    try:
        result = run_command(cmd, cwd=cwd, timeout_s=300)
    except Exception:
        return None
    if result.timed_out:
        return None
    return _extract_judge_verdict(result.stdout or "")


class _PromptCliExecutor(Executor):
    """Base for adapters whose CLI takes the prompt as one argument.

    Subclasses set ``binary`` and ``_prompt_args(prompt)`` returning the
    argument tail after the binary name. Command-line shapes for the
    adapters below are documented-only unless BUILD-LOG-W3.md records a
    help-verified probe in the build environment; ``available()`` is
    always an honest ``shutil.which`` check, and an absent binary never
    reaches the command at all.
    """

    binary: str = ""

    def _prompt_args(self, prompt: str) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def available(self) -> tuple[bool, str]:
        path = shutil.which(self.binary)
        if path:
            return (True, path)
        return (False, f"{self.binary} binary not found on PATH")

    def generate(self, worktree: Path, contract: dict, context: str) -> GenResult:
        ok, detail_avail = self.available()
        if not ok:
            return GenResult(status="unavailable", log="", detail=detail_avail, duration_ms=0)
        from .file_edits import build_prompt_with_files, contract_target_files

        targets = contract_target_files(contract)
        before_snapshots = _snapshot_targets(Path(worktree), targets) if targets else {}
        prompt = build_prompt_with_files(Path(worktree), contract, context) if targets else build_prompt(contract, context)
        cmd = f"{self.binary} {self._prompt_args(prompt)}"
        gen, stdout = _cli_generate(self.binary, cmd, worktree)
        if gen.status == "completed" and targets and _targets_unchanged(Path(worktree), before_snapshots):
            _apply_cli_stdout_edits(Path(worktree), contract, stdout, gen)
        return gen

    def judge(self, packet: str, rubric: dict) -> dict | None:
        ok, _ = self.available()
        if not ok:
            return None
        prompt = packet + "\n\nRUBRIC:\n" + json.dumps(rubric, indent=2, default=str) + "\n\n" + JUDGE_INSTRUCTION_V2
        with tempfile.TemporaryDirectory() as td:
            cmd = f"{self.binary} {self._prompt_args(prompt)}"
            return _cli_judge(cmd, Path(td))


class AgyExecutor(Executor):
    """Antigravity CLI (the Gemini seat) adapter.

    Honesty over coverage: a non-interactive/headless invocation of
    ``agy`` was NOT verified in the build environment (binary absent,
    and the decipher material does not document a headless print mode).
    ``available()`` reports the honest ``shutil.which`` result, but
    generate/judge never invent flags: they report unavailable with
    AGY_UNVERIFIED_DETAIL until a headless mode is verified.
    """

    expert_id = "agy"
    family = "google"
    binary = "agy"

    def available(self) -> tuple[bool, str]:
        path = shutil.which(self.binary)
        if path:
            return (True, path)
        return (False, "agy binary not found on PATH")

    def generate(self, worktree: Path, contract: dict, context: str) -> GenResult:
        return GenResult(status="unavailable", log="", detail=AGY_UNVERIFIED_DETAIL, duration_ms=0)

    def can_judge(self) -> bool:
        return False  # a declared None, not a judge

    def judge(self, packet: str, rubric: dict) -> dict | None:
        # judge()'s unavailable form is None (see CodexExecutor/GrokExecutor).
        return None


class ClaudeExecutor(_PromptCliExecutor):
    """Claude Code CLI adapter (documented-only command shape).

    Headless print mode ``claude -p <prompt>`` with
    ``--output-format json`` (usage lands in the JSON envelope, which
    _parse_jsonl_usage walks). Not help-verified in the build
    environment: the binary was absent there (see BUILD-LOG-W3.md).
    """

    expert_id = "claude"
    family = "anthropic"
    binary = "claude"

    def _prompt_args(self, prompt: str) -> str:
        return f"-p {shlex.quote(prompt)} --output-format json"


class QwenExecutor(_PromptCliExecutor):
    """Qwen Code CLI adapter (documented-only command shape).

    ``qwen -p <prompt>`` non-interactive shape, working directory taken
    from the process cwd. Not help-verified in the build environment:
    the binary was absent there (see BUILD-LOG-W3.md).

    Expert id is ``qwen-cli``: the ``qwen-local`` seat belongs to the
    Ollama-backed adapter (owner's local ollama serves
    qwen2.5-coder:14b; see executors_api.py). Both are family alibaba.
    """

    expert_id = "qwen-cli"
    family = "alibaba"
    binary = "qwen"

    def _prompt_args(self, prompt: str) -> str:
        return f"-p {shlex.quote(prompt)}"


class KimiExecutor(_PromptCliExecutor):
    """Kimi CLI adapter (documented-only command shape).

    ``kimi -p <prompt>`` non-interactive shape, working directory taken
    from the process cwd. Not help-verified in the build environment:
    the binary was absent there (see BUILD-LOG-W3.md).
    """

    expert_id = "kimi"
    family = "moonshot"
    binary = "kimi"

    def _prompt_args(self, prompt: str) -> str:
        return f"-p {shlex.quote(prompt)}"


class BeamExecutor(Executor):
    """Beam (Reflection), hosted via the Nous portal.

    There is no local CLI in this build, by design: Beam is an optional
    seat and the entry must not depend on it. ``available()`` is always
    False with the Nous detail, whatever is on PATH.
    """

    expert_id = "beam"
    family = "reflection"

    def available(self) -> tuple[bool, str]:
        return (False, BEAM_DETAIL)

    def can_judge(self) -> bool:
        return False  # no adapter path in this build

    def generate(self, worktree: Path, contract: dict, context: str) -> GenResult:
        return GenResult(status="unavailable", log="", detail=BEAM_DETAIL, duration_ms=0)

    def judge(self, packet: str, rubric: dict) -> dict | None:
        return None


def _find_dev_solver() -> Path:
    here = Path(__file__).resolve()
    # Spec text says parents[2]; actual layout is parents[1] == repo root.
    # Search upward robustly so either layout works.
    for parent in here.parents:
        candidate = parent / "tools" / "dev_solver.py"
        if candidate.exists():
            return candidate
    # Fallback to spec-literal path even if it doesn't exist yet
    try:
        return here.parents[2] / "tools" / "dev_solver.py"
    except Exception:
        return here.parents[1] / "tools" / "dev_solver.py"


class LocalSolverExecutor(Executor):
    family = "local"

    def __init__(self, expert_id: str, profile: str):
        self.expert_id = expert_id
        self.profile = profile

    def available(self) -> tuple[bool, str]:
        return (True, "local solver")

    def generate(self, worktree: Path, contract: dict, context: str) -> GenResult:
        context = context or ""
        lowered = context.lower()
        matched = [kw for kw in LESSON_KEYWORDS if kw.lower() in lowered]
        lesson_conditioned = bool(matched)
        if lesson_conditioned:
            strategy = "ring"
            detail = f"strategy=ring; lesson-conditioned: context matched {matched}; profile={self.profile}"
        elif self.profile == "ring":
            strategy = "ring"
            detail = f"strategy=ring; profile default (ring); no lesson trigger"
        elif self.profile == "balanced":
            strategy = "balanced"
            detail = f"strategy=balanced; profile default (balanced); no lesson trigger"
        else:
            strategy = "naive"
            detail = f"strategy=naive; profile default (naive); no lesson trigger"

        solver_path = _find_dev_solver()
        start = time.monotonic()
        payload = json.dumps({"contract": contract or {}, "context": context})
        try:
            proc = subprocess.run(
                [sys.executable, str(solver_path), "--strategy", strategy],
                cwd=str(worktree),
                input=payload,
                capture_output=True,
                text=True,
                timeout=120,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            log = (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode == 0:
                return GenResult(status="completed", log=log, tokens_in=None, tokens_out=None, cost_usd=None, request_ids=[], duration_ms=duration_ms, detail=detail)
            return GenResult(status="error", log=log, tokens_in=None, tokens_out=None, cost_usd=None, request_ids=[], duration_ms=duration_ms, detail=f"{detail}; dev_solver exited {proc.returncode}")
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log = ""
            if exc.stdout:
                log += exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout)
            if exc.stderr:
                log += exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr)
            return GenResult(status="timeout", log=log, duration_ms=duration_ms, detail=f"{detail}; dev_solver timed out")
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return GenResult(status="error", log=str(exc), duration_ms=duration_ms, detail=f"{detail}; {exc}")

    def can_judge(self) -> bool:
        return False  # the local solver family never judges

    def judge(self, packet: str, rubric: dict) -> dict | None:
        return None


_CANDIDATE_SECTION_RE = re.compile(r"^=== CANDIDATE ([A-Za-z0-9]+) ===\s*$", re.MULTILINE)
_SUMMARY_LINE_RE = re.compile(r"^Candidate ([A-Za-z0-9]+):\s*(.*)$", re.MULTILINE)
_CITATION_RE = re.compile(r"\w+\.\w+:\d+")

_LOCAL_JUDGE_STYLES = ("lenient", "strict", "evidence-first")
_LOCAL_JUDGE_CONFIDENCE = {"lenient": 0.6, "strict": 0.9, "evidence-first": 0.8}
_LOCAL_JUDGE_BASELINE_ADJ = {"lenient": 1.0, "strict": -1.0, "evidence-first": 0.0}


def _rubric_rows(rubric: dict) -> list[str]:
    """Row names from a rubric dict: 'items' (v0.2) or 'rows' (v0.3)."""
    if not isinstance(rubric, dict):
        return []
    raw = rubric.get("items")
    if raw is None:
        raw = rubric.get("rows")
    rows: list[str] = []
    for entry in raw or []:
        if isinstance(entry, str):
            rows.append(entry)
        elif isinstance(entry, dict):
            name = entry.get("name") or entry.get("id") or entry.get("row")
            if name:
                rows.append(str(name))
    return rows


def _clamp_score(value: float) -> float:
    return max(0.0, min(10.0, float(value)))


class LocalJudgeExecutor(Executor):
    """Deterministic local demo judge over packet text — NOT a model.

    This executor exists so the v0.3 panel demo can run fully offline:
    it parses the blinded packet (``=== CANDIDATE X ===`` sections plus
    the measured-summary lines) and derives pseudo-scores only from
    observable packet content — the candidate's measured PASS/FAIL
    summary, file:line-shaped citation tokens in its diff, and diff
    size. Two calls over the same packet always return the same scores.
    It never calls a model or a network; do not mistake its scores for
    a real judge's.
    """

    def __init__(self, expert_id: str, family: str, style: str):
        if style not in _LOCAL_JUDGE_STYLES:
            raise ValueError(
                f"unknown local judge style {style!r}; expected one of {_LOCAL_JUDGE_STYLES}"
            )
        self.expert_id = expert_id
        self.family = family
        self.style = style

    def available(self) -> tuple[bool, str]:
        return (True, "local deterministic demo judge (packet-text scoring, not a model)")

    def generate(self, worktree: Path, contract: dict, context: str) -> GenResult:
        return GenResult(
            status="unavailable",
            log="",
            detail=f"local judge {self.expert_id} does not generate candidates",
            duration_ms=0,
        )

    # -- packet parsing -------------------------------------------------

    @staticmethod
    def _parse_packet(packet: str) -> tuple[list[str], dict[str, str], dict[str, str]]:
        packet = packet or ""
        labels: list[str] = []
        sections: dict[str, str] = {}
        matches = list(_CANDIDATE_SECTION_RE.finditer(packet))
        for i, match in enumerate(matches):
            label = match.group(1)
            end = matches[i + 1].start() if i + 1 < len(matches) else len(packet)
            if label not in sections:
                labels.append(label)
            sections[label] = packet[match.end():end]
        summaries: dict[str, str] = {}
        for match in _SUMMARY_LINE_RE.finditer(packet):
            summaries.setdefault(match.group(1), match.group(2))
            if match.group(1) not in labels and match.group(1) not in sections:
                labels.append(match.group(1))
                sections[match.group(1)] = ""
        return labels, sections, summaries

    # -- scoring --------------------------------------------------------

    def _score_row(self, row: str, label: str, section: str, summary: str):
        """Return (score, evidence, rationale) for one rubric row."""
        name = row.lower()
        confidence = _LOCAL_JUDGE_CONFIDENCE[self.style]
        adj = _LOCAL_JUDGE_BASELINE_ADJ[self.style]
        if "test_agreement" in name or name in ("tests", "test", "measurement_agreement"):
            low = (summary or "").lower()
            evidence = f"packet: measured summary line for candidate {label}"
            if "fail" in low:
                return 2.0, evidence, "Measured summary shows a FAIL for this candidate, so agreement with the tests is low."
            if "hard gates pass" in low and "measured gates pass" in low:
                return 9.5, evidence, "Measured summary shows hard PASS and measured PASS for this candidate."
            if "pass" in low:
                return 9.0, evidence, "Measured summary shows PASS for this candidate."
            return _clamp_score(5.0 + adj), evidence, "No measured PASS/FAIL summary line for this candidate in the packet."
        citations = len(_CITATION_RE.findall(section or ""))
        if "evidence" in name:
            score = 3.0 + min(6.0, 2.0 * citations)
            if self.style == "evidence-first":
                score += 1.0 if citations else -1.0
            else:
                score += adj * 0.5
            evidence = f"packet: candidate {label} section ({citations} citation-shaped tokens)"
            rationale = f"Diff for this candidate carries {citations} file:line-shaped citation(s)."
            return _clamp_score(score), evidence, rationale
        # question_fidelity / completeness / any other row: mid-range
        # baseline varied by style, nudged by how much diff is present.
        size = len(section or "")
        size_adj = 1.0 if size >= 400 else (0.0 if size >= 100 else -1.0)
        score = _clamp_score(6.0 + adj + size_adj)
        evidence = f"packet: candidate {label} section ({size} chars of diff)"
        rationale = f"{row} scored from packet content at the {self.style} baseline."
        return score, evidence, rationale

    def judge(self, packet: str, rubric: dict) -> dict | None:
        labels, sections, summaries = self._parse_packet(packet)
        rows = _rubric_rows(rubric)
        if not labels or not rows:
            return None
        confidence = _LOCAL_JUDGE_CONFIDENCE[self.style]
        result: dict = {}
        for label in labels:
            items: dict = {}
            for row in rows:
                score, evidence, rationale = self._score_row(
                    row, label, sections.get(label, ""), summaries.get(label, "")
                )
                items[row] = {
                    "score": score,
                    "confidence": confidence,
                    "evidence": evidence,
                    "rationale": rationale,
                }
            result[label] = {"items": items}
        return result


def default_executors() -> dict[str, Executor]:
    """All panel adapters keyed by expert_id.

    Availability is each adapter's own report — nothing is faked here:
    absent CLIs report unavailable, Agy reports its headless mode
    unverified, and Beam always reports its Nous-portal detail.

    The alibaba seat (``qwen-local``) is the Ollama-backed adapter —
    the owner's local ollama serves qwen2.5-coder:14b. The qwen CLI
    adapter stays available under ``qwen-cli`` as an alternate seat for
    the same family.
    """
    from .executors_api import DeepSeekExecutor, OllamaQwenExecutor

    adapters: list[Executor] = [
        CodexExecutor(),
        GrokExecutor(),
        AgyExecutor(),
        ClaudeExecutor(),
        OllamaQwenExecutor(),
        QwenExecutor(),
        KimiExecutor(),
        DeepSeekExecutor(),
        BeamExecutor(),
    ]
    return {adapter.expert_id: adapter for adapter in adapters}
