"""Safe worktree file context embedding and reply extraction for model seats.

Used by ``HermesAuxExecutor``, API seats (``executors_api.py``), and print-mode
CLI seats (``ClaudeExecutor`` / ``GrokExecutor`` when they print fenced code to
stdout instead of editing in-place) so models can produce concrete multi-file or
single-file diffs inside isolated worktrees without shell-approval flags.
"""

from __future__ import annotations

import re
from pathlib import Path

from .executors import build_prompt

_FENCED_BLOCK_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)
_FILE_HEADER_BLOCK_RE = re.compile(
    r"===\s*FILE:\s*([^\n=]+?)\s*===\s*\n```[^\n`]*\n(.*?)```",
    re.DOTALL,
)

_SKIP_DIRS = frozenset({
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
    "node_modules", ".venv", "venv", "dist", "build",
})
_BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
    ".tar", ".gz", ".whl", ".so", ".dylib", ".dll", ".exe", ".pyc", ".db", ".sqlite",
})


def redact_for_llm(text: str) -> str:
    """Mask secrets before sending worktree file text to an LLM when inside Hermes."""
    if not text:
        return ""
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True)
    except Exception as exc:
        raise RuntimeError("secret redaction unavailable; refusing to send file content") from exc


def contract_target_files(contract: dict | None) -> list[str]:
    """Return ordered, deduplicated target relative paths declared on ``contract``."""
    if not isinstance(contract, dict):
        return []
    out: list[str] = []
    raw_many = contract.get("target_files")
    if isinstance(raw_many, list):
        for item in raw_many:
            if isinstance(item, str) and item.strip() and item.strip() not in out:
                out.append(item.strip())
    single = contract.get("target_file")
    if isinstance(single, str) and single.strip() and single.strip() not in out:
        out.insert(0, single.strip())
    return out


def resolve_confined_path(worktree: Path, relpath: str) -> Path | None:
    """Resolve ``relpath`` strictly inside ``worktree`` (never root itself or ``.git``)."""
    cleaned = (relpath or "").strip().strip("`'\"")
    if not cleaned:
        return None
    if cleaned.startswith("/"):
        return None
    root = Path(worktree).resolve()
    try:
        dest = (root / cleaned).resolve()
    except Exception:
        return None
    if dest == root or root not in dest.parents:
        return None
    rel_parts = dest.relative_to(root).parts
    if not rel_parts or ".git" in rel_parts:
        return None
    return dest


def discover_worktree_files(worktree: Path, max_files: int = 8, max_total_bytes: int = 49152) -> list[str]:
    """Discover small text source files in ``worktree`` when no target_file was given."""
    root = Path(worktree).resolve()
    if not root.is_dir():
        return []
    found: list[str] = []
    used_bytes = 0
    for path in sorted(root.rglob("*")):
        if len(found) >= max_files or used_bytes >= max_total_bytes:
            break
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if any(part in _SKIP_DIRS or part.startswith(".git") for part in rel.parts):
            continue
        if not path.is_file() or path.suffix.lower() in _BINARY_SUFFIXES:
            continue
        try:
            size = path.stat().st_size
            if size > max_total_bytes - used_bytes:
                continue
            path.read_text(encoding="utf-8")
        except Exception:
            continue
        found.append(rel.as_posix())
        used_bytes += size
    return found


def build_prompt_with_files(
    worktree: Path,
    contract: dict,
    context: str,
    *,
    allow_discovery: bool = False,
    max_file_bytes: int = 65536,
) -> str:
    """Build generation prompt including current target file contents and reply format rules."""
    base = redact_for_llm(build_prompt(contract, context))
    contract = contract or {}
    targets = contract_target_files(contract)
    if not targets and allow_discovery:
        targets = discover_worktree_files(worktree)

    parts = [base]
    if contract.get("mode") == "improve":
        parts.extend([
            "",
            "IMPROVE MODE:",
            (
                "The baseline test suite ALREADY PASSES. Your job is to make a real, "
                "measurable improvement to the code that fulfills the TASK above while "
                "keeping every hard and measured acceptance gate green. Zero-diff / "
                "no-op replies are automatically eliminated."
            ),
        ])

    if targets:
        root = Path(worktree).resolve()
        file_sections: list[str] = []
        for rel in targets:
            dest = resolve_confined_path(root, rel)
            if dest is None or not dest.is_file():
                file_sections.append(f"=== FILE: {rel} (new file — does not exist yet) ===")
                continue
            raw_bytes = dest.read_bytes()
            if len(raw_bytes) > max_file_bytes:
                raise ValueError(f"target file {rel} exceeds the safe full-file edit limit")
            text = raw_bytes.decode("utf-8")
            safe_text = redact_for_llm(text)
            if safe_text != text:
                raise ValueError(f"target file {rel} contains redacted content; refusing full-file replacement")
            file_sections.append(f"=== FILE: {rel} ===\n```\n{safe_text}\n```")
        if file_sections:
            parts.extend(["", "CURRENT WORKTREE FILES:", *file_sections])

    parts.append("")
    parts.append("OUTPUT FORMAT (REQUIRED):")
    if len(targets) == 1:
        only = targets[0]
        parts.append(
            f"Return the complete updated contents of `{only}` using:\n"
            f"=== FILE: {only} ===\n```python\n<full updated file contents>\n```\n"
            "(A single fenced code block with the complete file contents is also accepted.)"
        )
    else:
        parts.append(
            "For EVERY file you create or modify, emit its relative path header "
            "immediately followed by a fenced code block with the COMPLETE updated file contents:\n"
            "=== FILE: path/to/file.ext ===\n```\n<full updated file contents>\n```"
        )
    return "\n".join(parts)


def apply_reply_edits(
    worktree: Path,
    contract: dict | None,
    reply_text: str,
    *,
    allow_unlisted_headers: bool = False,
) -> tuple[bool, str]:
    """Extract file edits from ``reply_text`` and write them safely under ``worktree``.

    Returns ``(handled, detail)``. ``detail`` starts with ``"error: "`` when
    extraction or path confinement failed.
    """
    contract = contract if isinstance(contract, dict) else {}
    single_target = contract.get("target_file")
    targets = contract_target_files(contract)

    header_matches = list(_FILE_HEADER_BLOCK_RE.finditer(reply_text or ""))
    if header_matches:
        written: list[str] = []
        for match in header_matches:
            rel = match.group(1).strip()
            code = match.group(2)
            dest = resolve_confined_path(worktree, rel)
            if dest is None:
                return True, f"error: target_file {rel} escapes the worktree; not written"
            if targets and not allow_unlisted_headers and rel not in targets:
                # Allow normalized relative match (e.g. ./foo.py == foo.py)
                allowed_paths = {
                    p for p in (resolve_confined_path(worktree, t) for t in targets) if p is not None
                }
                if dest not in allowed_paths:
                    return True, f"error: reply path {rel} is not in contract target_files; not written"
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(code, encoding="utf-8")
                written.append(rel)
            except Exception as exc:
                return True, f"error: writing target_file {rel} failed: {exc}"
        return True, f"wrote {', '.join(written)} from model reply"

    # Single-target fenced-block fallback (preserves exact executors_api.py behavior/messages).
    fallback_target = single_target or (targets[0] if len(targets) == 1 else None)
    if fallback_target:
        # Check path confinement even when fenced block is present; match original order:
        match = _FENCED_BLOCK_RE.search(reply_text or "")
        if not match:
            return True, (
                f"error: no fenced code block found in model reply; "
                f"could not write target_file {fallback_target}"
            )
        code = match.group(1)
        dest = resolve_confined_path(worktree, str(fallback_target))
        if dest is None:
            return True, f"error: target_file {fallback_target} escapes the worktree; not written"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(code, encoding="utf-8")
        except Exception as exc:
            return True, f"error: writing target_file {fallback_target} failed: {exc}"
        return True, f"wrote {fallback_target} from fenced code block in model reply"

    if targets:
        return True, (
            "error: no === FILE: <path> === fenced blocks found in model reply; "
            f"could not write target_files {targets}"
        )
    if allow_unlisted_headers:
        return True, "error: no === FILE: <path> === fenced blocks found in model reply"
    return False, ""
