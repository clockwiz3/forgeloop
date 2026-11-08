"""Per-run copy of the target repo on its own branch. Hidden tests and secrets never enter it."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

EXCLUDE = (".git", ".env", "hidden", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv")
BASE_REF = "forgeloop-base"  # the commit a run started from; diffs, SAST and SCA compare against it
GIT_IDENTITY = ["-c", "user.name=forgeloop", "-c", "user.email=forgeloop@localhost", "-c", "commit.gpgsign=false"]


def remove(path: Path) -> None:
    """rmtree that also works past the 260-character limit on Windows and on read-only git objects."""
    target = str(path.resolve())
    if os.name == "nt" and not target.startswith("\\\\?\\"):
        target = "\\\\?\\" + target

    def unlock(func: Any, p: str, _: Any) -> None:
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if os.path.exists(target):
        shutil.rmtree(target, onexc=unlock)


def git(ws: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *GIT_IDENTITY, *args], cwd=ws, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return out.stdout


def prepare(source: Path, dest: Path, branch: str, *, setup: Path | None = None, clone: bool = False) -> Path:
    """A working copy of `source` on a new `branch`, with the starting point tagged as BASE_REF.

    clone=False: copy the files (minus secrets and caches) into a fresh repository. Used for the bench, where the
    polygon is a subdirectory and hidden tests must never be copied.
    clone=True: `git clone` the source, so the branch shares history with it and can be pushed as a pull request.
    Only tracked files come along; untracked secrets such as .env stay behind.
    `setup` is a patch that becomes part of the starting state.
    """
    if dest.exists():
        raise FileExistsError(dest)
    if clone:
        subprocess.run(
            ["git", "clone", "-q", "--local", "--no-hardlinks", str(source.resolve()), str(dest)],
            check=True, capture_output=True,
        )  # fmt: skip
    else:
        shutil.copytree(source, dest, ignore=shutil.ignore_patterns(*EXCLUDE))
        git(dest, "init", "-q", "-b", "main")
    if setup is not None:
        apply_patch(dest, setup)
    if not clone or setup is not None:
        git(dest, "add", "-A")
        git(dest, "commit", "-q", "--allow-empty", "-m", "baseline")
    git(dest, "tag", BASE_REF)
    git(dest, "switch", "-q", "-c", branch)
    return dest


def is_git_root(path: Path) -> bool:
    return (path / ".git").exists()


def commit_all(ws: Path, message: str) -> str | None:
    """Commit whatever the agent changed; None when there was nothing to commit."""
    git(ws, "add", "-A")
    if not git(ws, "status", "--porcelain").strip():
        return None
    git(ws, "commit", "-q", "-m", message)
    return git(ws, "rev-parse", "--short", "HEAD").strip()


def diff(ws: Path, base: str = BASE_REF) -> str:
    return git(ws, "diff", f"{base}...HEAD")


def apply_patch(ws: Path, patch: Path) -> None:
    git(ws, "apply", "--whitespace=nowarn", str(patch.resolve()))
