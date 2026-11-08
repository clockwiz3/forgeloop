"""Where agent commands run: a long-lived Docker container per run, no network, the workspace mounted, .git read-only.

Docker never forwards the host environment unless asked (-e/--env-file), and we never ask: API keys stay outside.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DEFAULT_IMAGE = "forgeloop-sandbox:0.2"
DOCKERFILE = Path(__file__).resolve().parents[2] / "sandbox" / "Dockerfile"
WORKDIR = "/work"


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str
    timed_out: bool = False


class Sandbox(Protocol):
    async def exec(self, argv: list[str], timeout_s: float) -> ExecResult: ...

    async def close(self) -> None: ...


class SandboxError(RuntimeError):
    pass


async def _run(*argv: str, timeout_s: float = 120.0) -> ExecResult:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ExecResult(-1, "", timed_out=True)
    return ExecResult(proc.returncode or 0, out.decode("utf-8", errors="replace"))


def docker_available() -> bool:
    return shutil.which("docker") is not None


async def docker_ready() -> bool:
    if not docker_available():
        return False
    return (await _run("docker", "info", "--format", "{{.ServerVersion}}", timeout_s=20)).exit_code == 0


async def ensure_image(image: str = DEFAULT_IMAGE) -> None:
    if (await _run("docker", "image", "inspect", image, timeout_s=30)).exit_code == 0:
        return
    built = await _run("docker", "build", "-t", image, str(DOCKERFILE.parent), timeout_s=900)
    if built.exit_code != 0:
        raise SandboxError(f"failed to build {image}:\n{built.output[-3000:]}")


class DockerSandbox:
    def __init__(self, name: str, image: str) -> None:
        self.name = name
        self.image = image

    @classmethod
    async def start(
        cls,
        workspace: Path,
        *,
        image: str = DEFAULT_IMAGE,
        memory: str = "1g",
        cpus: str = "1",
        user: str | None = None,
    ) -> DockerSandbox:
        workspace = workspace.resolve()
        name = f"forgeloop-{uuid.uuid4().hex[:12]}"
        argv = [
            "docker", "run", "-d", "--rm",
            "--name", name,
            "--network", "none",
            "--memory", memory, "--cpus", cpus, "--pids-limit", "256",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--read-only", "--tmpfs", "/tmp:rw,size=256m",
            "-v", f"{workspace}:{WORKDIR}",
            "-w", WORKDIR,
        ]  # fmt: skip
        if (workspace / ".git").is_dir():
            argv += ["-v", f"{workspace / '.git'}:{WORKDIR}/.git:ro"]
        if user:
            argv += ["--user", user]
        argv += [image, "sleep", "infinity"]
        started = await _run(*argv, timeout_s=120)
        if started.exit_code != 0:
            raise SandboxError(f"failed to start sandbox: {started.output.strip()[-2000:]}")
        return cls(name, image)

    async def exec(self, argv: list[str], timeout_s: float) -> ExecResult:
        # `timeout` inside the container kills the process tree; the host-side limit is only a backstop.
        inner = ["timeout", "-k", "5", str(int(timeout_s)), *argv]
        result = await _run("docker", "exec", self.name, *inner, timeout_s=timeout_s + 30)
        if result.exit_code == 124:
            return ExecResult(124, result.output, timed_out=True)
        return result

    async def close(self) -> None:
        await _run("docker", "rm", "-f", self.name, timeout_s=60)
