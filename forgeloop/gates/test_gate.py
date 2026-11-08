"""The test gate: visible tests, lint, SAST on the added lines and an SCA check of new dependencies.

Tests, ruff and bandit run inside the sandbox. The dependency check runs on the host, because it needs the network,
and it never executes code from the repository: it parses dependency declarations and asks the OSV database.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from forgeloop import workspace
from forgeloop.gates import GateResult
from forgeloop.tools import Sandbox

OSV_URL = "https://api.osv.dev/v1/query"
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_REQ = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(==\s*([^\s;,]+))?")


def added_lines(diff: str) -> dict[str, set[int]]:
    """File -> line numbers (in the new version) added or changed by a unified diff with -U0."""
    out: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
            continue
        m = _HUNK.match(line)
        if m and current:
            start, count = int(m.group(1)), int(m.group(2) or "1")
            out.setdefault(current, set()).update(range(start, start + count))
    return out


def dependencies(pyproject: str) -> dict[str, str | None]:
    """Name -> pinned version (None when not pinned with ==)."""
    if not pyproject.strip():
        return {}
    data = tomllib.loads(pyproject)
    project: dict[str, Any] = data.get("project", {})
    specs: list[str] = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        specs += extra
    deps: dict[str, str | None] = {}
    for spec in specs:
        m = _REQ.match(spec)
        if m:
            deps[m.group(1).lower().replace("_", "-")] = m.group(4)
    return deps


async def osv_vulnerabilities(name: str, version: str, client: httpx.AsyncClient) -> list[str]:
    r = await client.post(OSV_URL, json={"package": {"name": name, "ecosystem": "PyPI"}, "version": version})
    r.raise_for_status()
    return [v.get("id", "?") for v in r.json().get("vulns", [])]


def requirements(text: str) -> dict[str, str | None]:
    """A requirements.txt: one spec per line; comments, options (-r, -e, --hash) and URLs are skipped."""
    deps: dict[str, str | None] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-") or "://" in line:
            continue
        m = _REQ.match(line)
        if m:
            deps[m.group(1).lower().replace("_", "-")] = m.group(4)
    return deps


REQUIREMENT_FILES = ("requirements.txt", "requirements-dev.txt", "requirements/base.txt", "requirements/dev.txt")


def _declared(read: Callable[[str], str | None]) -> dict[str, str | None]:
    """All dependencies declared in pyproject.toml and requirement files."""
    deps: dict[str, str | None] = {}
    text = read("pyproject.toml")
    if text:
        deps.update(dependencies(text))
    for name in REQUIREMENT_FILES:
        text = read(name)
        if text:
            deps.update(requirements(text))
    return deps


async def check_dependencies(ws: Path, base: str, *, transport: httpx.AsyncBaseTransport | None = None) -> list[str]:
    """Problems with dependencies added or changed since `base`: unpinned, vulnerable, or unverifiable."""

    def at_base(path: str) -> str | None:
        try:
            return workspace.git(ws, "show", f"{base}:{path}")
        except Exception:
            return None

    def now(path: str) -> str | None:
        p = ws / path
        return p.read_text(encoding="utf-8") if p.is_file() else None

    before, after = _declared(at_base), _declared(now)
    changed = {name: ver for name, ver in after.items() if name not in before or before[name] != ver}
    problems: list[str] = []
    async with httpx.AsyncClient(timeout=30, transport=transport) as client:
        for name, version in sorted(changed.items()):
            if version is None:
                problems.append(f"dependency {name} is added without a pinned version (use ==)")
                continue
            try:
                vulns = await osv_vulnerabilities(name, version, client)
            except httpx.HTTPError as exc:  # fail closed: an unverified dependency does not pass
                problems.append(f"dependency {name}=={version} could not be checked against OSV: {exc}")
                continue
            if vulns:
                problems.append(f"dependency {name}=={version} has known vulnerabilities: {', '.join(vulns)}")
    return problems


async def run_bandit(sandbox: Sandbox, ws: Path, base: str) -> list[str]:
    """Bandit findings (medium or high severity) on lines this change added."""
    touched = added_lines(workspace.git(ws, "diff", "-U0", f"{base}...HEAD", "--", "*.py"))
    files = [f for f in touched if (ws / f).is_file()]
    if not files:
        return []
    r = await sandbox.exec(["bandit", "-q", "-f", "json", "--severity-level", "medium", *files], 120)
    try:
        report = json.loads(r.output[r.output.index("{") :])
    except ValueError:
        return [f"bandit failed to run: {r.output[-500:]}"]
    findings = []
    for issue in report.get("results", []):
        if issue.get("line_number") in touched.get(issue.get("filename", ""), set()):
            findings.append(
                f"{issue['filename']}:{issue['line_number']}: [{issue['test_id']} {issue['issue_severity']}] "
                f"{issue['issue_text']}"
            )
    return findings


async def run_test_gate(
    sandbox: Sandbox,
    ws: Path,
    *,
    base: str = workspace.BASE_REF,
    tests_cmd: list[str] | None = None,
    lint_cmd: list[str] | None = None,
    sast: bool = True,
    test_timeout_s: int = 600,
    sca_transport: httpx.AsyncBaseTransport | None = None,
) -> GateResult:
    """Commands default to the bench project's; `lint_cmd=[]` switches linting off."""
    tests_cmd = tests_cmd or ["pytest", "-q", "--tb=short", "-p", "no:cacheprovider"]
    lint_cmd = ["ruff", "check", "."] if lint_cmd is None else lint_cmd
    tests = await sandbox.exec(tests_cmd, test_timeout_s)
    lint = await sandbox.exec(lint_cmd, 300) if lint_cmd else None
    findings = await run_bandit(sandbox, ws, base) if sast else []
    sca = await check_dependencies(ws, base, transport=sca_transport)

    checks = {"tests": tests.exit_code == 0 and not tests.timed_out}
    if lint is not None:
        checks["lint"] = lint.exit_code == 0
    if sast:
        checks["bandit"] = not findings
    checks["dependencies"] = not sca
    feedback: list[str] = []
    if not checks["tests"]:
        feedback.append(f"Tests fail ({' '.join(tests_cmd)}):\n" + tests.output[-4000:])
    if lint is not None and not checks["lint"]:
        feedback.append(f"Lint fails ({' '.join(lint_cmd)}):\n" + lint.output[-2000:])
    if findings:
        feedback.append("Security findings (bandit) in the lines you added:\n" + "\n".join(findings))
    if sca:
        feedback.append("Dependency problems:\n" + "\n".join(sca))
    failed = [name for name, ok in checks.items() if not ok]
    return GateResult(
        gate="test",
        passed=not failed,
        summary="all checks pass" if not failed else f"failed: {', '.join(failed)}",
        feedback="\n\n".join(feedback),
        data={"checks": checks, "bandit": findings, "dependencies": sca},
    )
