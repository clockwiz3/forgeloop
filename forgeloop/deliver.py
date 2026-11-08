"""Delivery to GitHub: push the run's branch and open a (draft) pull request.

This is the only step that writes outside the machine, so it never happens on its own: the CLI needs `--open-pr`
and the web UI needs a button press. It runs on the host with the user's own git and `gh` credentials; the agents
and their sandbox never see them.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from forgeloop import workspace
from forgeloop.orchestrator import RunState, Stage, render_pr

Runner = Callable[..., subprocess.CompletedProcess[str]]
GITHUB_BODY_LIMIT = 60_000
_GITHUB = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")


class DeliveryError(RuntimeError):
    pass


def github_repo(remote_url: str) -> str:
    m = _GITHUB.search(remote_url.strip())
    if not m:
        raise DeliveryError(f"origin is not a GitHub repository: {remote_url}")
    return f"{m['owner']}/{m['repo']}"


def _run(runner: Runner, argv: list[str], cwd: Path | None = None) -> str:
    out = runner(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise DeliveryError(f"{' '.join(argv[:3])} failed: {(out.stderr or out.stdout).strip()[-1500:]}")
    return str(out.stdout)


def open_pull_request(state: RunState, *, runner: Runner = subprocess.run) -> str:
    """Pushes the branch to the source repository's origin and opens a pull request; returns its URL."""
    if state.stage != Stage.DELIVERED:
        raise DeliveryError(f"only a delivered run becomes a pull request (this one is {state.stage.value})")
    if not state.cloned or not state.source_repo:
        raise DeliveryError("this run worked on a copy without git history; start it on a git repository root")
    if state.pr_url:
        return state.pr_url
    source, ws, deliver = Path(state.source_repo), state.ws, state.config.deliver
    origin = _run(runner, ["git", "remote", "get-url", "origin"], cwd=source).strip()
    repo = github_repo(origin)
    branch = workspace.git(ws, "branch", "--show-current").strip()
    if not workspace.git(ws, "log", "--oneline", f"{workspace.BASE_REF}..HEAD").strip():
        raise DeliveryError("the run has no commits to propose")
    _run(runner, ["git", "push", origin, f"HEAD:refs/heads/{branch}"], cwd=ws)

    body = render_pr(state, "")  # GitHub shows the diff itself
    body = body.split("<details><summary>Diff</summary>")[0].rstrip() + "\n\n_Opened by ForgeLoop._\n"
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body[:GITHUB_BODY_LIMIT])
        body_file = f.name
    argv: list[Any] = [
        "gh", "pr", "create", "--repo", repo, "--head", branch, "--base", deliver.base,
        "--title", state.task.title, "--body-file", body_file,
    ]  # fmt: skip
    if deliver.draft:
        argv.append("--draft")
    try:
        url = _run(runner, argv, cwd=ws).strip().splitlines()[-1]
    finally:
        Path(body_file).unlink(missing_ok=True)
    state.pr_url = url
    return url
