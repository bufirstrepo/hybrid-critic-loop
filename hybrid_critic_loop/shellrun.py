"""Shell runner utilities (stdlib only)."""
from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


# Linux MAX_ARG_STRLEN: the kernel refuses (E2BIG) any single argv string
# longer than this. Checked before execve (fix pass Wave 2.9, review M8 /
# 1D-09) so an oversize prompt or judge packet is reported with its cause
# instead of surfacing as a bare OSError.
MAX_ARG_STRLEN = 131072

# Only configuration paths belonging to the invoked vendor are inherited.
_VENDOR_CONFIG = {
    "codex": ("CODEX_HOME",),
    "claude": ("CLAUDE_CONFIG_DIR",),
    "grok": ("GROK_CONFIG_DIR", "GROK_HOME"),
    "kimi": ("KIMI_HOME", "KIMI_CONFIG_DIR"),
    "qwen": ("QWEN_HOME", "QWEN_CONFIG_DIR"),
    "agy": ("AGY_CONFIG_DIR", "AGY_HOME"),
}


def vendor_cli_env(vendor: str, source=None) -> dict[str, str]:
    """Return an explicit minimal environment, without provider credentials.

    Config paths may allow a CLI to use its own saved login. They do not make
    the child a sandbox: it still runs with the operator's filesystem access.
    """
    source = os.environ if source is None else source
    name = Path(vendor).name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    allowed = {"PATH", "HOME", "LANG", "LANGUAGE", "TMPDIR"}
    allowed.update(_VENDOR_CONFIG.get(name, ()))
    return {k: v for k, v in source.items()
            if k in allowed or k.startswith("LC_")}


@dataclass
class RunResult:
    cmd: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool


def run_command(cmd: str, cwd=None, timeout_s: int = 900, env=None) -> RunResult:
    """Run a command string without a shell.

    - shlex.split the cmd, subprocess.run with shell=False, stdin=DEVNULL,
      capture text.
    - Timeout -> timed_out=True, exit_code=None.
    - Never raise on non-zero exit.
    """
    cmd_str = cmd if isinstance(cmd, str) else " ".join(shlex.quote(str(c)) for c in cmd)  # defensive
    start = time.monotonic()
    try:
        parts = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)  # type: ignore[arg-type]
    except ValueError as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return RunResult(cmd=cmd_str, exit_code=None, stdout="", stderr=str(exc), duration_ms=duration_ms, timed_out=False)

    if not parts:
        duration_ms = int((time.monotonic() - start) * 1000)
        return RunResult(cmd=cmd_str, exit_code=None, stdout="", stderr="empty command", duration_ms=duration_ms, timed_out=False)

    biggest = max(len(str(p).encode("utf-8", errors="replace")) for p in parts)
    if biggest > MAX_ARG_STRLEN:
        duration_ms = int((time.monotonic() - start) * 1000)
        return RunResult(
            cmd=cmd_str, exit_code=None, stdout="",
            stderr=(f"E2BIG: a single argument of {biggest} bytes exceeds the "
                    f"{MAX_ARG_STRLEN}-byte limit; the command was not run"),
            duration_ms=duration_ms, timed_out=False,
        )

    try:
        proc = subprocess.run(
            parts,
            cwd=str(cwd) if cwd is not None else None,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=vendor_cli_env("") if env is None else env,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return RunResult(
            cmd=cmd_str,
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration_ms=duration_ms,
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return RunResult(
            cmd=cmd_str,
            exit_code=None,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=True,
        )
    except FileNotFoundError as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return RunResult(
            cmd=cmd_str,
            exit_code=None,
            stdout="",
            stderr=str(exc),
            duration_ms=duration_ms,
            timed_out=False,
        )
    except OSError as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return RunResult(
            cmd=cmd_str,
            exit_code=None,
            stdout="",
            stderr=str(exc),
            duration_ms=duration_ms,
            timed_out=False,
        )


def sha256_text(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()
