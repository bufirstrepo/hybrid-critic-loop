"""Hermes ``ctx.llm`` 8-family auxiliary panel adapters for The Millionaire Matrix.

Registers eight distinct vendor-family auxiliary task slots on ``ctx``
(``mm_openai``, ``mm_anthropic``, ``mm_xai``, ``mm_google``, ``mm_alibaba``,
``mm_moonshot``, ``mm_deepseek``, ``mm_zai``) with working multi-provider
defaults (verified on both Nous Portal and OpenRouter). Operators can inspect or
re-pin any seat in ``hermes model`` -> *Configure auxiliary models* or Desktop
Settings -> Models -> Auxiliary.

Invariant 2 (no self-scoring) is enforced at runtime on EVERY response:
``infer_model_family(result.model)`` must match the seat's declared ``family``.
If a single-model endpoint or fallback collapses a slot onto a different family,
the seat honestly reports ``unavailable`` / returns ``None`` in ``judge()``
rather than impersonating another family.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .executors import (
    Executor,
    GenResult,
    JUDGE_INSTRUCTION_V2,
    _clean_cost,
    _clean_token_count,
    _extract_judge_verdict,
)
from .file_edits import (
    apply_reply_edits,
    build_prompt_with_files,
    contract_target_files,
    redact_for_llm,
)

# (expert_id, family, aux_task_key, display_name, default_model)
HERMES_FAMILY_SLOTS: tuple[tuple[str, str, str, str, str], ...] = (
    ("mm-openai", "openai", "mm_openai", "MM Seat · OpenAI", "openai/gpt-5.4-mini"),
    ("mm-anthropic", "anthropic", "mm_anthropic", "MM Seat · Anthropic", "anthropic/claude-sonnet-5"),
    ("mm-xai", "xai", "mm_xai", "MM Seat · xAI Grok", "x-ai/grok-4.6"),
    ("mm-google", "google", "mm_google", "MM Seat · Google Gemini", "google/gemini-3.8-flash"),
    ("mm-alibaba", "alibaba", "mm_alibaba", "MM Seat · Alibaba Qwen", "qwen/qwen3.8-flash"),
    ("mm-moonshot", "moonshot", "mm_moonshot", "MM Seat · Moonshot Kimi", "moonshotai/kimi-k3"),
    ("mm-deepseek", "deepseek", "mm_deepseek", "MM Seat · DeepSeek", "deepseek/deepseek-v4.1-flash"),
    ("mm-zai", "zai", "mm_zai", "MM Seat · Z.ai GLM", "z-ai/glm-5.3-flash"),
)

_VENDOR_FAMILY_MAP: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "x-ai": "xai",
    "xai": "xai",
    "google": "google",
    "qwen": "alibaba",
    "alibaba": "alibaba",
    "moonshotai": "moonshot",
    "moonshot": "moonshot",
    "deepseek": "deepseek",
    "z-ai": "zai",
    "zai": "zai",
    "thudm": "zai",
    "mistralai": "mistral",
    "mistral": "mistral",
    "meta-llama": "meta",
    "meta": "meta",
    "upstage": "upstage",
    "reflection": "reflection",
    "cohere": "cohere",
    "minimax": "minimax",
}

_NAME_PREFIX_FAMILIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("gpt-", "gpt4", "gpt5", "o1-", "o1_", "o3-", "o3_", "o4-", "codex"), "openai"),
    (("claude",), "anthropic"),
    (("grok",), "xai"),
    (("gemini", "gemma"), "google"),
    (("qwen", "qwq"), "alibaba"),
    (("kimi", "moonshot"), "moonshot"),
    (("deepseek",), "deepseek"),
    (("glm-", "glm4", "glm5", "chatglm"), "zai"),
    (("mistral", "codestral", "devstral", "ministral"), "mistral"),
    (("llama-", "llama3", "llama4"), "meta"),
    (("solar-", "solar_"), "upstage"),
    (("command-r",), "cohere"),
    (("minimax",), "minimax"),
)

_GEN_SYSTEM_PROMPT = (
    "You are a competing engineering seat in The Millionaire Matrix. "
    "Produce clean, complete, working code that satisfies every acceptance gate "
    "and strictly follows the required === FILE: <path> === fenced output format."
)

_JUDGE_SYSTEM_PROMPT = (
    "You are an independent blind peer judge in The Millionaire Matrix. "
    "Evaluate every candidate strictly against the original contract task, "
    "acceptance criteria, and measured test summary. Return ONLY valid JSON."
)


def infer_model_family(model_id: str | None) -> str | None:
    """Infer canonical model family from a provider-returned model identifier."""
    if not model_id or not isinstance(model_id, str):
        return None
    cleaned = model_id.strip().lower()
    if not cleaned or cleaned in ("auto", "default", "unknown"):
        return None
    # Strip :free / :nitro / tag suffixes on the tail segment
    if "/" in cleaned:
        vendor, rest = cleaned.split("/", 1)
        vendor = vendor.strip()
        if vendor in _VENDOR_FAMILY_MAP:
            return _VENDOR_FAMILY_MAP[vendor]
        cleaned = rest.strip()
    base = cleaned.split(":", 1)[0]
    for prefixes, fam in _NAME_PREFIX_FAMILIES:
        if any(base.startswith(p) for p in prefixes):
            return fam
    return None


def register_auxiliary_tasks(ctx) -> int:
    """Register all 8 family auxiliary task slots on ``ctx`` when supported."""
    reg = getattr(ctx, "register_auxiliary_task", None)
    if not callable(reg):
        return 0
    registered = 0
    for _eid, family, task_key, display_name, default_model in HERMES_FAMILY_SLOTS:
        try:
            reg(
                task_key,
                display_name=display_name,
                description=(
                    f"Millionaire Matrix {family} family seat (generates candidates "
                    f"and blind-scores other families; default {default_model})."
                ),
                defaults={
                    "provider": "auto",
                    "model": default_model,
                    "timeout": 180,
                },
            )
            registered += 1
        except Exception:
            continue
    return registered


def _is_timeout_exc(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "timeout" in text or "timed out" in text


def _is_refusal_or_unavailable_exc(exc: BaseException) -> tuple[str, str]:
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    if _is_timeout_exc(exc):
        return "timeout", f"timed out: {text}"
    if any(k in low for k in (
        "unavailable", "not configured", "missing_provider", "401", "402", "403",
        "404", "model_not_found", "no endpoints found", "trust", "permission",
    )):
        return "unavailable", text
    return "error", text


class HermesAuxExecutor(Executor):
    """One model-family seat backed by Hermes ``ctx.llm`` auxiliary routing."""

    parallel_safe = True

    def __init__(
        self,
        ctx,
        expert_id: str,
        family: str,
        task_key: str,
        display_name: str,
        default_model: str,
    ) -> None:
        self.ctx = ctx
        self.expert_id = expert_id
        self.family = family
        self.task_key = task_key
        self.display_name = display_name
        self.default_model = default_model
        self.last_model: str | None = None

    def available(self) -> tuple[bool, str]:
        llm = getattr(self.ctx, "llm", None)
        if llm is None or not callable(getattr(llm, "complete", None)):
            return (False, "Hermes ctx.llm is not available in this context")
        shown = self.last_model or self.default_model
        return (True, f"hermes aux slot {self.task_key} ({shown})")

    def generate(self, worktree: Path, contract: dict, context: str) -> GenResult:
        start = time.monotonic()
        ok, why = self.available()
        if not ok:
            return GenResult(status="unavailable", log="", duration_ms=0, detail=why)
        try:
            prompt = build_prompt_with_files(
                Path(worktree), contract or {}, context or "", allow_discovery=True
            )
            res = self.ctx.llm.complete(
                messages=[
                    {"role": "system", "content": _GEN_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                task=self.task_key,
                max_tokens=4096,
                timeout=90,
                purpose=f"mm.generate.{self.family}",
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            real_model = str(getattr(res, "model", "") or "").strip()
            real_provider = str(getattr(res, "provider", "") or "auto").strip()
            actual_family = infer_model_family(real_model)
            if actual_family != self.family:
                return GenResult(
                    status="unavailable",
                    log="",
                    duration_ms=duration_ms,
                    detail=(
                        f"{self.expert_id}: slot {self.task_key} resolved to "
                        f"{real_provider}:{real_model or 'unknown'} "
                        f"(family={actual_family!r}, expected={self.family!r}); "
                        "seat skipped to preserve 8-family no-self-scoring integrity"
                    ),
                )
            self.last_model = f"{real_provider}:{real_model}" if real_provider else real_model
            usage = getattr(res, "usage", None)
            tokens_in = _clean_token_count(getattr(usage, "input_tokens", None)) if usage else None
            tokens_out = _clean_token_count(getattr(usage, "output_tokens", None)) if usage else None
            cost_usd = _clean_cost(getattr(usage, "cost_usd", None)) if usage else None
            if tokens_in == 0 and tokens_out == 0:
                tokens_in = None
                tokens_out = None

            reply_text = str(getattr(res, "text", "") or "")
            allow_unlisted = not bool(contract_target_files(contract))
            handled, edit_detail = apply_reply_edits(
                Path(worktree),
                contract,
                reply_text,
                allow_unlisted_headers=allow_unlisted,
            )
            if handled and edit_detail.startswith("error: "):
                return GenResult(
                    status="error",
                    log=reply_text,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost_usd,
                    duration_ms=duration_ms,
                    detail=f"{self.last_model}: {edit_detail[len('error: '):]}",
                )
            return GenResult(
                status="completed",
                log=reply_text,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                duration_ms=duration_ms,
                detail=f"{self.last_model}; {edit_detail}".strip("; "),
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            status, detail = _is_refusal_or_unavailable_exc(exc)
            return GenResult(
                status=status,
                log="",
                cost_usd=None,
                duration_ms=duration_ms,
                detail=f"{self.expert_id} ({self.task_key}): {detail}",
            )

    def judge(self, packet: str, rubric: dict) -> dict | None:
        ok, _ = self.available()
        if not ok:
            return None
        safe_packet = redact_for_llm(packet or "")
        prompt = (
            safe_packet
            + "\n\nRUBRIC:\n"
            + json.dumps(rubric, indent=2, default=str)
            + "\n\n"
            + JUDGE_INSTRUCTION_V2
        )
        try:
            res = self.ctx.llm.complete(
                messages=[
                    {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                task=self.task_key,
                max_tokens=4096,
                temperature=0.0,
                timeout=90,
                purpose=f"mm.judge.{self.family}",
            )
        except Exception:
            return None
        real_model = str(getattr(res, "model", "") or "").strip()
        if infer_model_family(real_model) != self.family:
            return None
        return _extract_judge_verdict(str(getattr(res, "text", "") or ""))


def build_hermes_executors(ctx) -> dict[str, HermesAuxExecutor]:
    """Construct the 8-family ``HermesAuxExecutor`` panel bound to ``ctx``."""
    return {
        eid: HermesAuxExecutor(
            ctx=ctx,
            expert_id=eid,
            family=family,
            task_key=task_key,
            display_name=display_name,
            default_model=default_model,
        )
        for eid, family, task_key, display_name, default_model in HERMES_FAMILY_SLOTS
    }
