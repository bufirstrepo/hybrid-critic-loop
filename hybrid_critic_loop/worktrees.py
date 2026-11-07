"""Git worktree management (stdlib only)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd) if cwd is not None else None,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


class WorktreeManager:
    def __init__(self, repo: Path):
        self.repo = Path(repo)

    @property
    def root(self) -> Path:
        return self.repo / "wt"

    def baseline_hash(self) -> str:
        # argv list, like every other git call here: a repository path
        # containing whitespace must never be split into two arguments.
        result = _git(["-C", str(self.repo), "rev-parse", "HEAD"])
        if result.returncode != 0:
            raise RuntimeError(f"git rev-parse HEAD failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def create(self, contract_id, slot) -> Path:
        path = self.repo / "wt" / str(contract_id) / str(slot)
        branch = f"critic/{contract_id}/{slot}"
        if path.exists():
            # Remove existing worktree first
            _git(["-C", str(self.repo), "worktree", "remove", "--force", str(path)])
            _git(["-C", str(self.repo), "worktree", "prune"])
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
            _git(["-C", str(self.repo), "worktree", "prune"])
        path.parent.mkdir(parents=True, exist_ok=True)
        # git worktree add -B <branch> <path> HEAD
        result = _git(["-C", str(self.repo), "worktree", "add", "-B", branch, str(path), "HEAD"])
        if result.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {result.stderr.strip() or result.stdout.strip()}")
        return path

    def diff(self, path) -> str:
        path = Path(path)
        _git(["-C", str(path), "add", "-A"])
        result = _git(["-C", str(path), "diff", "--cached"])
        if result.returncode != 0:
            raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
        return result.stdout or ""

    def commit_all(self, path, message) -> str:
        path = Path(path)
        _git(["-C", str(path), "add", "-A"])
        # Commit with inline identity; tolerate "nothing to commit"
        result = _git([
            "-C", str(path),
            "-c", "user.email=critic@local",
            "-c", "user.name=Hybrid Critic Loop",
            "commit", "-m", str(message),
        ])
        # If commit failed for a reason other than nothing-to-commit, check status:
        # If there were staged changes, this is a real error.
        if result.returncode != 0:
            combined = (result.stdout or "") + (result.stderr or "")
            if "nothing to commit" not in combined and "no changes added" not in combined:
                # Verify whether HEAD is still valid; if staged changes exist, raise
                status = _git(["-C", str(path), "status", "--porcelain"])
                if status.stdout.strip():
                    raise RuntimeError(f"git commit failed: {combined.strip()}")
                # else fall through to rev-parse
        rev = _git(["-C", str(path), "rev-parse", "HEAD"])
        if rev.returncode != 0:
            raise RuntimeError(f"git rev-parse HEAD failed: {rev.stderr.strip()}")
        return rev.stdout.strip()

    def _branch_for_path(self, path: Path) -> str:
        result = _git(["-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"])
        if result.returncode == 0 and result.stdout.strip() and result.stdout.strip() != "HEAD":
            return result.stdout.strip()
        raise RuntimeError(f"could not determine branch for worktree {path}: {result.stderr.strip()}")

    def merge_into_main(self, path) -> str:
        path = Path(path)
        # Determine branch before committing (commit doesn't change branch)
        try:
            branch = self._branch_for_path(path)
        except Exception:
            # Fallback: derive from path wt/<contract>/<slot>
            try:
                slot = path.name
                contract_id = path.parent.name
                branch = f"critic/{contract_id}/{slot}"
            except Exception as exc:
                raise RuntimeError(f"could not determine branch for {path}: {exc}") from exc
        self.commit_all(path, f"critic: {branch}")
        result = _git([
            "-C", str(self.repo),
            "-c", "user.email=critic@local",
            "-c", "user.name=Hybrid Critic Loop",
            "merge", "--no-ff", branch, "-m", f"Merge {branch}",
        ])
        if result.returncode != 0:
            combined = (result.stdout or "") + (result.stderr or "")
            # Abort merge to leave repo clean-ish on conflict
            _git(["-C", str(self.repo), "merge", "--abort"])
            raise RuntimeError(f"git merge failed: {combined.strip()}")
        rev = _git(["-C", str(self.repo), "rev-parse", "HEAD"])
        if rev.returncode != 0:
            raise RuntimeError(f"git rev-parse HEAD failed after merge: {rev.stderr.strip()}")
        return rev.stdout.strip()

    def keep_as_ref(self, contract_id, slot) -> str:
        branch = f"critic/{contract_id}/{slot}"
        path = self.repo / "wt" / str(contract_id) / str(slot)
        # Branch already exists (created by create()); ensure it exists, else nothing to verify.
        # Remove worktree dir so branch remains as loser ref.
        if path.exists():
            _git(["-C", str(self.repo), "worktree", "remove", "--force", str(path)])
            _git(["-C", str(self.repo), "worktree", "prune"])
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
                _git(["-C", str(self.repo), "worktree", "prune"])
        return branch

    def remove(self, path):
        path = Path(path)
        try:
            _git(["-C", str(self.repo), "worktree", "remove", "--force", str(path)])
        except Exception:
            pass
        try:
            _git(["-C", str(self.repo), "worktree", "prune"])
        except Exception:
            pass
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            try:
                _git(["-C", str(self.repo), "worktree", "prune"])
            except Exception:
                pass
