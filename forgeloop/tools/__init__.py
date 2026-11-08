"""Agent tools: file access with a path policy, commands in a network-less sandbox."""

from __future__ import annotations

from forgeloop.tools.base import PathPolicy, Tool, Toolbox, ToolError, Workspace, truncate
from forgeloop.tools.exec import ALLOWED_PROGRAMS, exec_tools
from forgeloop.tools.fs import fs_tools
from forgeloop.tools.sandbox import DockerSandbox, ExecResult, Sandbox, SandboxError


def default_toolbox(
    ws: Workspace,
    sandbox: Sandbox,
    *,
    programs: frozenset[str] = ALLOWED_PROGRAMS,
    test_command: list[str] | None = None,
    test_timeout_s: int = 180,
) -> Toolbox:
    return Toolbox(
        [
            *fs_tools(ws),
            *exec_tools(ws, sandbox, programs=programs, test_command=test_command, test_timeout_s=test_timeout_s),
        ]
    )


__all__ = [
    "ALLOWED_PROGRAMS",
    "DockerSandbox",
    "ExecResult",
    "PathPolicy",
    "Sandbox",
    "SandboxError",
    "Tool",
    "ToolError",
    "Toolbox",
    "Workspace",
    "default_toolbox",
    "exec_tools",
    "fs_tools",
    "truncate",
]
