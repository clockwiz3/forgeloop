"""Local web UI: start tickets, watch runs live, approve plans, browse traces and eval results.

Bound to localhost by design. Even there a browser can be tricked into talking to it, so:
- requests must carry a localhost Host header (blocks DNS rebinding from other sites);
- state-changing requests must carry the `X-ForgeLoop` header, which a cross-site form or image cannot set (CSRF);
- the API key and other secrets never leave the server; ticket text is rendered as text by the frontend (XSS).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from forgeloop import bench, intake, report, workspace
from forgeloop.deliver import DeliveryError, open_pull_request
from forgeloop.eval import CONFIGS, RESULTS, load_records
from forgeloop.orchestrator import TERMINAL, Orchestrator, PipelineConfig, RunState
from forgeloop.project import ProjectConfig, ensure_project_image
from forgeloop.roles.planner import Plan
from forgeloop.store import RunStore
from forgeloop.tools import Sandbox
from llmcore import LLMClient, Tracer

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}
CSRF_HEADER = "x-forgeloop"


class RunRequest(BaseModel):
    source: Literal["text", "bench", "github"]
    title: str = ""
    body: str = ""
    task_id: str = ""  # bench task
    issue: str = ""  # owner/repo#number
    repo: str = "bench/bench-repo"
    project_config: str = ""  # forgeloop.toml outside the repo; empty: <repo>/forgeloop.toml or defaults
    config: str = "full"
    implementer_model: str | None = None  # None: the project's default, then haiku
    planner_model: str | None = None
    reviewer_model: str | None = None
    max_usd: float | None = Field(None, gt=0, le=5)
    approve_plan: bool = False


class Decision(BaseModel):
    approve: bool


@dataclass
class RunManager:
    """Runs pipelines as background tasks in this process and brokers human plan approvals."""

    client: LLMClient
    store: RunStore
    tracer: Tracer
    runs_dir: Path
    sandbox_factory: Callable[[Path], Awaitable[Sandbox]] | None = None
    tasks: dict[str, asyncio.Task[RunState]] = field(default_factory=dict)
    approvals: dict[str, asyncio.Future[bool]] = field(default_factory=dict)

    def orchestrator(self) -> Orchestrator:
        kw: dict[str, Any] = {"sandbox_factory": self.sandbox_factory} if self.sandbox_factory else {}
        return Orchestrator(
            self.client, self.store, tracer=self.tracer, approver=self._wait_for_human, runs_dir=self.runs_dir, **kw
        )

    async def _wait_for_human(self, state: RunState, _: Plan) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.approvals[state.run_id] = future
        try:
            return await future
        finally:
            self.approvals.pop(state.run_id, None)

    def start(self, state: RunState) -> None:
        orch = self.orchestrator()

        async def go() -> RunState:
            try:
                return await orch.run(state)
            except asyncio.CancelledError:
                self.store.event(state.run_id, state.stage.value, "cancelled", {})
                raise
            except Exception as exc:
                self.store.event(state.run_id, state.stage.value, "crashed", {"error": f"{type(exc).__name__}: {exc}"})
                raise

        task = asyncio.create_task(go(), name=state.run_id)
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # keep failures from being "never retrieved"
        self.tasks[state.run_id] = task

    def phase(self, run_id: str, stage: str) -> str:
        if run_id in self.approvals:
            return "awaiting_approval"
        task = self.tasks.get(run_id)
        if task is not None and not task.done():
            return "running"
        return "finished" if stage in {s.value for s in TERMINAL} else "paused"


def _check_host(request: Request) -> None:
    header = request.headers.get("host", "")
    host = header.split("]")[0] + "]" if header.startswith("[") else header.rsplit(":", 1)[0]
    if host not in LOCAL_HOSTS:
        raise HTTPException(403, "this server only answers on localhost")


def create_app(
    client: LLMClient,
    *,
    runs_dir: Path = Path("runs"),
    store: RunStore | None = None,
    tracer: Tracer | None = None,
    sandbox_factory: Callable[[Path], Awaitable[Sandbox]] | None = None,
    results_dir: Path = RESULTS,
    api_key: str | None = None,
    image_resolver: Callable[[Path, ProjectConfig], Awaitable[str]] | None = None,
    deliver_pr: Callable[[RunState], str] = open_pull_request,
) -> FastAPI:
    manager = RunManager(
        client=client,
        store=store or RunStore(runs_dir / "forgeloop.sqlite"),
        tracer=tracer or Tracer(runs_dir / "trace.sqlite"),
        runs_dir=runs_dir,
        sandbox_factory=sandbox_factory,
    )
    app = FastAPI(title="ForgeLoop", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager
    balance_cache: dict[str, Any] = {}

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        try:
            _check_host(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"detail": f"missing {CSRF_HEADER} header"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        )
        return response

    def load(run_id: str) -> RunState:
        try:
            return manager.orchestrator().load(run_id)
        except KeyError as exc:
            raise HTTPException(404, f"no run {run_id}") from exc

    def view(state: RunState) -> dict[str, Any]:
        pr = runs_dir / state.run_id / "PR.md"
        return {
            "state": json.loads(state.model_dump_json()),
            "phase": manager.phase(state.run_id, state.stage.value),
            "has_pr": pr.exists(),
            "workspace_exists": state.ws.exists(),
        }

    # --- runs ---------------------------------------------------------------------------------------------------

    @app.get("/api/runs")
    async def list_runs(limit: int = 50) -> list[dict[str, Any]]:
        return [{**r, "phase": manager.phase(r["id"], r["stage"])} for r in manager.store.runs(limit)]

    @app.post("/api/runs", status_code=201)
    async def create_run(req: RunRequest) -> dict[str, Any]:
        if req.config not in CONFIGS:
            raise HTTPException(422, f"config must be one of {CONFIGS}")
        repo, setup = Path(req.repo), None
        if req.source == "text":
            if not req.title.strip() or not req.body.strip():
                raise HTTPException(422, "title and body are required")
            slug = "".join(ch if ch.isalnum() else "-" for ch in req.title.lower()).strip("-")[:40] or "ticket"
            task = intake.Task(id=slug, title=req.title.strip(), body=req.body, source="file")
        elif req.source == "bench":
            try:
                (bt,) = bench.load_tasks(ids=[req.task_id])
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc
            task = intake.Task(id=bt.id, title=bt.meta.title, body=bt.ticket, source="file")
            repo, setup = bench.REPO, bt.setup
        else:
            owner_repo, _, number = req.issue.partition("#")
            if not owner_repo or not number.isdigit():
                raise HTTPException(422, "issue must look like owner/repo#123")
            try:
                task = await asyncio.to_thread(intake.from_github, owner_repo, int(number))
            except Exception as exc:
                raise HTTPException(502, f"could not read the issue: {exc}") from exc
        if not repo.is_dir():
            raise HTTPException(422, f"repository not found: {repo}")
        try:
            project = ProjectConfig.load(repo, Path(req.project_config) if req.project_config else None)
        except Exception as exc:
            raise HTTPException(422, f"bad project config: {exc}") from exc
        try:
            image = await (image_resolver or ensure_project_image)(repo, project)
        except Exception as exc:
            raise HTTPException(500, f"could not prepare the sandbox image (try `forgeloop doctor`): {exc}") from exc
        config = PipelineConfig.for_project(
            req.config, project, image, implementer_model=req.implementer_model, planner_model=req.planner_model,
            reviewer_model=req.reviewer_model, max_usd_per_run=req.max_usd, approve_plan=req.approve_plan,
        )  # fmt: skip
        state = await asyncio.to_thread(manager.orchestrator().new_run, task, repo, config, setup=setup)
        manager.start(state)
        return view(state)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        return view(load(run_id))

    @app.post("/api/runs/{run_id}/approve")
    async def approve(run_id: str, decision: Decision) -> dict[str, Any]:
        future = manager.approvals.get(run_id)
        if future is None or future.done():
            raise HTTPException(409, "this run is not waiting for a plan decision")
        future.set_result(decision.approve)
        return {"ok": True}

    @app.post("/api/runs/{run_id}/open-pr")
    async def open_pr(run_id: str) -> dict[str, Any]:
        """Pushes the branch and opens a draft pull request. Only ever on an explicit request from the user."""
        state = load(run_id)
        try:
            url = await asyncio.to_thread(deliver_pr, state)
        except DeliveryError as exc:
            raise HTTPException(409, str(exc)) from exc
        manager.store.save(state.run_id, state.task.id, state.stage.value, state.cost_usd, state)
        manager.store.event(state.run_id, state.stage.value, "pull_request", {"url": url})
        return {"url": url}

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: str) -> dict[str, Any]:
        task = manager.tasks.get(run_id)
        if task is None or task.done():
            raise HTTPException(409, "this run is not running")
        task.cancel()
        return {"ok": True}

    @app.post("/api/runs/{run_id}/resume")
    async def resume(run_id: str) -> dict[str, Any]:
        state = load(run_id)
        if manager.phase(run_id, state.stage.value) != "paused":
            raise HTTPException(409, "only a paused run can be resumed")
        manager.start(state)
        return view(state)

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request) -> StreamingResponse:
        """Server-sent events: the run's event log as it grows, then `end` when the run stops."""
        load(run_id)

        async def stream() -> AsyncIterator[str]:
            seen = 0
            idle_since = time.monotonic()
            while not await request.is_disconnected():
                batch = manager.store.events(run_id)[seen:]
                for e in batch:
                    yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
                seen += len(batch)
                state = manager.orchestrator().load(run_id)
                phase = manager.phase(run_id, state.stage.value)
                if phase in ("finished", "paused") and not batch:
                    yield f"event: end\ndata: {json.dumps({'phase': phase})}\n\n"
                    return
                if batch:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since > 15:
                    yield ": keep-alive\n\n"
                    idle_since = time.monotonic()
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/runs/{run_id}/pr")
    async def pr(run_id: str) -> Response:
        path = runs_dir / run_id / "PR.md"
        if not path.exists():
            raise HTTPException(404, "no PR description yet")
        return Response(path.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")

    @app.get("/api/runs/{run_id}/diff")
    async def diff(run_id: str) -> Response:
        state = load(run_id)
        if not state.ws.exists():
            raise HTTPException(404, "the workspace no longer exists")
        text = await asyncio.to_thread(workspace.diff, state.ws)
        return Response(text, media_type="text/plain; charset=utf-8")

    @app.get("/api/runs/{run_id}/trace")
    async def trace(run_id: str) -> list[dict[str, Any]]:
        load(run_id)
        return manager.tracer.spans(run_id)

    # --- bench, evals, account ----------------------------------------------------------------------------------

    @app.get("/api/tasks")
    async def tasks() -> list[dict[str, Any]]:
        return [t.meta.model_dump() for t in bench.load_tasks()]

    @app.get("/api/evals")
    async def evals() -> list[dict[str, Any]]:
        out = []
        for path in sorted(results_dir.glob("*.jsonl")):
            if path.name.endswith(".superseded.jsonl"):
                continue
            records = load_records(path)
            if records:
                out.append(
                    {"id": path.stem, "runs": len(records), "model": records[0].model,
                     "configs": sorted({r.config for r in records}),
                     "usd": round(sum(r.cost_usd for r in records), 4)}
                )  # fmt: skip
        return out

    @app.get("/api/evals/{eval_id}")
    async def eval_detail(eval_id: str) -> dict[str, Any]:
        path = (results_dir / f"{eval_id}.jsonl").resolve()
        if path.parent != results_dir.resolve() or not path.exists():
            raise HTTPException(404, f"no eval {eval_id}")
        return {"id": eval_id, **report.overview(load_records(path))}

    @app.get("/api/account")
    async def account() -> dict[str, Any]:
        """Session spend and the OpenRouter balance (the key itself never leaves the server)."""
        meter = client.meter
        out: dict[str, Any] = {
            "session_spent_usd": meter.spent_usd,
            "session_limit_usd": meter.limit_usd,
            "cache_saved_usd": meter.saved_usd,
            "defaults": RunRequest.model_json_schema()["properties"],
            "configs": CONFIGS,
        }
        if api_key:
            if not balance_cache or time.monotonic() - balance_cache["at"] > 60:
                try:
                    async with httpx.AsyncClient(timeout=10) as http:
                        r = await http.get(
                            "https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {api_key}"}
                        )
                    data = r.json().get("data", {})
                    balance_cache.update(at=time.monotonic(), balance=data["total_credits"] - data["total_usage"])
                except Exception:
                    balance_cache.update(at=time.monotonic(), balance=None)
            out["balance_usd"] = balance_cache.get("balance")
        return out

    # --- frontend -----------------------------------------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app
