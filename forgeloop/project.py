"""Per-repository configuration (`forgeloop.toml` in the target repo) and the project's sandbox image.

Without a config file a repository gets the defaults, which are exactly what the bench polygon needs.

Example:

    [sandbox]
    base_image = "python:3.12-slim"
    setup = ["pip install -e '.[dev]'", "pip install bandit"]   # or: dockerfile = "ci/forgeloop.Dockerfile"
    programs = ["pytest", "ruff", "python"]

    [checks]
    tests = ["pytest", "-q", "--tb=short", "-p", "no:cacheprovider"]
    lint = ["ruff", "check", "."]
    autofix = ["ruff", "check", "--fix", "--quiet", "."]
    sast = true

    [models]
    implementer = "anthropic/claude-haiku-5.5"

    [deliver]
    base = "main"
    draft = true

The image is built once from the pristine checkout, before any agent touches the code. Building is the only step
with network access, and it runs the repository's own setup commands: treat it like running the project's CI.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from forgeloop.tools.sandbox import DEFAULT_IMAGE, SandboxError, _run

CONFIG_FILE = "forgeloop.toml"
DEPENDENCY_FILES = (
    "pyproject.toml", "uv.lock", "poetry.lock", "requirements.txt", "requirements-dev.txt", "setup.py", "setup.cfg",
)  # fmt: skip


def clean_checkout(repo: Path, dest: Path) -> Path:
    """Tracked files only (git repos), or a copy without secrets and caches (plain directories)."""
    if (repo / ".git").exists():
        subprocess.run(["git", "clone", "-q", "--local", "--no-hardlinks", str(repo.resolve()), str(dest)], check=True)
        shutil.rmtree(dest / ".git", ignore_errors=True)
    else:
        from forgeloop.workspace import EXCLUDE

        shutil.copytree(repo, dest, ignore=shutil.ignore_patterns(*EXCLUDE))
    return dest


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo in forgeloop.toml is an error, not a silent default


class SandboxConfig(_Strict):
    image: str | None = None  # a prebuilt image; wins over everything else
    dockerfile: str | None = None  # path in the repo, built with the repo as context
    base_image: str = "python:3.12-slim"
    setup: list[str] = []  # shell commands run at build time (with network) after copying the repo in
    programs: list[str] = ["pytest", "python", "ruff"]  # what the agent may run with run_cmd
    user: str = "1000:1000"
    memory: str = "1g"
    cpus: str = "1"


class ChecksConfig(_Strict):
    tests: list[str] = ["pytest", "-q", "--tb=short", "-p", "no:cacheprovider"]
    lint: list[str] | None = ["ruff", "check", "."]
    autofix: list[str] | None = ["ruff", "check", "--fix", "--quiet", "."]
    sast: bool = True  # bandit on added lines; the image must contain bandit
    test_timeout_s: int = 600


class ModelsConfig(_Strict):
    implementer: str | None = None
    planner: str | None = None
    reviewer: str | None = None


class LimitsConfig(_Strict):
    max_usd_per_run: float | None = None
    max_steps: int | None = None


class DeliverConfig(_Strict):
    base: str = "main"
    draft: bool = True
    branch_prefix: str = "forgeloop/"


class ProjectConfig(_Strict):
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    checks: ChecksConfig = Field(default_factory=ChecksConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    deliver: DeliverConfig = Field(default_factory=DeliverConfig)

    @classmethod
    def load(cls, repo: Path, override: Path | None = None) -> ProjectConfig:
        """`override` lets a repository be configured without committing anything to it."""
        path = override or repo / CONFIG_FILE
        if not path.exists():
            if override:
                raise FileNotFoundError(override)
            return cls()
        return cls.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))

    @property
    def custom_image(self) -> bool:
        s = self.sandbox
        return bool(s.image or s.dockerfile or s.setup)


def generated_dockerfile(cfg: SandboxConfig) -> str:
    lines = [f"FROM {cfg.base_image}", "WORKDIR /work", "COPY . /work"]
    lines += [f"RUN {cmd}" for cmd in cfg.setup]
    lines += [
        "RUN chown -R 1000:1000 /work || true",
        "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1",
    ]
    return "\n".join(lines) + "\n"


def image_tag(repo: Path, cfg: SandboxConfig) -> str:
    """Content hash of everything that shapes the image: the recipe and the dependency declarations."""
    h = hashlib.sha256()
    recipe = (repo / cfg.dockerfile).read_text(encoding="utf-8") if cfg.dockerfile else generated_dockerfile(cfg)
    h.update(recipe.encode())
    for name in DEPENDENCY_FILES:
        p = repo / name
        if p.exists():
            h.update(name.encode() + b"\0" + p.read_bytes())
    return f"forgeloop-project:{h.hexdigest()[:16]}"


async def ensure_project_image(repo: Path, cfg: ProjectConfig, *, timeout_s: float = 1800) -> str:
    """The image tag to run agents in, building it if needed. Defaults to the bench image."""
    s = cfg.sandbox
    if s.image:
        return s.image
    if not cfg.custom_image:
        return DEFAULT_IMAGE
    tag = image_tag(repo, s)
    if (await _run("docker", "image", "inspect", tag, timeout_s=30)).exit_code == 0:
        return tag
    with tempfile.TemporaryDirectory(prefix="forgeloop-build-") as tmp:
        # Build from a clean checkout of tracked files: no .git, no .env, nothing untracked ends up in the image.
        context = clean_checkout(repo, Path(tmp) / "context")
        if s.dockerfile:
            dockerfile = context / s.dockerfile
        else:
            dockerfile = Path(tmp) / "Dockerfile"
            dockerfile.write_text(generated_dockerfile(s), encoding="utf-8")
        built = await _run("docker", "build", "-t", tag, "-f", str(dockerfile), str(context), timeout_s=timeout_s)
    if built.exit_code != 0:
        raise SandboxError(f"building the project sandbox image failed:\n{built.output[-4000:]}")
    return tag
