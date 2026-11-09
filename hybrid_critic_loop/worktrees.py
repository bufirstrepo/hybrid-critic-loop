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

    def path_for(self, contract_id, slot) -> Path:
        """The path ``create`` WOULD give a worktree, without creating
        it (fix spec G9): the loop names a candidate's worktree before
        it knows whether the seat is available at all."""
        return self.repo / "wt" / str(contract_id) / str(slot)

    def claim_run(self, contract_id) -> bool:
        """Atomically claim a contract's single run (fix spec G9;
        round-4 contest-core F7).

        An ``O_CREAT | O_EXCL`` file under the contract's worktree
        directory: exactly one process creates it, every racing second
        run gets False instead of fighting over worktree and branch
        names. The claim is taken before any worktree is created and
        records the claimant's pid and the time it was taken. The loop
        releases it on EVERY run exit (round-5 fix spec H6; round-5
        gate-core F4): the file guards a run that is actually live —
        the contract row reads "running" — and nothing else. A claim
        left behind by a run that died inside the claim window (before
        the status flip, e.g. during baseline verification) is stale:
        the loop breaks it with a named note on the result and takes
        the claim for the new run, because that contract never
        dispatched and its id was never spent.
        """
        import os
        import time

        claim_dir = self.root / str(contract_id)
        try:
            claim_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                claim_dir / ".critic-run-claim",
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            return False
        except OSError:
            # A filesystem that cannot take the claim must not block the
            # run: the store's status claim still guards sequentially.
            return True
        try:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            os.write(
                fd,
                f"pid {os.getpid()} time {stamp} contract {contract_id}\n".encode(),
            )
        finally:
            os.close(fd)
        return True

    def release_run(self, contract_id):
        """Release a run claim taken by :meth:`claim_run`."""
        try:
            (self.root / str(contract_id) / ".critic-run-claim").unlink(
                missing_ok=True)
        except OSError:
            pass

    def snapshot_ref(self, contract_id, candidate_id) -> str:
        """The dedicated ref pinning a candidate's verdict-time
        snapshot commit (round-5 fix spec H6; round-5 gate-core F3).

        The snapshot used to live only on the winner's branch —
        deleting that one ref (plus a gc) silently demoted the human
        gate to the legacy live-tree merge. The snapshot ref is a
        second, purpose-named anchor: pinned at verdict time, resolved
        first at gate time, deleted at terminal states."""
        return f"refs/critic-snapshots/{contract_id}/{candidate_id}"

    def pin_snapshot_ref(self, contract_id, candidate_id, commit) -> str:
        """Pin ``commit`` under the candidate's snapshot ref."""
        ref = self.snapshot_ref(contract_id, candidate_id)
        result = _git(["-C", str(self.repo), "update-ref", ref, str(commit)])
        if result.returncode != 0:
            raise RuntimeError(
                f"git update-ref {ref} failed: "
                f"{result.stderr.strip() or result.stdout.strip()}")
        return ref

    def snapshot_ref_commit(self, contract_id, candidate_id):
        """The commit the candidate's snapshot ref names, or None."""
        ref = self.snapshot_ref(contract_id, candidate_id)
        result = _git(
            ["-C", str(self.repo), "rev-parse", "--verify", f"{ref}^{{commit}}"])
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def delete_snapshot_ref(self, contract_id, candidate_id) -> bool:
        """Delete a candidate's snapshot ref (terminal cleanup)."""
        ref = self.snapshot_ref(contract_id, candidate_id)
        result = _git(["-C", str(self.repo), "update-ref", "-d", ref])
        return result.returncode == 0

    def is_commit(self, ref) -> bool:
        """True iff ``ref`` names a commit object in this repo — the
        discriminator between a verdict-time snapshot commit recorded
        as a candidate's payload hash and a legacy diff hash (fix spec
        G1; round-4 contest-core F1)."""
        if not ref or not isinstance(ref, str):
            return False
        result = _git(["-C", str(self.repo), "cat-file", "-e", f"{ref}^{{commit}}"])
        return result.returncode == 0

    def merge_commit_into_main(self, commit) -> str:
        """Merge an existing commit (a verdict-time snapshot) into the
        repo's main checkout — never the live worktree it came from
        (fix spec G1). Returns the new HEAD sha."""
        result = _git([
            "-C", str(self.repo),
            "-c", "user.email=critic@local",
            "-c", "user.name=Hybrid Critic Loop",
            "merge", "--no-ff", str(commit), "-m", f"Merge {commit}",
        ])
        if result.returncode != 0:
            combined = (result.stdout or "") + (result.stderr or "")
            _git(["-C", str(self.repo), "merge", "--abort"])
            raise RuntimeError(f"git merge failed: {combined.strip()}")
        rev = _git(["-C", str(self.repo), "rev-parse", "HEAD"])
        if rev.returncode != 0:
            raise RuntimeError(f"git rev-parse HEAD failed after merge: {rev.stderr.strip()}")
        return rev.stdout.strip()

    def delete_branch(self, branch) -> bool:
        """Delete a branch (fix spec G9: branches die at terminal
        states). ``-d`` first; ``-D`` when the branch was never merged
        (a rejected or escalated field). Returns True iff the branch
        is gone afterwards."""
        for flag in ("-d", "-D"):
            result = _git(["-C", str(self.repo), "branch", flag, str(branch)])
            if result.returncode == 0:
                return True
        check = _git(["-C", str(self.repo), "rev-parse", "--verify", str(branch)])
        return check.returncode != 0

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
