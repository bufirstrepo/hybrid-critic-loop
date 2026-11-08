"""API-backed executors for the Hybrid Critic Loop (stdlib only).

W5 workstream: the two panel adapters that do not shell out to a local
CLI —

* ``OllamaQwenExecutor`` — expert_id ``qwen-local``, family ``alibaba``.
  Talks to the owner's local Ollama server (``OLLAMA_HOST``, default
  ``http://127.0.0.1:11434``) which serves ``qwen2.5-coder:14b``.
* ``DeepSeekExecutor`` — expert_id ``deepseek``, family ``deepseek``.
  OpenAI-compatible chat-completions API, key from ``DEEPSEEK_API_KEY``.

This module is deliberately separate from ``executors.py`` so it could
be built without racing the CLI-adapter workstream that owns that
file; the coordinator wires these adapters into ``default_executors()``
at integration. (Note for integration: the CLI ``QwenExecutor`` in
``executors.py`` also uses expert_id ``qwen-local`` — the panel dict
can only hold one of them under that key, so integration must choose
which seat fills it, or re-key one adapter.)

Availability is always reported honestly from the environment: no
server / no model / no API key means ``available()`` is False, and
neither adapter ever raises out of ``available()``, ``generate()`` or
``judge()``. Costs are never invented: local Ollama reports
``cost_usd=None`` and DeepSeek's response carries no cost figure, so
it also reports ``cost_usd=None`` ("not reported").
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from .executors import (
    Executor,
    GenResult,
    _extract_judge_verdict,
    _parse_jsonl_usage,
    build_prompt,
)

# JUDGE instruction: prefer v2 when present (it is, in the current
# executors.py), fall back to the v1/alias name if a future refactor
# drops the v2 symbol.
try:  # pragma: no cover - exercised by whichever symbol exists
    from .executors import JUDGE_INSTRUCTION_V2 as _JUDGE_INSTRUCTION
except ImportError:  # pragma: no cover
    from .executors import JUDGE_INSTRUCTION as _JUDGE_INSTRUCTION

__all__ = [
    "DEEPSEEK_API_URL",
    "DEEPSEEK_DEFAULT_MODEL",
    "OLLAMA_DEFAULT_HOST",
    "OLLAMA_MODEL_PREFIX",
    "DeepSeekExecutor",
    "OllamaQwenExecutor",
]

OLLAMA_DEFAULT_HOST = "http://127.0.0.1:11434"
OLLAMA_MODEL_PREFIX = "qwen2.5-coder"
OLLAMA_FALLBACK_MODEL = "qwen2.5-coder:14b"

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"

_AVAILABLE_TIMEOUT_S = 2.0
_MODEL_LOOKUP_TIMEOUT_S = 2.0
_GENERATE_TIMEOUT_S = 120.0
_JUDGE_TIMEOUT_S = 120.0

_FENCED_BLOCK_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _http_get_json(url: str, timeout: float) -> tuple[dict | None, str | None]:
    """GET ``url`` and parse a JSON object. Returns (obj, error)."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} from {url}"
    except Exception as exc:  # URLError, timeout, refused, ...
        return None, f"{type(exc).__name__}: {exc}"
    try:
        obj = json.loads(body)
    except Exception as exc:
        return None, f"invalid JSON from {url}: {exc}"
    if not isinstance(obj, dict):
        return None, f"non-object JSON from {url}"
    return obj, None


def _http_post_json(
    url: str, payload: dict, timeout: float, headers: dict | None = None
) -> tuple[dict | None, str | None]:
    """POST ``payload`` as JSON to ``url``. Returns (obj, error)."""
    data = json.dumps(payload).encode("utf-8")
    all_headers = {"Content-Type": "application/json"}
    if headers:
        all_headers.update(headers)
    try:
        req = urllib.request.Request(url, data=data, headers=all_headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        snippet = ""
        try:
            snippet = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        return None, f"HTTP {exc.code} from {url}: {snippet}".strip()
    except Exception as exc:  # URLError, timeout, refused, ...
        return None, f"{type(exc).__name__}: {exc}"
    try:
        obj = json.loads(body)
    except Exception as exc:
        return None, f"invalid JSON from {url}: {exc}"
    if not isinstance(obj, dict):
        return None, f"non-object JSON from {url}"
    return obj, None


def _extract_code_block(text: str) -> str | None:
    """First fenced code block in a model reply, or None."""
    if not text:
        return None
    match = _FENCED_BLOCK_RE.search(text)
    if not match:
        return None
    return match.group(1)


def _write_target_file(
    worktree: Path, contract: dict, reply_text: str
) -> tuple[bool, str]:
    """Write the reply's fenced code block to the contract target file.

    Returns (handled, detail). ``handled`` is False when the contract
    names no target_file (nothing to write — the reply itself is the
    result). When a target_file IS named but no code block can be
    extracted, returns (True, error-detail) with an ``error:`` prefix
    so the caller can fail the generation honestly.
    """
    target = contract.get("target_file") if isinstance(contract, dict) else None
    if not target:
        return False, ""
    code = _extract_code_block(reply_text)
    if code is None:
        return True, (
            f"error: no fenced code block found in model reply; "
            f"could not write target_file {target}"
        )
    root = Path(worktree).resolve()
    dest = (root / str(target)).resolve()
    if dest != root and root not in dest.parents:
        return True, f"error: target_file {target} escapes the worktree; not written"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(code, encoding="utf-8")
    except Exception as exc:
        return True, f"error: writing target_file {target} failed: {exc}"
    return True, f"wrote {target} from fenced code block in model reply"


def _judge_prompt(packet: str, rubric: dict) -> str:
    return (
        packet
        + "\n\nRUBRIC:\n"
        + json.dumps(rubric, indent=2, default=str)
        + "\n\n"
        + _JUDGE_INSTRUCTION
    )


def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Ollama-backed Qwen
# ---------------------------------------------------------------------------

class OllamaQwenExecutor(Executor):
    """Qwen2.5-Coder served by the owner's local Ollama.

    The model cannot edit files itself, so ``generate()`` extracts a
    fenced code block from its reply and writes it to the contract's
    ``target_file`` inside the worktree. ``available()`` is a live
    probe of ``{OLLAMA_HOST}/api/tags`` with a short (2s) timeout and
    requires a listed model whose name starts with ``qwen2.5-coder``;
    the exact tag found is recorded on ``self.model_tag``.
    """

    expert_id = "qwen-local"
    family = "alibaba"

    def __init__(self) -> None:
        self.model_tag: str | None = None

    # -- plumbing ------------------------------------------------------

    def _host(self) -> str:
        return (os.environ.get("OLLAMA_HOST") or OLLAMA_DEFAULT_HOST).rstrip("/")

    def _find_model(self) -> str | None:
        """Exact qwen2.5-coder tag listed by the server, or None."""
        obj, err = _http_get_json(
            f"{self._host()}/api/tags", timeout=_MODEL_LOOKUP_TIMEOUT_S
        )
        if err is not None or obj is None:
            return None
        for entry in obj.get("models") or []:
            name = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(name, str) and name.startswith(OLLAMA_MODEL_PREFIX):
                self.model_tag = name
                return name
        return None

    def _model(self) -> str:
        return (
            os.environ.get("OLLAMA_MODEL")
            or self.model_tag
            or self._find_model()
            or OLLAMA_FALLBACK_MODEL
        )

    def _generate_text(self, prompt: str, timeout: float) -> tuple[dict | None, str | None]:
        payload = {"model": self._model(), "prompt": prompt, "stream": False}
        return _http_post_json(f"{self._host()}/api/generate", payload, timeout=timeout)

    # -- Executor interface ---------------------------------------------

    def available(self) -> tuple[bool, str]:
        host = self._host()
        obj, err = _http_get_json(f"{host}/api/tags", timeout=_AVAILABLE_TIMEOUT_S)
        if err is not None or obj is None:
            return (False, f"ollama not reachable at {host}")
        names: list[str] = []
        for entry in obj.get("models") or []:
            name = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(name, str):
                names.append(name)
                if name.startswith(OLLAMA_MODEL_PREFIX):
                    self.model_tag = name
                    return (True, f"ollama at {host}: {name}")
        listed = ", ".join(names) if names else "none"
        return (
            False,
            f"ollama reachable at {host} but no {OLLAMA_MODEL_PREFIX} model "
            f"listed (models: {listed})",
        )

    def generate(self, worktree: Path, contract: dict, context: str) -> GenResult:
        start = time.monotonic()
        try:
            prompt = build_prompt(contract, context)
            obj, err = self._generate_text(prompt, timeout=_GENERATE_TIMEOUT_S)
            duration_ms = int((time.monotonic() - start) * 1000)
            if err is not None or obj is None:
                return GenResult(
                    status="error",
                    log="",
                    cost_usd=None,
                    duration_ms=duration_ms,
                    detail=f"ollama generate failed at {self._host()}: {err}",
                )
            reply = obj.get("response") or ""
            tokens_in = _as_int(obj.get("prompt_eval_count"))
            tokens_out = _as_int(obj.get("eval_count"))
            handled, detail = _write_target_file(Path(worktree), contract or {}, reply)
            if handled and detail.startswith("error:"):
                return GenResult(
                    status="error",
                    log=reply,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=None,
                    duration_ms=duration_ms,
                    detail=detail[len("error: "):],
                )
            return GenResult(
                status="completed",
                log=reply,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=None,  # local model — no cost reported, none invented
                duration_ms=duration_ms,
                detail=detail,
            )
        except Exception as exc:  # never let an exception escape generate()
            duration_ms = int((time.monotonic() - start) * 1000)
            return GenResult(
                status="error",
                log="",
                cost_usd=None,
                duration_ms=duration_ms,
                detail=f"ollama generate failed: {type(exc).__name__}: {exc}",
            )

    def judge(self, packet: str, rubric: dict) -> dict | None:
        try:
            ok, _ = self.available()
            if not ok:
                return None
            obj, err = self._generate_text(
                _judge_prompt(packet, rubric), timeout=_JUDGE_TIMEOUT_S
            )
            if err is not None or obj is None:
                return None
            return _extract_judge_verdict(obj.get("response") or "")
        except Exception:
            return None


# ---------------------------------------------------------------------------
# DeepSeek API
# ---------------------------------------------------------------------------

class DeepSeekExecutor(Executor):
    """DeepSeek chat-completions API (OpenAI-compatible shape).

    Availability is purely the presence of a non-empty
    ``DEEPSEEK_API_KEY`` — no network probe is made by ``available()``.
    Like the Ollama adapter, the model cannot edit files, so
    ``generate()`` extracts a fenced code block from the reply for the
    contract's ``target_file``. The API response carries no cost
    figure, so ``cost_usd`` stays None ("not reported") — no price
    table is invented to fill it in.
    """

    expert_id = "deepseek"
    family = "deepseek"

    def _api_key(self) -> str:
        return os.environ.get("DEEPSEEK_API_KEY") or ""

    def _model(self) -> str:
        return os.environ.get("DEEPSEEK_MODEL") or DEEPSEEK_DEFAULT_MODEL

    def _chat(self, prompt: str, timeout: float) -> tuple[dict | None, str | None]:
        payload = {
            "model": self._model(),
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self._api_key()}"}
        return _http_post_json(DEEPSEEK_API_URL, payload, timeout=timeout, headers=headers)

    @staticmethod
    def _reply_text(obj: dict) -> str:
        try:
            choices = obj.get("choices") or []
            if choices and isinstance(choices[0], dict):
                message = choices[0].get("message") or {}
                if isinstance(message, dict):
                    return message.get("content") or ""
        except Exception:
            pass
        return ""

    @staticmethod
    def _usage(obj: dict) -> tuple[int | None, int | None, list[str]]:
        tokens_in = tokens_out = None
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else None
        if usage is not None:
            tokens_in = _as_int(usage.get("prompt_tokens"))
            tokens_out = _as_int(usage.get("completion_tokens"))
        if tokens_in is None and tokens_out is None:
            # Fallback: reuse the JSONL usage walker from executors.py
            # over the serialised response object.
            try:
                t_in, t_out, _rids, _cost = _parse_jsonl_usage(json.dumps(obj))
                tokens_in, tokens_out = t_in, t_out
            except Exception:
                pass
        request_ids = [str(obj["id"])] if obj.get("id") else []
        return tokens_in, tokens_out, request_ids

    # -- Executor interface ---------------------------------------------

    def available(self) -> tuple[bool, str]:
        if self._api_key().strip():
            return (True, "api key present")
        return (False, "DEEPSEEK_API_KEY not set")

    def generate(self, worktree: Path, contract: dict, context: str) -> GenResult:
        start = time.monotonic()
        ok, detail_avail = self.available()
        if not ok:
            return GenResult(status="unavailable", log="", detail=detail_avail, duration_ms=0)
        try:
            prompt = build_prompt(contract, context)
            obj, err = self._chat(prompt, timeout=_GENERATE_TIMEOUT_S)
            duration_ms = int((time.monotonic() - start) * 1000)
            if err is not None or obj is None:
                return GenResult(
                    status="error",
                    log="",
                    cost_usd=None,
                    duration_ms=duration_ms,
                    detail=f"deepseek generate failed: {err}",
                )
            reply = self._reply_text(obj)
            tokens_in, tokens_out, request_ids = self._usage(obj)
            handled, detail = _write_target_file(Path(worktree), contract or {}, reply)
            if handled and detail.startswith("error:"):
                return GenResult(
                    status="error",
                    log=reply,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=None,
                    request_ids=request_ids,
                    duration_ms=duration_ms,
                    detail=detail[len("error: "):],
                )
            return GenResult(
                status="completed",
                log=reply,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=None,  # not reported by the API — never computed
                request_ids=request_ids,
                duration_ms=duration_ms,
                detail=detail,
            )
        except Exception as exc:  # never let an exception escape generate()
            duration_ms = int((time.monotonic() - start) * 1000)
            return GenResult(
                status="error",
                log="",
                cost_usd=None,
                duration_ms=duration_ms,
                detail=f"deepseek generate failed: {type(exc).__name__}: {exc}",
            )

    def judge(self, packet: str, rubric: dict) -> dict | None:
        try:
            ok, _ = self.available()
            if not ok:
                return None
            obj, err = self._chat(_judge_prompt(packet, rubric), timeout=_JUDGE_TIMEOUT_S)
            if err is not None or obj is None:
                return None
            return _extract_judge_verdict(self._reply_text(obj))
        except Exception:
            return None
