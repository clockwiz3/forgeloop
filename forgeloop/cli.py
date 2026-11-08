"""Command line: run a ticket through the pipeline, resume and inspect runs, validate the bench, run the eval."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from forgeloop import bench, intake, report
from forgeloop.deliver import DeliveryError, open_pull_request
from forgeloop.eval import CONFIGS, RESULTS, EvalSettings, RunRecord, load_records, run_eval
from forgeloop.orchestrator import Orchestrator, PipelineConfig, RunState, Stage
from forgeloop.project import ProjectConfig, ensure_project_image
from forgeloop.roles.planner import Plan
from forgeloop.store import RunStore
from forgeloop.tools.sandbox import docker_ready, ensure_image
from llmcore import LLMClient, Meter, PriceTable, ResponseCache, Tracer, load_dotenv

app = typer.Typer(no_args_is_help=True, add_completion=False)

DEFAULT_MODEL = "anthropic/claude-haiku-5.5"
DEFAULT_REVIEWER = "z-ai/glm-5.3-flash"
RUNS = Path("runs")


@app.callback()
def main() -> None:
    """ForgeLoop: ticket -> plan -> code -> gates -> PR, with an eval of what each gate buys."""
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy code page
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


async def _require_docker() -> None:
    if not await docker_ready():
        raise typer.BadParameter("Docker is not running: agents only execute commands inside the sandbox")
    await ensure_image()


def _client(budget_usd: float, tracer: Tracer) -> LLMClient:
    load_dotenv()
    return LLMClient(cache=ResponseCache(), meter=Meter(limit_usd=budget_usd), tracer=tracer, prices=PriceTable.load())


async def _approve_interactively(_: RunState, plan: Plan) -> bool:
    typer.echo(plan.render())
    return await asyncio.to_thread(typer.confirm, "\nApprove this plan?", default=True)


def _print_outcome(state: RunState) -> None:
    typer.echo(f"\nrun {state.run_id}: {state.stage.value}, ${state.cost_usd:.4f}")
    for g in state.gates:
        typer.echo(f"  gate {g.gate:7} {'pass' if g.passed else 'FAIL'}  {g.summary}")
    if state.escalation:
        typer.echo(f"  escalated: {state.escalation}")
    if state.question:
        typer.echo(f"  question for a human: {state.question}")
    if state.stage == Stage.DELIVERED:
        typer.echo(f"  PR description: {RUNS / state.run_id / 'PR.md'}")
    if state.pr_url:
        typer.echo(f"  pull request: {state.pr_url}")
    typer.echo(f"  workspace: {state.workspace}  (trace: forgeloop trace {state.run_id})")


@app.command()
def run(
    ticket: Annotated[Path | None, typer.Option(exists=True, dir_okay=False, help="Ticket in markdown")] = None,
    issue: Annotated[str | None, typer.Option(help="GitHub issue as owner/repo#number")] = None,
    repo: Annotated[
        Path, typer.Option(exists=True, file_okay=False, help="Repository to work on (never modified)")
    ] = Path("bench/bench-repo"),
    project_config: Annotated[
        Path | None,
        typer.Option(exists=True, dir_okay=False, help="forgeloop.toml to use instead of <repo>/forgeloop.toml"),
    ] = None,
    config: Annotated[str, typer.Option(help=f"Pipeline preset: {CONFIGS}")] = "full",
    model: Annotated[str | None, typer.Option(help="Implementer model (default: project config, then haiku)")] = None,
    planner_model: Annotated[str | None, typer.Option()] = None,
    reviewer_model: Annotated[str | None, typer.Option()] = None,
    approve_plan: Annotated[bool, typer.Option("--approve-plan", help="Ask a human to approve the plan")] = False,
    max_usd: Annotated[
        float | None, typer.Option(help="Budget for this run (default: project config, then $1)")
    ] = None,
    open_pr: Annotated[
        bool, typer.Option("--open-pr", help="When delivered, push the branch and open a draft PR")
    ] = False,
) -> None:
    """Run one ticket through the pipeline: plan, implement, gates, PR description (or escalation)."""
    if (ticket is None) == (issue is None):
        raise typer.BadParameter("pass exactly one of --ticket or --issue")
    if issue:
        owner_repo, _, number = issue.partition("#")
        task = intake.from_github(owner_repo, int(number))
    else:
        assert ticket is not None
        task = intake.from_markdown(ticket)
    project = ProjectConfig.load(repo, project_config)

    async def go() -> RunState:
        await _require_docker()
        if project.custom_image:
            typer.echo("preparing the project's sandbox image (built once per dependency change)...")
        image = await ensure_project_image(repo, project)
        pipeline = PipelineConfig.for_project(
            config, project, image, implementer_model=model, planner_model=planner_model,
            reviewer_model=reviewer_model, max_usd_per_run=max_usd, approve_plan=approve_plan,
        )  # fmt: skip
        tracer = Tracer(RUNS / "trace.sqlite")
        client = _client(pipeline.max_usd_per_run * 1.1, tracer)
        store = RunStore()
        orch = Orchestrator(
            client, store, tracer=tracer, approver=_approve_interactively, runs_dir=RUNS, log=typer.echo
        )
        try:
            state = orch.new_run(task, repo, pipeline)
            typer.echo(f"ticket: {task.title}")
            typer.echo(
                f"run {state.run_id}  config={config}  implementer={pipeline.implementer_model}  "
                f"reviewer={pipeline.reviewer_model}  sandbox={image}\n"
            )
            state = await orch.run(state)
        finally:
            await client.aclose()
        if open_pr and state.stage == Stage.DELIVERED:
            try:
                url = open_pull_request(state)
                store.save(state.run_id, state.task.id, state.stage.value, state.cost_usd, state)
                store.event(state.run_id, state.stage.value, "pull_request", {"url": url})
            except DeliveryError as exc:
                typer.echo(f"could not open a pull request: {exc}")
        return state

    _print_outcome(asyncio.run(go()))


@app.command("open-pr")
def open_pr_cmd(run_id: str) -> None:
    """Push a delivered run's branch and open a draft pull request on GitHub."""
    store = RunStore()
    state = RunState.model_validate_json(store.load(run_id))
    try:
        url = open_pull_request(state)
    except DeliveryError as exc:
        raise typer.BadParameter(str(exc)) from exc
    store.save(state.run_id, state.task.id, state.stage.value, state.cost_usd, state)
    store.event(state.run_id, state.stage.value, "pull_request", {"url": url})
    typer.echo(url)


@app.command()
def resume(run_id: str, max_usd: Annotated[float, typer.Option()] = 1.0) -> None:
    """Continue a run from the stage it stopped at."""

    async def go() -> RunState:
        await _require_docker()
        tracer = Tracer(RUNS / "trace.sqlite")
        client = _client(max_usd, tracer)
        orch = Orchestrator(
            client, RunStore(), tracer=tracer, approver=_approve_interactively, runs_dir=RUNS, log=typer.echo
        )
        try:
            state = orch.load(run_id)
            typer.echo(f"resuming {run_id} at {state.stage.value}")
            return await orch.run(state)
        finally:
            await client.aclose()

    _print_outcome(asyncio.run(go()))


@app.command()
def runs(limit: int = 20) -> None:
    """Recent runs."""
    for r in RunStore().runs(limit):
        typer.echo(f"{r['id']:40} {r['stage']:12} ${r['cost_usd']:.4f}  {r['updated_at']}")


@app.command()
def trace(run_id: str, spans: Annotated[bool, typer.Option(help="Also show model and tool spans")] = False) -> None:
    """What happened in a run: stage transitions, agents with cost, and optionally every model and tool call."""
    for e in RunStore().events(run_id):
        data = e["data"]
        if e["kind"] == "agent":
            line = f"agent {data['role']}: {data['status']}, {data['steps']} steps, ${data['cost_usd']:.4f}"
        elif e["kind"] == "stage_done":
            line = f"{data['stage']} -> {data['next']}"
        else:
            line = f"{e['kind']} {json.dumps(data, ensure_ascii=False)[:160]}"
        typer.echo(f"{e['ts']}  {line}")
    if spans:
        for s in Tracer(RUNS / "trace.sqlite").spans(run_id):
            a = s["attrs"]
            extra = f"${a.get('cost_usd', 0):.4f} in={a.get('input_tokens')}" if s["kind"] == "llm" else ""
            if s["kind"] == "tool":
                extra = json.dumps(a.get("args", {}), ensure_ascii=False)[:100]
            typer.echo(f"  {s['kind']:6} {s['name']:28} {s['duration_s'] or 0:6.2f}s {extra}")


@app.command()
def validate(
    task: Annotated[list[str] | None, typer.Option(help="Task ids (default: all)")] = None,
) -> None:
    """Check every task: hidden tests fail on the bare repo and pass (3x, no flakes) with the reference patch."""

    async def go() -> list[bench.Validation]:
        await _require_docker()
        return await bench.validate_all(bench.load_tasks(ids=task))

    results = asyncio.run(go())
    for v in results:
        typer.echo(f"{'OK  ' if v.ok else 'FAIL'} {v.task_id}")
        for p in v.problems:
            typer.echo("     " + p.replace("\n", "\n     "))
    bad = [v for v in results if not v.ok]
    typer.echo(f"{len(results) - len(bad)}/{len(results)} tasks valid")
    if bad:
        raise typer.Exit(1)


@app.command("eval")
def eval_(
    eval_id: Annotated[str, typer.Option("--id", help="Name of the eval; results go to evals/results/<id>.jsonl")],
    config: Annotated[list[str], typer.Option(help=f"Pipelines: {CONFIGS}")] = ["baseline"],  # noqa: B006
    task: Annotated[list[str] | None, typer.Option(help="Task ids (default: all)")] = None,
    repeats: Annotated[int, typer.Option(min=1)] = 1,
    model: Annotated[str, typer.Option(help="Implementer model")] = DEFAULT_MODEL,
    planner_model: Annotated[str, typer.Option()] = DEFAULT_MODEL,
    reviewer_model: Annotated[str, typer.Option()] = DEFAULT_REVIEWER,
    budget_usd: Annotated[float, typer.Option(help="Hard stop for the whole eval")] = 2.0,
    max_usd_per_run: Annotated[float, typer.Option()] = 1.0,
    concurrency: Annotated[int, typer.Option(min=1, max=8)] = 4,
) -> None:
    """Run tasks x configs x repeats. Re-running with the same --id resumes and skips finished runs."""
    unknown = set(config) - set(CONFIGS)
    if unknown:
        raise typer.BadParameter(f"unknown configs {sorted(unknown)}; available: {CONFIGS}")
    tasks = bench.load_tasks(ids=task)
    settings = EvalSettings(
        model=model, reviewer_model=reviewer_model, planner_model=planner_model, max_usd_per_run=max_usd_per_run
    )

    def show(r: RunRecord) -> None:
        mark = "PASS" if r.passed else "fail"
        typer.echo(f"{mark} {r.task_id:32} {r.config:10} #{r.repeat} {r.status:10} {r.steps:3} steps ${r.cost_usd:.4f}")

    async def go() -> list[RunRecord]:
        await _require_docker()
        tracer = Tracer(RUNS / "trace.sqlite")
        client = _client(budget_usd, tracer)
        try:
            return await run_eval(
                client, tracer, eval_id, tasks, config, repeats, settings, concurrency=concurrency, on_record=show
            )
        finally:
            typer.echo(f"spent ${client.meter.spent_usd:.4f} (cache saved ${client.meter.saved_usd:.4f})")
            await client.aclose()

    records = asyncio.run(go())
    typer.echo("\n" + report.summary_table(records))


@app.command("report")
def report_(eval_id: Annotated[str, typer.Option("--id")]) -> None:
    """Summary, gate contribution and per-task tables for an eval."""
    records = load_records(RESULTS / f"{eval_id}.jsonl")
    if not records:
        raise typer.BadParameter(f"no results for {eval_id}")
    typer.echo(report.summary_table(records))
    typer.echo("")
    typer.echo(report.delivery_table(records))
    typer.echo("")
    typer.echo(report.gates_table(records))
    typer.echo("")
    typer.echo(report.per_task_table(records))


@app.command()
def serve(
    port: Annotated[int, typer.Option()] = 8765,
    budget_usd: Annotated[float, typer.Option(help="Hard stop for everything started from this server session")] = 5.0,
) -> None:
    """Local web UI: start tickets, watch runs live, approve plans, browse evals. Listens on 127.0.0.1 only."""
    import os

    import uvicorn

    from forgeloop.web import create_app

    load_dotenv()
    asyncio.run(_require_docker())
    tracer = Tracer(RUNS / "trace.sqlite")
    client = _client(budget_usd, tracer)
    web = create_app(client, runs_dir=RUNS, tracer=tracer, api_key=os.environ.get("OPENROUTER_API_KEY"))
    typer.echo(f"ForgeLoop UI on http://127.0.0.1:{port}  (session budget ${budget_usd:.2f})")
    uvicorn.run(web, host="127.0.0.1", port=port, log_level="warning")


@app.command()
def doctor(
    repo: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    project_config: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
) -> None:
    """Check a repository is ready: build its sandbox, run its checks on a pristine copy, all before any agent."""
    import tempfile

    from forgeloop import workspace
    from forgeloop.gates.test_gate import run_test_gate
    from forgeloop.tools import DockerSandbox

    project = ProjectConfig.load(repo, project_config)
    checks = project.checks

    async def go() -> bool:
        await _require_docker()
        typer.echo("building the sandbox image (cached per dependency change)...")
        image = await ensure_project_image(repo, project)
        typer.echo(f"image: {image}")
        with tempfile.TemporaryDirectory(prefix="forgeloop-doctor-") as tmp:
            ws = workspace.prepare(repo, Path(tmp) / "ws", "doctor", clone=workspace.is_git_root(repo))
            box = await DockerSandbox.start(
                ws, image=image, user=project.sandbox.user if project.custom_image else None,
                memory=project.sandbox.memory, cpus=project.sandbox.cpus,
            )  # fmt: skip
            try:
                net = await box.exec(
                    ["python", "-c", "import socket; socket.create_connection(('1.1.1.1', 53), 3)"], 20
                )
                typer.echo(f"network isolated: {'yes' if net.exit_code != 0 else 'NO'}")
                for program in project.sandbox.programs:
                    found = await box.exec([program, "--version"], 60)
                    typer.echo(f"program {program}: {'ok' if found.exit_code == 0 else 'MISSING'}")
                gate = await run_test_gate(
                    box, ws, tests_cmd=checks.tests, lint_cmd=checks.lint or [], sast=checks.sast,
                    test_timeout_s=checks.test_timeout_s, extra=checks.extra,
                )  # fmt: skip
            finally:
                await box.close()
            workspace.remove(Path(tmp) / "ws")
        for name, ok in gate.data["checks"].items():
            typer.echo(f"check {name}: {'pass' if ok else 'FAIL'}")
        if not gate.passed:
            typer.echo("\nThe checks must pass on the untouched code, or every run would be sent back to fix them:\n")
            typer.echo(gate.feedback[-4000:])
        return gate.passed and net.exit_code != 0

    if not asyncio.run(go()):
        raise typer.Exit(1)
    typer.echo("\nready")
