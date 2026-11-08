"""The pipeline as an explicit state machine: intake -> plan -> plan gate -> implement -> test gate -> review gate ->
deliver, or escalate to a human.

Each stage reads and writes `RunState`; the state is persisted after every transition, so a run can be resumed from
the stage it stopped at. Gates are transitions, not suggestions: there is no path to DELIVER that skips an enabled
gate. Stages can be switched off by configuration, which is how the eval measures what each one adds.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from forgeloop import workspace
from forgeloop.agent_loop import AgentConfig, AgentLoop, AgentResult, FinishTool
from forgeloop.gates import GateResult
from forgeloop.gates.plan_gate import check_plan
from forgeloop.gates.test_gate import run_test_gate
from forgeloop.intake import Task
from forgeloop.project import ChecksConfig, DeliverConfig, ProjectConfig
from forgeloop.roles import implementer, planner, reviewer
from forgeloop.roles.planner import Plan
from forgeloop.roles.reviewer import Review
from forgeloop.store import RunStore
from forgeloop.tools import DockerSandbox, Sandbox, Workspace, default_toolbox
from forgeloop.tools.sandbox import DEFAULT_IMAGE
from llmcore import LLMClient, Tracer


class Stage(StrEnum):
    PLAN = "plan"
    PLAN_GATE = "plan_gate"
    IMPLEMENT = "implement"
    TEST_GATE = "test_gate"
    REVIEW_GATE = "review_gate"
    DELIVER = "deliver"
    DELIVERED = "delivered"
    ESCALATED = "escalated"


TERMINAL = {Stage.DELIVERED, Stage.ESCALATED}


class PipelineConfig(BaseModel):
    plan: bool = True
    test_gate: bool = True
    review_gate: bool = True
    approve_plan: bool = False
    implementer_model: str = "anthropic/claude-haiku-5.5"
    planner_model: str = "anthropic/claude-haiku-5.5"
    reviewer_model: str = "z-ai/glm-5.3-flash"  # a different model than the implementer
    max_plan_attempts: int = 2
    max_checkpoint_attempts: int = 2
    max_test_rounds: int = 3
    max_review_rounds: int = 2
    max_usd_per_agent: float = 0.40
    max_usd_per_run: float = 1.00
    max_steps: int = 40
    reviewer_max_steps: int = 20
    reviewer_reasoning: str | None = "low"  # reasoning models otherwise spend the output budget thinking
    temperature: float = 0.2
    variant: int = 0
    # the project's sandbox and checks (forgeloop.toml); defaults are the bench polygon's
    sandbox_image: str = DEFAULT_IMAGE
    sandbox_user: str | None = None
    sandbox_memory: str = "1g"
    sandbox_cpus: str = "1"
    programs: list[str] = ["pytest", "python", "ruff"]
    checks: ChecksConfig = ChecksConfig()
    deliver: DeliverConfig = DeliverConfig()

    @classmethod
    def preset(cls, name: str, **overrides: Any) -> PipelineConfig:
        flags = {
            "baseline": {"plan": False, "test_gate": False, "review_gate": False},
            "plan": {"plan": True, "test_gate": False, "review_gate": False},
            "test-gate": {"plan": True, "test_gate": True, "review_gate": False},
            "full": {"plan": True, "test_gate": True, "review_gate": True},
        }
        if name not in flags:
            raise ValueError(f"unknown preset {name!r}; available: {sorted(flags)}")
        return cls(**{**flags[name], **overrides})

    @classmethod
    def for_project(cls, name: str, project: ProjectConfig, image: str, **overrides: Any) -> PipelineConfig:
        """A preset with the project's sandbox, checks and defaults; explicit overrides (not None) win."""
        base: dict[str, Any] = {
            "sandbox_image": image,
            "sandbox_user": project.sandbox.user if project.custom_image else None,
            "sandbox_memory": project.sandbox.memory,
            "sandbox_cpus": project.sandbox.cpus,
            "programs": project.sandbox.programs,
            "checks": project.checks,
            "deliver": project.deliver,
        }
        for key, value in (
            ("implementer_model", project.models.implementer),
            ("planner_model", project.models.planner),
            ("reviewer_model", project.models.reviewer),
            ("max_usd_per_run", project.limits.max_usd_per_run),
            ("max_steps", project.limits.max_steps),
        ):
            if value is not None:
                base[key] = value
        base.update({k: v for k, v in overrides.items() if v is not None})
        return cls.preset(name, **base)


class AgentSummary(BaseModel):
    role: str
    status: str
    steps: int
    cost_usd: float
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int


class RunState(BaseModel):
    run_id: str
    task: Task
    config: PipelineConfig
    stage: Stage = Stage.PLAN
    workspace: str
    source_repo: str | None = None  # the repository the run was cloned from (for opening a pull request)
    cloned: bool = False
    pr_url: str | None = None
    plan: Plan | None = None
    plan_attempts: int = 0
    plan_feedback: str | None = None
    checkpoints_done: int = 0
    checkpoint_attempts: int = 0  # attempts at the current checkpoint
    unfinished_status: str | None = None  # how the previous attempt at the current checkpoint ended
    test_rounds: int = 0
    review_rounds: int = 0
    gates: list[GateResult] = []
    agents: list[AgentSummary] = []
    reviews: list[Review] = []
    cost_usd: float = 0.0
    escalation: str | None = None  # why a human is needed
    idle_fixes: int = 0  # fix rounds that changed nothing
    last_fix_idle: bool = False
    question: str | None = None  # an agent's question for an ambiguous ticket

    @property
    def ws(self) -> Path:
        return Path(self.workspace)


Observer = Callable[[str, RunState], Awaitable[None]]  # (event, state): eval hooks, e.g. grade after each gate
Approver = Callable[[RunState, Plan], Awaitable[bool]]  # a human decision; may wait (CLI prompt, web button)


@dataclass
class Orchestrator:
    client: LLMClient
    store: RunStore
    tracer: Tracer | None = None
    observer: Observer | None = None
    approver: Approver | None = None
    log: Callable[[str], None] | None = None  # human-readable progress, e.g. for the CLI
    runs_dir: Path = Path("runs")
    sandbox_factory: Callable[[Path], Awaitable[Sandbox]] | None = None  # tests inject fakes; default is Docker

    def new_run(
        self,
        task: Task,
        repo: Path,
        config: PipelineConfig,
        *,
        setup: Path | None = None,
        run_id: str | None = None,
        clone: bool | None = None,
    ) -> RunState:
        """`clone` defaults to True for a git repository root, so the result can become a pull request."""
        run_id = run_id or f"{task.id}-{uuid.uuid4().hex[:8]}"
        clone = workspace.is_git_root(repo) if clone is None else clone
        branch = f"{config.deliver.branch_prefix}{run_id[:60]}" if clone else f"forgeloop/{task.id[:40]}"
        ws = workspace.prepare(repo, self.runs_dir / run_id / "workspace", branch, setup=setup, clone=clone)
        state = RunState(
            run_id=run_id, task=task, config=config, workspace=str(ws), source_repo=str(repo.resolve()), cloned=clone
        )
        state.stage = Stage.PLAN if config.plan else Stage.IMPLEMENT
        self._save(state, "created", {"task": task.id, "config": config.model_dump()})
        return state

    def load(self, run_id: str) -> RunState:
        return RunState.model_validate_json(self.store.load(run_id))

    async def run(self, state: RunState) -> RunState:
        """Advance until delivered or escalated. Safe to call again on a loaded state (resume)."""
        cfg = state.config
        if self.sandbox_factory is not None:
            sandbox = await self.sandbox_factory(state.ws)
        else:
            sandbox = await DockerSandbox.start(
                state.ws, image=cfg.sandbox_image, user=cfg.sandbox_user, memory=cfg.sandbox_memory,
                cpus=cfg.sandbox_cpus,
            )  # fmt: skip
        handlers: dict[Stage, Callable[[RunState, Sandbox], Awaitable[None]]] = {
            Stage.PLAN: self._plan,
            Stage.PLAN_GATE: self._plan_gate,
            Stage.IMPLEMENT: self._implement,
            Stage.TEST_GATE: self._test_gate,
            Stage.REVIEW_GATE: self._review_gate,
            Stage.DELIVER: self._deliver,
        }
        try:
            while state.stage not in TERMINAL:
                stage = state.stage
                if stage != Stage.DELIVER and state.cost_usd >= state.config.max_usd_per_run:
                    self._escalate(state, f"run budget of ${state.config.max_usd_per_run:.2f} exhausted")
                    continue
                if self.tracer:
                    with self.tracer.span("stage", stage.value, run_id=state.run_id):
                        await handlers[stage](state, sandbox)
                else:
                    await handlers[stage](state, sandbox)
                self._save(state, "stage_done", {"stage": stage.value, "next": state.stage.value})
        finally:
            await sandbox.close()
        return state

    # --- stages -------------------------------------------------------------------------------------------------

    async def _plan(self, state: RunState, sandbox: Sandbox) -> None:
        cfg = state.config
        state.plan_attempts += 1
        result = await self._agent(
            state,
            sandbox,
            role="planner",
            model=cfg.planner_model,
            system=planner.SYSTEM,
            task=planner.task_prompt(state.task.ticket, state.plan_feedback),
            read_only=True,
            finish=planner.FINISH,
        )
        if result.status == "escalated":
            state.question = result.question
            self._escalate(state, "the planner needs clarification")
        elif result.status != "done" or result.output is None:
            self._escalate(state, f"planning failed: {result.status} {result.error or ''}".strip())
        else:
            state.plan = Plan.model_validate(result.output)
            state.stage = Stage.PLAN_GATE

    async def _plan_gate(self, state: RunState, sandbox: Sandbox) -> None:
        assert state.plan is not None
        gate = check_plan(state.plan, Workspace(state.ws))
        state.gates.append(gate)
        if not gate.passed:
            if state.plan_attempts < state.config.max_plan_attempts:
                state.plan_feedback = gate.feedback
                state.stage = Stage.PLAN
            else:
                self._escalate(state, f"plan rejected by the plan gate: {gate.summary}")
            return
        if state.config.approve_plan and not await self._approve(state):
            self._escalate(state, "plan rejected by a human")
            return
        state.stage = Stage.IMPLEMENT

    async def _implement(self, state: RunState, sandbox: Sandbox) -> None:
        cfg = state.config
        if state.plan is None:  # no planner: the ticket goes straight to one implementer run
            result = await self._agent(
                state, sandbox, role="implementer", model=cfg.implementer_model, system=implementer.SYSTEM,
                task=implementer.task_prompt(state.task.ticket),
            )  # fmt: skip
            if result.status == "escalated":
                state.question = result.question
                self._escalate(state, "the implementer needs clarification")
                return
            workspace.commit_all(state.ws, f"forgeloop: {state.task.id}")
        else:
            checkpoints = state.plan.checkpoints
            plan_text = state.plan.render()
            while state.checkpoints_done < len(checkpoints):
                n = state.checkpoints_done
                cp = checkpoints[n]
                state.checkpoint_attempts += 1
                retry = ".retry" if state.checkpoint_attempts > 1 else ""
                result = await self._agent(
                    state, sandbox, role=f"implementer.cp{n + 1}{retry}", model=cfg.implementer_model,
                    system=implementer.SYSTEM, context=plan_text,
                    task=implementer.checkpoint_prompt(state.task.ticket, n + 1, len(checkpoints), cp.title,
                                                       cp.steps, cp.done_when, unfinished=state.unfinished_status),
                )  # fmt: skip
                if result.status == "escalated":
                    state.question = result.question
                    self._escalate(state, f"the implementer needs clarification at checkpoint {n + 1}")
                    return
                workspace.commit_all(state.ws, f"forgeloop: {state.task.id} checkpoint {n + 1}: {cp.title}")
                if result.status != "done":
                    # The agent stopped on a limit: the checkpoint is not done, and the next one must not be told it is.
                    if state.checkpoint_attempts >= cfg.max_checkpoint_attempts:
                        self._escalate(
                            state, f"checkpoint {n + 1} did not finish in {state.checkpoint_attempts} attempts "
                                   f"(last: {result.status})"
                        )  # fmt: skip
                        return
                    state.unfinished_status = result.status
                    self._save(state, "checkpoint_unfinished", {"n": n + 1, "status": result.status})
                    if state.cost_usd >= cfg.max_usd_per_run:
                        return  # the run loop escalates on the budget
                    continue
                state.checkpoints_done += 1
                state.checkpoint_attempts = 0
                state.unfinished_status = None
                self._save(state, "checkpoint", {"n": n + 1, "title": cp.title, "status": result.status})
                if state.cost_usd >= cfg.max_usd_per_run:
                    break  # the run loop escalates on the budget before the next stage
        await self._notify("implemented", state)
        state.stage = self._after_implement(state)

    def _after_implement(self, state: RunState) -> Stage:
        if state.config.test_gate:
            return Stage.TEST_GATE
        if state.config.review_gate:
            return Stage.REVIEW_GATE
        return Stage.DELIVER

    async def _test_gate(self, state: RunState, sandbox: Sandbox) -> None:
        checks = state.config.checks
        if checks.autofix:  # deterministic autofix first: lint a tool can fix is not worth a model round
            await sandbox.exec(checks.autofix, 300)
            workspace.commit_all(state.ws, f"forgeloop: {state.task.id} autofix")
        gate = await run_test_gate(
            sandbox, state.ws, tests_cmd=checks.tests, lint_cmd=checks.lint or [], sast=checks.sast,
            test_timeout_s=checks.test_timeout_s, extra=checks.extra,
        )  # fmt: skip
        gate.data["round"] = state.test_rounds
        state.gates.append(gate)
        if gate.passed:
            state.stage = Stage.REVIEW_GATE if state.config.review_gate else Stage.DELIVER
            return
        if state.test_rounds >= state.config.max_test_rounds:
            self._escalate(state, f"test gate still failing after {state.test_rounds} fix rounds: {gate.summary}")
            return
        state.test_rounds += 1
        await self._fix(state, sandbox, "test gate", gate.feedback)
        await self._notify("test_fix", state)
        # stay in TEST_GATE: the fix is verified by the same gate

    async def _review_gate(self, state: RunState, sandbox: Sandbox) -> None:
        cfg = state.config
        diff = workspace.diff(state.ws)
        if not diff.strip():
            state.gates.append(GateResult(gate="review", passed=False, summary="empty diff"))
            self._escalate(state, "nothing was changed")
            return
        result = await self._agent(
            state, sandbox, role="reviewer", model=cfg.reviewer_model, system=reviewer.SYSTEM,
            task=reviewer.task_prompt(state.task.ticket, state.plan.render() if state.plan else None, diff[-60_000:]),
            read_only=True, finish=reviewer.FINISH, allow_escalation=False,
            max_steps=cfg.reviewer_max_steps, reasoning_effort=cfg.reviewer_reasoning,
        )  # fmt: skip
        if result.status != "done" or result.output is None:
            state.gates.append(GateResult(gate="review", passed=False, summary=f"reviewer failed: {result.status}"))
            self._escalate(state, f"review could not be completed: {result.status}")
            return
        review = Review.model_validate(result.output)
        state.reviews.append(review)
        await self._notify("reviewed", state)
        approved = review.verdict == "approve"
        state.gates.append(
            GateResult(
                gate="review", passed=approved, summary=review.verdict, feedback=review.render(),
                data={"round": state.review_rounds, "issues": len(review.issues)},
            )
        )  # fmt: skip
        if approved:
            state.stage = Stage.DELIVER
            return
        if state.review_rounds >= cfg.max_review_rounds:
            self._escalate(state, f"reviewer still requests changes after {state.review_rounds} rounds")
            return
        state.review_rounds += 1
        await self._fix(state, sandbox, "code review", review.render())
        await self._notify("review_fix", state)
        if state.stage != Stage.ESCALATED:
            state.stage = Stage.TEST_GATE if cfg.test_gate else Stage.REVIEW_GATE

    async def _deliver(self, state: RunState, sandbox: Sandbox) -> None:
        workspace.commit_all(state.ws, f"forgeloop: {state.task.id}")
        pr = self.runs_dir / state.run_id / "PR.md"
        pr.write_text(render_pr(state, workspace.diff(state.ws)), encoding="utf-8", newline="\n")
        state.stage = Stage.DELIVERED

    # --- helpers ------------------------------------------------------------------------------------------------

    async def _fix(self, state: RunState, sandbox: Sandbox, source: str, feedback: str) -> None:
        cfg = state.config
        result = await self._agent(
            state, sandbox, role=f"implementer.fix-{source.replace(' ', '-')}", model=cfg.implementer_model,
            system=implementer.SYSTEM, context=state.plan.render() if state.plan else None,
            task=implementer.fix_prompt(state.task.ticket, source, feedback, previous_idle=state.last_fix_idle),
        )  # fmt: skip
        committed = workspace.commit_all(state.ws, f"forgeloop: {state.task.id} fix after {source}")
        state.last_fix_idle = committed is None
        state.idle_fixes += int(committed is None)
        if result.status == "escalated":
            state.question = result.question
            self._escalate(state, f"the implementer needs clarification while fixing {source} findings")

    async def _agent(
        self,
        state: RunState,
        sandbox: Sandbox,
        *,
        role: str,
        model: str,
        system: str,
        task: str,
        context: str | None = None,
        read_only: bool = False,
        finish: FinishTool | None = None,
        allow_escalation: bool = True,
        max_steps: int | None = None,
        reasoning_effort: str | None = None,
    ) -> AgentResult:
        cfg = state.config
        toolbox = default_toolbox(
            Workspace(state.ws), sandbox, programs=frozenset(cfg.programs), test_command=cfg.checks.tests,
            test_timeout_s=cfg.checks.test_timeout_s,
        )  # fmt: skip
        if read_only:
            toolbox = toolbox.subset(read_only=True)
        remaining = max(cfg.max_usd_per_run - state.cost_usd, 0.0)
        loop = AgentLoop(
            self.client,
            toolbox,
            AgentConfig(
                model=model,
                system=system,
                max_usd=min(cfg.max_usd_per_agent, remaining),
                max_steps=max_steps or cfg.max_steps,
                temperature=cfg.temperature,
                reasoning_effort=reasoning_effort,
                variant=cfg.variant,
                allow_escalation=allow_escalation,
                finish=finish,
            ),
            tracer=self.tracer,
        )
        result = await loop.run(task, context=context, name=role)
        state.cost_usd += result.cost_usd
        state.agents.append(
            AgentSummary(
                role=role, status=result.status, steps=result.steps, cost_usd=result.cost_usd,
                input_tokens=result.usage.input_tokens, cache_read_tokens=result.usage.cache_read_tokens,
                output_tokens=result.usage.output_tokens,
            )
        )  # fmt: skip
        self.store.event(state.run_id, state.stage.value, "agent", state.agents[-1].model_dump())
        if self.log:
            a = state.agents[-1]
            self.log(f"  {a.role:26} {a.status:9} {a.steps:3} steps  ${a.cost_usd:.4f}")
        return result

    def _escalate(self, state: RunState, reason: str) -> None:
        state.escalation = reason
        state.stage = Stage.ESCALATED

    def _save(self, state: RunState, kind: str, data: dict[str, Any]) -> None:
        self.store.save(state.run_id, state.task.id, state.stage.value, state.cost_usd, state)
        self.store.event(state.run_id, state.stage.value, kind, data)
        if not self.log:
            return
        if kind == "stage_done":
            done = data["stage"]
            gate = state.gates[-1] if state.gates and done.endswith("gate") else None
            verdict = f"  [{'pass' if gate.passed else 'FAIL'}: {gate.summary}]" if gate else ""
            self.log(f"{done} -> {data['next']}{verdict}")
        elif kind == "checkpoint":
            self.log(f"  checkpoint {data['n']} committed: {data['title']}")

    async def _approve(self, state: RunState) -> bool:
        assert state.plan is not None
        if self.approver is None:
            return False
        self._save(state, "awaiting_approval", {"stage": state.stage.value})
        if self.log:
            self.log("  waiting for a human to approve the plan")
        approved = await self.approver(state, state.plan)
        self.store.event(state.run_id, state.stage.value, "plan_decision", {"approved": approved})
        return approved

    async def _notify(self, event: str, state: RunState) -> None:
        if self.observer:
            await self.observer(event, state)


def render_pr(state: RunState, diff: str) -> str:
    lines = [f"# {state.task.title}", ""]
    if state.task.url:
        lines += [f"Closes {state.task.url}", ""]
    lines += ["## Ticket", "", state.task.body.strip(), ""]
    if state.plan:
        lines += [state.plan.render().replace("# Plan", "## Plan", 1), ""]
    lines += ["## Gates", "", "| gate | result | details |", "| --- | --- | --- |"]
    for g in state.gates:
        lines.append(f"| {g.gate} | {'pass' if g.passed else 'fail'} | {g.summary} |")
    lines += ["", "## Cost", "", "| agent | status | steps | $ |", "| --- | --- | ---: | ---: |"]
    for a in state.agents:
        lines.append(f"| {a.role} | {a.status} | {a.steps} | {a.cost_usd:.4f} |")
    lines += [f"| **total** | | | **{state.cost_usd:.4f}** |", ""]
    lines += ["<details><summary>Diff</summary>", "", "```diff", diff[-30_000:], "```", "", "</details>", ""]
    return "\n".join(lines)
