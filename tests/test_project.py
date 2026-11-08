import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from forgeloop import workspace
from forgeloop.deliver import DeliveryError, github_repo, open_pull_request
from forgeloop.gates.test_gate import requirements, run_test_gate
from forgeloop.intake import Task
from forgeloop.orchestrator import PipelineConfig, RunState, Stage
from forgeloop.project import ProjectConfig, generated_dockerfile, image_tag
from forgeloop.tools import ExecResult

TOML = """
[sandbox]
base_image = "python:3.12-slim"
setup = ["pip install -e ."]
programs = ["pytest", "python"]
memory = "2g"

[checks]
tests = ["pytest", "-q", "tests"]
lint = []
sast = false

[models]
implementer = "google/gemini-2.5-flash-lite"

[limits]
max_usd_per_run = 0.3

[deliver]
base = "dev"
"""


def _git_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "app.py").write_text("X = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = ["httpx==0.27.0"]\n', encoding="utf-8")
    workspace.git(root, "init", "-q", "-b", "main")
    workspace.git(root, "add", "-A")
    workspace.git(root, "commit", "-q", "-m", "init")
    (root / ".env").write_text("OPENROUTER_API_KEY=sk-secret\n", encoding="utf-8")  # untracked, like in real life
    return root


def test_project_config_is_strict_and_can_live_outside_the_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert ProjectConfig.load(repo) == ProjectConfig()  # no file: bench defaults
    outside = tmp_path / "cfg.toml"
    outside.write_text(TOML, encoding="utf-8")
    cfg = ProjectConfig.load(repo, outside)
    assert cfg.custom_image and cfg.checks.lint == [] and cfg.deliver.base == "dev"
    (repo / "forgeloop.toml").write_text("[sandbox]\nimgae = 'typo'\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        ProjectConfig.load(repo)


def test_pipeline_takes_project_defaults_and_explicit_flags_win(tmp_path: Path) -> None:
    cfg = ProjectConfig.model_validate(__import__("tomllib").loads(TOML))
    p = PipelineConfig.for_project("full", cfg, "img:1", implementer_model=None, reviewer_model="r/m")
    assert p.implementer_model == "google/gemini-2.5-flash-lite" and p.reviewer_model == "r/m"
    assert p.max_usd_per_run == 0.3 and p.sandbox_image == "img:1" and p.sandbox_user == "1000:1000"
    assert p.programs == ["pytest", "python"] and p.sandbox_memory == "2g" and p.checks.sast is False


def test_image_tag_follows_recipe_and_dependency_files(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    cfg = ProjectConfig().sandbox.model_copy(update={"setup": ["pip install -e ."]})
    tag = image_tag(repo, cfg)
    assert tag == image_tag(repo, cfg)
    (repo / "app.py").write_text("X = 2\n", encoding="utf-8")
    assert image_tag(repo, cfg) == tag  # source changes are mounted at run time, no rebuild
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = ["httpx==0.28.0"]\n', encoding="utf-8")
    assert image_tag(repo, cfg) != tag
    assert "RUN pip install -e ." in generated_dockerfile(cfg)


def test_clone_mode_keeps_history_and_leaves_untracked_secrets_behind(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    ws = workspace.prepare(repo, tmp_path / "ws", "forgeloop/t1", clone=True)
    assert not (ws / ".env").exists() and (ws / "app.py").exists()
    assert workspace.git(ws, "rev-parse", workspace.BASE_REF) == workspace.git(repo, "rev-parse", "HEAD")
    (ws / "app.py").write_text("X = 3\n", encoding="utf-8")
    workspace.commit_all(ws, "change")
    assert "+X = 3" in workspace.diff(ws)
    assert workspace.git(repo, "rev-parse", "HEAD") == workspace.git(ws, "rev-parse", workspace.BASE_REF)  # untouched


def _delivered(tmp_path: Path) -> RunState:
    repo = _git_repo(tmp_path / "repo")
    ws = workspace.prepare(repo, tmp_path / "ws", "forgeloop/fix-1", clone=True)
    (ws / "app.py").write_text("X = 3\n", encoding="utf-8")
    workspace.commit_all(ws, "change")
    task = Task(id="fix-1", title="Fix X", body="X must be 3", source="file")
    return RunState(
        run_id="fix-1", task=task, config=PipelineConfig(), workspace=str(ws), source_repo=str(repo), cloned=True,
        stage=Stage.DELIVERED,
    )  # fmt: skip


def test_pull_request_pushes_the_branch_and_opens_a_draft(tmp_path: Path) -> None:
    state = _delivered(tmp_path)
    calls: list[list[str]] = []

    def runner(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        out = {
            "remote": "git@github.com:clockwiz3/menubench.git\n",
            "pr": "https://github.com/clockwiz3/menubench/pull/7\n",
        }
        key = "remote" if argv[:2] == ["git", "remote"] else "pr" if argv[0] == "gh" else ""
        return subprocess.CompletedProcess(argv, 0, out.get(key, ""), "")

    url = open_pull_request(state, runner=runner)
    assert url.endswith("/pull/7") and state.pr_url == url
    push, gh = calls[1], calls[2]
    assert push == ["git", "push", "git@github.com:clockwiz3/menubench.git", "HEAD:refs/heads/forgeloop/fix-1"]
    assert gh[:3] == ["gh", "pr", "create"] and "--draft" in gh and gh[gh.index("--repo") + 1] == "clockwiz3/menubench"
    assert gh[gh.index("--base") + 1] == "main" and gh[gh.index("--title") + 1] == "Fix X"
    assert open_pull_request(state, runner=runner) == url and len(calls) == 3  # idempotent


def test_pull_request_refuses_runs_that_are_not_ready(tmp_path: Path) -> None:
    state = _delivered(tmp_path)
    for broken in (state.model_copy(update={"stage": Stage.ESCALATED}), state.model_copy(update={"cloned": False})):
        with pytest.raises(DeliveryError):
            open_pull_request(broken, runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    with pytest.raises(DeliveryError):
        github_repo("https://gitlab.com/a/b.git")
    assert github_repo("https://github.com/a/b.git") == "a/b"


def test_test_gate_uses_project_commands(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo")
    ws = workspace.prepare(repo, tmp_path / "ws", "w", clone=True)
    seen: list[list[str]] = []

    class Box:
        async def exec(self, argv: list[str], timeout_s: float) -> ExecResult:
            seen.append(argv)
            return ExecResult(0, "ok")

        async def close(self) -> None:
            return None

    gate = asyncio.run(run_test_gate(Box(), ws, tests_cmd=["pytest", "-q", "tests"], lint_cmd=[], sast=False))
    assert gate.passed and seen == [["pytest", "-q", "tests"]]
    assert set(gate.data["checks"]) == {"tests", "dependencies"}


def test_requirements_files_are_parsed() -> None:
    text = "# deps\nrequests==2.32.0\n-r base.txt\nnumpy>=2\ngit+https://x/y.git\nPyYAML==6.0.1  # yaml\n"
    assert requirements(text) == {"requests": "2.32.0", "numpy": None, "pyyaml": "6.0.1"}
