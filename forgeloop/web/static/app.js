import { ago, append, badge, diffView, h, mount, pct, stageBadge, usd } from "./dom.js";
import { markdown } from "./md.js";

const view = document.getElementById("view");
let cleanup = [];  // timers and event sources of the current page

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: { "X-ForgeLoop": "1", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  const data = text && res.headers.get("content-type")?.includes("json") ? JSON.parse(text) : text;
  if (!res.ok) throw new Error(typeof data === "object" && data.detail ? JSON.stringify(data.detail) : String(data));
  return data;
}

function every(ms, fn) {
  fn();
  const id = setInterval(fn, ms);
  cleanup.push(() => clearInterval(id));
}

// --- account bar ------------------------------------------------------------------------------------------------

async function refreshAccount() {
  try {
    const a = await api("/api/account");
    mount(
      document.getElementById("account"),
      a.balance_usd !== undefined && a.balance_usd !== null ? h("span", {}, "balance ", h("b", {}, usd(a.balance_usd, 2))) : null,
      h("span", {}, "this session ", h("b", {}, usd(a.session_spent_usd, 3)), a.session_limit_usd ? ` / ${usd(a.session_limit_usd, 2)}` : ""),
    );
  } catch { /* the bar is informational */ }
}

// --- runs list --------------------------------------------------------------------------------------------------

function runsPage() {
  const table = h("tbody");
  const body = h("div", { class: "panel" },
    h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Run"), h("th", {}, "Status"), h("th", { class: "num" }, "Cost"), h("th", {}, "Updated"))), table));
  mount(view,
    h("div", { class: "row" },
      h("div", { class: "grow" }, h("h1", {}, "Runs"), h("div", { class: "sub" }, "Every ticket that went through the pipeline. Running ones update live.")),
      h("button", { class: "primary", onclick: () => (location.hash = "#/new") }, "New run")),
    body);
  every(3000, async () => {
    const runs = await api("/api/runs");
    if (!runs.length) {
      mount(table, h("tr", {}, h("td", { colspan: 4, class: "empty" }, "No runs yet. Start one from “New run”.")));
      return;
    }
    mount(table, runs.map((r) =>
      h("tr", { class: "click", onclick: () => (location.hash = `#/run/${r.id}`) },
        h("td", {}, h("div", {}, r.task_id), h("div", { class: "muted mono" }, r.id)),
        h("td", {}, stageBadge(r.stage, r.phase)),
        h("td", { class: "num" }, usd(r.cost_usd)),
        h("td", { class: "muted" }, ago(r.updated_at)))));
  });
}

// --- new run ----------------------------------------------------------------------------------------------------

const CONFIG_HELP = {
  baseline: "One agent works on the ticket directly. No plan, no gates.",
  plan: "A planner writes a checked plan; the implementer works checkpoint by checkpoint.",
  "test-gate": "Plan + tests, ruff, bandit and a dependency check, with fix rounds.",
  full: "Plan + test gate + an independent reviewer on another model.",
};
const MODELS = ["anthropic/claude-haiku-5.5", "google/gemini-2.5-flash-lite", "deepseek/deepseek-v4.1-flash", "z-ai/glm-5.3-flash", "qwen/qwen3-coder-30b-a3b-instruct"];

async function newRunPage() {
  const form = { source: "bench", task_id: "", title: "", body: "", issue: "", repo: "bench/bench-repo", project_config: "",
    config: "full", implementer_model: "", planner_model: "", reviewer_model: "", max_usd: "", approve_plan: true };
  const tasks = await api("/api/tasks");
  const sourceBox = h("div");
  const error = h("div", { class: "error" });

  const seg = (options, key, onchange) => {
    const box = h("div", { class: "seg" });
    const draw = () => mount(box, options.map(([value, label]) =>
      h("button", { type: "button", class: form[key] === value ? "on" : "", onclick: () => { form[key] = value; draw(); onchange?.(); } }, label)));
    draw();
    return box;
  };
  const input = (key, attrs = {}) => h(attrs.multiline ? "textarea" : "input", {
    type: attrs.type || "text", value: form[key], placeholder: attrs.placeholder || "", list: attrs.list,
    oninput: (e) => { form[key] = attrs.type === "number" ? Number(e.target.value) : e.target.value; },
  });

  const repoFields = () => [
    h("label", {}, "Repository ", h("span", { class: "hint" }, "local path; a git repo is cloned, the original is never touched")), input("repo"),
    h("label", {}, "Project config ", h("span", { class: "hint" }, "optional forgeloop.toml outside the repo; run `forgeloop doctor` once first")),
    input("project_config", { placeholder: "examples/menubench/forgeloop.toml" }),
  ];
  const drawSource = () => {
    if (form.source === "bench") {
      const cards = h("div", { class: "cards" });
      const draw = () => mount(cards, tasks.map((t) =>
        h("div", { class: `card ${form.task_id === t.id ? "on" : ""}`, onclick: () => { form.task_id = t.id; draw(); } },
          h("div", { class: "t" }, t.title),
          h("div", { class: "m" }, badge(t.kind), badge(t.difficulty, t.difficulty === "hard" ? "warn" : ""),
            t.expected === "escalate" ? badge("ambiguous", "info") : null, t.tags.includes("injection") ? badge("injection", "bad") : null))));
      draw();
      mount(sourceBox, h("label", {}, "Bench task ", h("span", { class: "hint" }, "runs against the Rentwise polygon; hidden tests are not used here")), cards);
    } else if (form.source === "text") {
      mount(sourceBox,
        h("label", {}, "Title"), input("title", { placeholder: "Search ignores letter case" }),
        h("label", {}, "Ticket ", h("span", { class: "hint" }, "markdown: symptom or desired behaviour, acceptance criteria")),
        input("body", { multiline: true, placeholder: "Searching for 'tent' finds nothing, 'Tent' works…" }),
        ...repoFields());
    } else {
      mount(sourceBox,
        h("label", {}, "GitHub issue ", h("span", { class: "hint" }, "read with your gh login")), input("issue", { placeholder: "owner/repo#123" }),
        ...repoFields());
    }
  };
  drawSource();

  const configHelp = h("div", { class: "hint" }, CONFIG_HELP[form.config]);
  const models = h("datalist", { id: "models" }, MODELS.map((m) => h("option", { value: m })));
  const submit = h("button", { class: "primary", onclick: async () => {
    submit.disabled = true;
    error.textContent = "";
    try {
      const body = Object.fromEntries(Object.entries(form).filter(([, v]) => v !== ""));
      if (body.max_usd !== undefined) body.max_usd = Number(body.max_usd);
      const run = await api("/api/runs", { method: "POST", body });
      location.hash = `#/run/${run.state.run_id}`;
    } catch (e) { error.textContent = e.message; submit.disabled = false; }
  } }, "Start run");

  mount(view,
    h("h1", {}, "New run"), h("div", { class: "sub" }, "Pick a ticket and how much of the pipeline to use."),
    models,
    h("div", { class: "panel" }, h("h2", {}, "Ticket"),
      seg([["bench", "Bench task"], ["text", "Write a ticket"], ["github", "GitHub issue"]], "source", drawSource), sourceBox),
    h("div", { class: "panel" }, h("h2", {}, "Pipeline"),
      seg(Object.keys(CONFIG_HELP).map((c) => [c, c]), "config", () => { configHelp.textContent = CONFIG_HELP[form.config]; }),
      h("div", { style: { marginTop: "8px" } }, configHelp),
      h("div", { class: "grid-3" },
        h("div", {}, h("label", {}, "Implementer"), input("implementer_model", { list: "models", placeholder: "project default / haiku" })),
        h("div", {}, h("label", {}, "Planner"), input("planner_model", { list: "models", placeholder: "project default / haiku" })),
        h("div", {}, h("label", {}, "Reviewer"), input("reviewer_model", { list: "models", placeholder: "project default / glm-5.3-flash" }))),
      h("div", { class: "grid-2" },
        h("div", {}, h("label", {}, "Budget for this run, $ ", h("span", { class: "hint" }, "hard stop; typical ticket on haiku: $0.02–0.05")), input("max_usd", { type: "number", placeholder: "project default / 1" })),
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: form.approve_plan, onchange: (e) => { form.approve_plan = e.target.checked; } }),
          "Ask me to approve the plan before any code is written"))),
    h("div", { class: "row" }, submit, error));
}

// --- run detail -------------------------------------------------------------------------------------------------

const STAGES = [
  ["plan", "Plan", (c) => c.plan],
  ["plan_gate", "Plan gate", (c) => c.plan],
  ["implement", "Implement", () => true],
  ["test_gate", "Test gate", (c) => c.test_gate],
  ["review_gate", "Review", (c) => c.review_gate],
  ["deliver", "Deliver", () => true],
];
const GATE_OF = { plan_gate: "plan", test_gate: "test", review_gate: "review" };

function stepper(state, phase, events) {
  const order = STAGES.map((s) => s[0]);
  const escalatedAt = [...events].reverse().find((e) => e.kind === "stage_done" && e.data.next === "escalated")?.data.stage;
  const current = state.stage === "delivered" ? "done" : state.stage === "escalated" ? escalatedAt : state.stage;
  const idx = order.indexOf(current);
  return h("div", { class: "steps" }, STAGES.map(([key, label, enabled], i) => {
    let cls = "";
    let note = "";
    if (!enabled(state.config)) { cls = "skip"; note = "off"; }
    else if (state.stage === "delivered" || i < idx) { cls = "done"; }
    else if (i === idx && state.stage === "escalated") { cls = "fail"; note = "escalated"; }
    else if (i === idx && (phase === "running" || phase === "awaiting_approval")) { cls = "active"; note = phase === "awaiting_approval" ? "waiting for you" : "working"; }
    else if (i === idx) { note = "paused here"; }
    const gates = state.gates.filter((g) => g.gate === GATE_OF[key]);
    if (gates.length && cls !== "skip") note = gates.map((g) => (g.passed ? "pass" : "fail")).join(" → ");
    return h("div", { class: `step ${cls}` }, h("span", { class: "dot" }), h("b", {}, label), note);
  }));
}

function eventLine(e) {
  const d = e.data;
  const ts = new Date(e.ts).toLocaleTimeString();
  let text = `${e.kind} ${JSON.stringify(d)}`;
  let cls = "";
  if (e.kind === "agent") text = `${d.role} · ${d.status} · ${d.steps} steps · ${usd(d.cost_usd)}`;
  else if (e.kind === "stage_done") { text = `${d.stage} → ${d.next}`; cls = d.next === "escalated" ? "ev-bad" : "ev-stage"; }
  else if (e.kind === "checkpoint") text = `checkpoint ${d.n} committed: ${d.title}`;
  else if (e.kind === "checkpoint_unfinished") { text = `checkpoint ${d.n} stopped unfinished (${d.status}), retrying`; cls = "ev-bad"; }
  else if (e.kind === "created") text = `run created · config ${JSON.stringify(Object.fromEntries(["plan", "test_gate", "review_gate"].map((k) => [k, d.config[k]])))}`;
  else if (e.kind === "awaiting_approval") { text = "waiting for a human to approve the plan"; cls = "ev-stage"; }
  else if (e.kind === "plan_decision") text = d.approved ? "plan approved" : "plan rejected";
  else if (e.kind === "cancelled" || e.kind === "crashed") { text = `${e.kind} ${d.error || ""}`; cls = "ev-bad"; }
  return h("div", {}, h("span", { class: "ts" }, ts), h("span", { class: cls }, text));
}

async function runPage(runId) {
  let data = await api(`/api/runs/${runId}`);
  const events = [];
  let tab = "activity";
  const header = h("div");
  const banner = h("div");
  const stepsBox = h("div", { class: "panel" });
  const tabsBox = h("div", { class: "panel" });
  const logBox = h("div", { class: "log" });
  mount(view, header, stepsBox, banner, tabsBox);

  const drawHeader = () => {
    const s = data.state;
    const actions = [];
    if (data.phase === "running" || data.phase === "awaiting_approval")
      actions.push(h("button", { class: "danger", onclick: async () => { await api(`/api/runs/${runId}/cancel`, { method: "POST" }); } }, "Cancel"));
    if (data.phase === "paused")
      actions.push(h("button", { class: "primary", onclick: async () => { data = await api(`/api/runs/${runId}/resume`, { method: "POST" }); listen(); drawAll(); } }, "Resume"));
    mount(header, h("div", { class: "row" },
      h("div", { class: "grow" },
        h("h1", {}, s.task.title),
        h("div", { class: "sub row" }, stageBadge(s.stage, data.phase), h("span", { class: "mono" }, s.run_id),
          h("span", {}, `${s.config.implementer_model} · reviewer ${s.config.reviewer_model}`), h("b", {}, usd(s.cost_usd)), `of ${usd(s.config.max_usd_per_run, 2)}`)),
      actions));
  };

  const drawBanner = () => {
    const s = data.state;
    if (data.phase === "awaiting_approval" && s.plan) {
      const decide = async (approve) => { await api(`/api/runs/${runId}/approve`, { method: "POST", body: { approve } }); };
      mount(banner, h("div", { class: "banner info" },
        h("h2", {}, "Approve the plan?"), h("div", { class: "muted" }, "Nothing has been written yet. Rejecting escalates the run."),
        markdown(planMarkdown(s.plan)),
        h("div", { class: "row" }, h("button", { class: "primary", onclick: () => decide(true) }, "Approve and implement"),
          h("button", { class: "danger", onclick: () => decide(false) }, "Reject"))));
    } else if (s.stage === "escalated") {
      mount(banner, h("div", { class: "banner warn" }, h("h2", {}, "Escalated to a human"), h("div", {}, s.escalation),
        s.question ? h("div", { style: { marginTop: "8px" } }, h("b", {}, "Question: "), s.question) : null));
    } else if (s.stage === "delivered") {
      const prError = h("div", { class: "error" });
      const prButton = h("button", { class: "primary", onclick: async () => {
        prButton.disabled = true;
        prError.textContent = "";
        try { await api(`/api/runs/${runId}/open-pr`, { method: "POST" }); data = await api(`/api/runs/${runId}`); drawAll(); }
        catch (e) { prError.textContent = e.message; prButton.disabled = false; }
      } }, "Open draft PR on GitHub");
      const action = s.pr_url
        ? h("div", {}, "Pull request: ", h("a", { href: s.pr_url, target: "_blank", rel: "noopener noreferrer" }, s.pr_url))
        : s.cloned ? h("div", { class: "row", style: { marginTop: "10px" } }, prButton, h("span", { class: "muted" }, "pushes the branch with your git credentials"), prError)
          : h("div", { class: "muted" }, "This run worked on a copy without git history, so it cannot become a pull request.");
      mount(banner, h("div", { class: "banner ok" }, h("h2", {}, "Delivered"), "All enabled gates passed. The PR description is in the PR tab.", action));
    } else mount(banner);
  };

  const tabContent = async () => {
    const s = data.state;
    if (tab === "activity") return logBox;
    if (tab === "plan") return s.plan ? markdown(planMarkdown(s.plan)) : h("div", { class: "empty" }, s.config.plan ? "No plan yet." : "This configuration has no planner.");
    if (tab === "gates") return s.gates.length ? h("div", {}, s.gates.map((g) => h("div", { class: "panel" },
      h("div", { class: "row" }, h("b", {}, g.gate), badge(g.passed ? "pass" : "fail", g.passed ? "ok" : "bad"), h("span", { class: "muted" }, g.summary)),
      g.feedback ? h("pre", { class: "code", style: { marginTop: "10px" } }, g.feedback) : null))) : h("div", { class: "empty" }, "No gate has run yet.");
    if (tab === "agents") return h("table", {}, h("thead", {}, h("tr", {}, ["Agent", "Status", "Steps", "Input tokens", "From cache", "Output", "Cost"].map((t, i) => h("th", { class: i > 1 ? "num" : "" }, t)))),
      h("tbody", {}, s.agents.map((a) => h("tr", {}, h("td", {}, a.role), h("td", {}, badge(a.status, a.status === "done" ? "ok" : a.status === "escalated" ? "warn" : "bad")),
        h("td", { class: "num" }, a.steps), h("td", { class: "num" }, a.input_tokens.toLocaleString()), h("td", { class: "num" }, a.cache_read_tokens.toLocaleString()),
        h("td", { class: "num" }, a.output_tokens.toLocaleString()), h("td", { class: "num" }, usd(a.cost_usd))))));
    if (tab === "pr") return data.has_pr ? markdown(await api(`/api/runs/${runId}/pr`)) : h("div", { class: "empty" }, "The PR description is written when the run is delivered.");
    if (tab === "diff") {
      if (!data.workspace_exists) return h("div", { class: "empty" }, "The workspace was removed.");
      const text = await api(`/api/runs/${runId}/diff`);
      return text.trim() ? diffView(text) : h("div", { class: "empty" }, "No changes yet.");
    }
    if (tab === "trace") {
      const spans = await api(`/api/runs/${runId}/trace`);
      return h("table", {}, h("thead", {}, h("tr", {}, ["Kind", "Name", "Seconds", "Detail"].map((t) => h("th", {}, t)))),
        h("tbody", {}, spans.map((sp) => h("tr", {}, h("td", {}, badge(sp.kind)), h("td", { class: "mono" }, sp.name), h("td", { class: "num" }, (sp.duration_s || 0).toFixed(2)),
          h("td", { class: "mono muted" }, sp.kind === "llm" ? `${sp.attrs.model} · in ${sp.attrs.input_tokens} (cache ${sp.attrs.cache_read_tokens}) · ${usd(sp.attrs.cost_usd)}`
            : sp.kind === "tool" ? JSON.stringify(sp.attrs.args).slice(0, 140) : sp.status)))));
    }
    return h("div");
  };

  const drawTabs = async () => {
    const names = [["activity", "Activity"], ["plan", "Plan"], ["gates", "Gates"], ["agents", "Agents & cost"], ["pr", "PR"], ["diff", "Diff"], ["trace", "Trace"]];
    const bar = h("div", { class: "tabs" }, names.map(([k, label]) => h("button", { class: tab === k ? "on" : "", onclick: () => { tab = k; drawTabs(); } }, label)));
    const content = await tabContent();
    mount(tabsBox, bar, content);
  };

  const drawAll = () => {
    drawHeader();
    mount(stepsBox, stepper(data.state, data.phase, events));
    drawBanner();
    if (tab !== "activity") drawTabs();
  };

  let source = null;
  const listen = () => {
    source?.close();
    events.length = 0;
    logBox.replaceChildren();
    source = new EventSource(`/api/runs/${runId}/events`);
    source.onmessage = async (m) => {
      const e = JSON.parse(m.data);
      events.push(e);
      logBox.append(eventLine(e));
      logBox.scrollTop = logBox.scrollHeight;
      data = await api(`/api/runs/${runId}`);
      drawAll();
    };
    source.addEventListener("end", async () => { source.close(); data = await api(`/api/runs/${runId}`); drawAll(); });
  };
  cleanup.push(() => source?.close());

  drawAll();
  await drawTabs();
  listen();
}

function planMarkdown(plan) {
  const lines = [plan.summary, "", "**Files**", ...plan.files.map((f) => `- ${f.action} \`${f.path}\` — ${f.why}`), "", "**Checkpoints**"];
  plan.checkpoints.forEach((c, i) => lines.push(`${i + 1}. **${c.title}** — ${c.steps.join("; ")} (done when: ${c.done_when})`));
  lines.push("", "**Acceptance**", ...plan.acceptance.map((a) => `- ${a}`));
  if (plan.risks?.length) lines.push("", "**Risks**", ...plan.risks.map((r) => `- ${r}`));
  return lines.join("\n");
}

// --- evals ------------------------------------------------------------------------------------------------------

async function evalsPage() {
  const evals = await api("/api/evals");
  mount(view, h("h1", {}, "Evals"), h("div", { class: "sub" }, "What each stage of the pipeline buys, measured with hidden tests."),
    h("div", { class: "panel" }, h("table", {}, h("thead", {}, h("tr", {}, ["Eval", "Implementer", "Configs", "Runs", "Cost"].map((t, i) => h("th", { class: i > 2 ? "num" : "" }, t)))),
      h("tbody", {}, evals.map((e) => h("tr", { class: "click", onclick: () => (location.hash = `#/eval/${e.id}`) },
        h("td", {}, h("b", {}, e.id)), h("td", { class: "mono" }, e.model), h("td", {}, e.configs.join(", ")),
        h("td", { class: "num" }, e.runs), h("td", { class: "num" }, usd(e.usd, 3))))))));
}

function deliveryBar(c) {
  const n = c.solvable || 1;
  const part = (cls, v) => h("span", { class: cls, style: { width: `${(100 * v) / n}%` }, title: `${v}` });
  return h("div", { class: "bar" }, part("ok", c.working), part("bad", c.broken), part("warn", c.escalated));
}

async function evalPage(id) {
  const e = await api(`/api/evals/${id}`);
  const best = e.configs.find((c) => c.config === "baseline") || e.configs[0];
  const full = e.configs.find((c) => c.config === "full") || e.configs[e.configs.length - 1];
  const configs = e.configs.map((c) => c.config);
  mount(view,
    h("a", { href: "#/evals" }, "← Evals"),
    h("h1", {}, id), h("div", { class: "sub mono" }, e.model),
    h("div", { class: "kpis" },
      kpi(pct(best.pass_at_1), `pass@1 · ${best.config}`), kpi(pct(full.pass_at_1), `pass@1 · ${full.config}`),
      kpi(`${best.broken} → ${full.broken}`, "broken deliveries"), kpi(`${usd(best.usd_per_working, 3)} → ${usd(full.usd_per_working, 3)}`, "$ per working change")),
    h("div", { class: "panel" }, h("h2", {}, "By configuration"),
      h("table", {}, h("thead", {}, h("tr", {}, ["Config", "pass@1", "Delivered working / broken / escalated", "Ambiguous → asked", "Precision", "$ / run", "$ / working"].map((t, i) => h("th", { class: i > 2 ? "num" : "" }, t)))),
        h("tbody", {}, e.configs.map((c) => h("tr", {}, h("td", {}, h("b", {}, c.config)), h("td", {}, pct(c.pass_at_1)),
          h("td", {}, h("div", { class: "row" }, deliveryBar(c), h("span", { class: "mono" }, `${c.working} / ${c.broken} / ${c.escalated}`))),
          h("td", { class: "num" }, `${c.ambiguous_asked}/${c.ambiguous}`), h("td", { class: "num" }, pct(c.precision)),
          h("td", { class: "num" }, usd(c.usd_per_run)), h("td", { class: "num" }, usd(c.usd_per_working, 3)))))),
      h("div", { class: "legend" }, h("span", {}, h("i", { class: "ok" }), "working"), h("span", {}, h("i", { class: "bad" }), "broken"), h("span", {}, h("i", { class: "warn" }), "escalated"))),
    h("div", { class: "panel" }, h("h2", {}, "What each gate did"),
      h("table", {}, h("thead", {}, h("tr", {}, ["Config", "Test-gate saves", "Review saves", "request_changes right / wrong", "Approved broken", "Review broke working", "Escalated working"].map((t, i) => h("th", { class: i ? "num" : "" }, t)))),
        h("tbody", {}, e.configs.map((c) => h("tr", {}, h("td", {}, h("b", {}, c.config)),
          ["test_saves", "review_saves"].map((k) => h("td", { class: "num" }, c.gates[k] ?? 0)),
          h("td", { class: "num" }, `${c.gates.review_true_rc ?? 0} / ${c.gates.review_false_rc ?? 0}`),
          ["review_missed", "review_broke", "false_escalations"].map((k) => h("td", { class: "num" }, c.gates[k] ?? 0))))))),
    h("div", { class: "panel" }, h("h2", {}, "By task"),
      h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Task"), h("th", {}, "Kind"), configs.map((c) => h("th", {}, c)))),
        h("tbody", {}, e.tasks.map((t) => h("tr", {},
          h("td", {}, h("div", {}, t.task_id), h("div", { class: "m" }, badge(t.difficulty, t.difficulty === "hard" ? "warn" : ""), t.expected === "escalate" ? badge("ambiguous", "info") : null)),
          h("td", { class: "muted" }, t.kind),
          configs.map((c) => {
            const cells = t.cells[c] || [];
            if (!cells.length) return h("td", {}, "–");
            const passed = cells.filter((x) => x.passed).length;
            const kind = passed === cells.length ? "ok" : cells.some((x) => x.status === "escalated") ? "warn" : "bad";
            return h("td", { title: cells.map((x) => x.error || x.status).join("\n") }, h("span", { class: `cell ${kind}` }, `${passed}/${cells.length}`),
              " ", h("span", { class: "muted" }, kind === "ok" ? "" : cells[0].status));
          })))))));
}

function kpi(value, label) {
  return h("div", { class: "kpi" }, h("div", { class: "v" }, value), h("div", { class: "l" }, label));
}

// --- router -----------------------------------------------------------------------------------------------------

async function route() {
  cleanup.forEach((fn) => fn());
  cleanup = [];
  window.scrollTo(0, 0);
  const [, page, arg] = location.hash.split("/");
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === (page || "runs") || (page === "run" && a.dataset.nav === "runs") || (page === "eval" && a.dataset.nav === "evals")));
  try {
    if (page === "new") await newRunPage();
    else if (page === "run" && arg) await runPage(decodeURIComponent(arg));
    else if (page === "evals") await evalsPage();
    else if (page === "eval" && arg) await evalPage(decodeURIComponent(arg));
    else runsPage();
  } catch (e) {
    mount(view, h("div", { class: "panel error" }, e.message));
  }
}

window.addEventListener("hashchange", route);
route();
setInterval(refreshAccount, 15000);
refreshAccount();
