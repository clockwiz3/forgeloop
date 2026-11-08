import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_orchestrator import APPROVE, PLAN, FakeSandbox, Models, call, implementer_edits

from forgeloop.tools import Sandbox
from forgeloop.web import create_app
from llmcore import LLMClient, Meter

HEADERS = {"X-ForgeLoop": "1"}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    return root


def make_client(tmp_path: Path, models: Models, results: Path | None = None) -> TestClient:
    async def factory(_: Path) -> Sandbox:
        return FakeSandbox([])

    llm = LLMClient(api_key="k", transport=httpx.MockTransport(models), meter=Meter(limit_usd=1.0))
    app = create_app(llm, runs_dir=tmp_path / "runs", sandbox_factory=factory, results_dir=results or tmp_path / "res")
    return TestClient(app)


def wait_for(client: TestClient, run_id: str, phases: set[str], timeout: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data: dict[str, Any] = client.get(f"/api/runs/{run_id}").json()
        if data["phase"] in phases:
            return data
        time.sleep(0.05)
    raise AssertionError(f"run never reached {phases}: {data['phase']} / {data['state']['stage']}")


def test_only_localhost_and_csrf_header_are_accepted(tmp_path: Path) -> None:
    with make_client(tmp_path, Models({})) as client:
        assert client.get("/api/runs", headers={"Host": "evil.example"}).status_code == 403
        assert client.get("/api/runs", headers={"Host": "127.0.0.1:8765"}).status_code == 200
        r = client.post("/api/runs", json={"source": "text", "title": "t", "body": "b"})
        assert r.status_code == 403 and "x-forgeloop" in r.json()["detail"]


def test_frontend_is_served_with_a_strict_csp(tmp_path: Path) -> None:
    with make_client(tmp_path, Models({})) as client:
        r = client.get("/")
        assert r.status_code == 200 and "ForgeLoop" in r.text
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert client.get("/static/app.js").status_code == 200


def test_a_ticket_runs_end_to_end_with_plan_approval_in_the_browser(tmp_path: Path, repo: Path) -> None:
    models = Models(
        {
            "planner": [call("submit_plan", **PLAN)],
            "implementer": implementer_edits(2),
            "reviewer": [call("submit_review", **APPROVE)],
        }
    )
    with make_client(tmp_path, models) as client:
        body = {
            "source": "text",
            "title": "Fix add",
            "body": "add() subtracts",
            "repo": str(repo),
            "approve_plan": True,
        }
        r = client.post("/api/runs", json=body, headers=HEADERS)
        assert r.status_code == 201, r.text
        run_id = r.json()["state"]["run_id"]

        waiting = wait_for(client, run_id, {"awaiting_approval"})
        assert waiting["state"]["plan"]["summary"] == PLAN["summary"] and models.calls["implementer"] == 0
        assert client.post(f"/api/runs/{run_id}/approve", json={"approve": True}, headers=HEADERS).json() == {
            "ok": True
        }

        done = wait_for(client, run_id, {"finished"})
        assert done["state"]["stage"] == "delivered" and done["has_pr"]
        assert "## Plan" in client.get(f"/api/runs/{run_id}/pr").text
        assert "+X = 1" in client.get(f"/api/runs/{run_id}/diff").text
        assert [r["id"] for r in client.get("/api/runs").json()] == [run_id]

        with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
            payload = "".join(stream.iter_text())
        kinds = [json.loads(line[6:])["kind"] for line in payload.splitlines() if line.startswith('data: {"seq"')]
        assert "awaiting_approval" in kinds and "plan_decision" in kinds and "event: end" in payload


def test_rejecting_the_plan_escalates(tmp_path: Path, repo: Path) -> None:
    models = Models({"planner": [call("submit_plan", **PLAN)]})
    with make_client(tmp_path, models) as client:
        body = {"source": "text", "title": "Fix add", "body": "x", "repo": str(repo), "approve_plan": True}
        run_id = client.post("/api/runs", json=body, headers=HEADERS).json()["state"]["run_id"]
        wait_for(client, run_id, {"awaiting_approval"})
        client.post(f"/api/runs/{run_id}/approve", json={"approve": False}, headers=HEADERS)
        done = wait_for(client, run_id, {"finished"})
        assert done["state"]["stage"] == "escalated" and "rejected by a human" in done["state"]["escalation"]
        again = client.post(f"/api/runs/{run_id}/approve", json={"approve": True}, headers=HEADERS)
        assert again.status_code == 409


def test_bad_requests_are_rejected(tmp_path: Path) -> None:
    with make_client(tmp_path, Models({})) as client:

        def post(body: dict[str, Any]) -> int:
            return int(client.post("/api/runs", json=body, headers=HEADERS).status_code)

        assert post({"source": "text", "title": "", "body": ""}) == 422
        assert post({"source": "text", "title": "t", "body": "b", "repo": str(tmp_path / "nope")}) == 422
        assert post({"source": "bench", "task_id": "no-such-task"}) == 404
        assert post({"source": "github", "issue": "not-an-issue"}) == 422
        assert post({"source": "text", "title": "t", "body": "b", "max_usd": 50}) == 422
        assert client.get("/api/runs/missing").status_code == 404


def test_eval_results_are_served_as_data(tmp_path: Path) -> None:
    results = tmp_path / "res"
    results.mkdir()
    record = {
        "eval_id": "e", "task_id": "bug-1", "kind": "bug", "difficulty": "easy", "expected": "solve", "tags": [],
        "config": "baseline", "repeat": 0, "model": "m", "status": "delivered", "passed": True, "hidden_passed": True,
        "cost_usd": 0.01, "steps": 3, "input_tokens": 1, "cache_read_tokens": 0, "output_tokens": 1, "wall_s": 1.0,
    }  # fmt: skip
    (results / "e.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    (results / "e.superseded.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    with make_client(tmp_path, Models({}), results) as client:
        assert [e["id"] for e in client.get("/api/evals").json()] == ["e"]
        detail = client.get("/api/evals/e").json()
        (row,) = detail["configs"]
        assert row["working"] == 1 and row["pass_at_1"] == 1.0
        assert detail["tasks"][0]["cells"]["baseline"][0]["passed"] is True
        assert client.get("/api/evals/..%2Fsecrets").status_code == 404


def test_open_pr_button_calls_delivery_only_on_request(tmp_path: Path, repo: Path) -> None:
    models = Models({"implementer": implementer_edits(1)})
    opened: list[str] = []

    def fake_deliver(state: Any) -> str:
        opened.append(state.run_id)
        state.pr_url = "https://github.com/o/r/pull/1"
        return str(state.pr_url)

    async def factory(_: Path) -> Sandbox:
        return FakeSandbox([])

    llm = LLMClient(api_key="k", transport=httpx.MockTransport(models), meter=Meter(limit_usd=1.0))
    app = create_app(llm, runs_dir=tmp_path / "runs", sandbox_factory=factory, deliver_pr=fake_deliver)
    with TestClient(app) as client:
        body = {"source": "text", "title": "Fix", "body": "x", "repo": str(repo), "config": "baseline"}
        run_id = client.post("/api/runs", json=body, headers=HEADERS).json()["state"]["run_id"]
        wait_for(client, run_id, {"finished"})
        assert opened == []  # delivery never happens by itself
        assert client.post(f"/api/runs/{run_id}/open-pr").status_code == 403  # needs the CSRF header
        r = client.post(f"/api/runs/{run_id}/open-pr", headers=HEADERS)
        assert r.json() == {"url": "https://github.com/o/r/pull/1"} and opened == [run_id]
        assert client.get(f"/api/runs/{run_id}").json()["state"]["pr_url"].endswith("/pull/1")
