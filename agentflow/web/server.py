"""Web 控制台（纯标准库 http.server，无第三方依赖）。

功能：

* 浏览内置工作流的节点/边/入参，填表一键运行；
* 运行中每 600ms 轮询事件，**实时重绘**流程图（节点按状态变色）与日志流；
* 支持故障注入（演示重试）、查看历史运行、打开完整 HTML 报告；
* 只读展示，不存在写文件的接口（除运行日志/报告，均写在 ``runs/`` 下）。

实现要点：这里的“实时图”不是前端画图，而是服务端用同一套
``layout + render_graph_svg`` 按当前事件流重渲染 SVG，前端只换 ``<img>``
的 src —— 前端代码量极小，且保证控制台与报告里的图完全一致。
"""

from __future__ import annotations

import json
import random
import string
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from ..core.agent import default_registry
from ..core.engine import Engine
from ..core.graph import WorkflowSpec, list_workflows, load_workflow
from ..core.layout import compute_layout
from ..core.report import render_graph_svg

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentFlow 控制台</title>
<style>
:root{--bg:#0b1120;--panel:#111a2e;--panel2:#0f172a;--border:#1f2b45;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
a{color:var(--accent)}
.wrap{max-width:1400px;margin:0 auto;padding:20px}
header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border);padding-bottom:14px;margin-bottom:18px;flex-wrap:wrap;gap:10px}
h1{font-size:18px;margin:0}
h2{font-size:14px;margin:0 0 10px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.cols{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:16px;align-items:start}
@media(max-width:1080px){.cols{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.card+.card{margin-top:14px}
.wf-btn{display:block;width:100%;text-align:left;background:var(--panel2);border:1px solid var(--border);color:var(--text);
border-radius:10px;padding:9px 12px;margin-bottom:8px;cursor:pointer;font-size:13px}
.wf-btn.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.wf-btn .d{color:var(--muted);font-size:11.5px;display:block;margin-top:2px}
label{display:block;font-size:12px;color:var(--muted);margin:10px 0 4px}
input,select,textarea{width:100%;background:var(--panel2);border:1px solid var(--border);color:var(--text);
border-radius:8px;padding:7px 10px;font-family:inherit;font-size:13px}
textarea{font-family:var(--mono);resize:vertical;min-height:56px}
button.run{width:100%;margin-top:14px;background:linear-gradient(180deg,#0ea5e9,#0284c7);border:0;color:#fff;
font-weight:600;padding:10px;border-radius:10px;cursor:pointer;font-size:14px}
button.run:disabled{opacity:.55;cursor:not-allowed}
.pill{display:inline-flex;gap:6px;align-items:center;padding:4px 11px;border-radius:999px;font-size:12.5px;font-weight:600}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
#graph img{width:100%;display:block}
.log{font-family:var(--mono);font-size:12px;max-height:340px;overflow-y:auto}
.log .r{display:flex;gap:8px;border-bottom:1px dashed rgba(148,163,184,.12);padding:2px 0}
.log .t{color:var(--muted);min-width:56px;text-align:right}
.log .k{min-width:78px;font-weight:600}
.log .m{color:var(--muted);overflow-wrap:anywhere}
.muted{color:var(--muted)}
.runs{font-size:12.5px}
.runs a{display:block;padding:6px 8px;border-radius:8px;text-decoration:none;color:var(--text)}
.runs a:hover{background:var(--panel2)}
.runs .s{font-size:11px;color:var(--muted)}
kbd{background:var(--panel2);border:1px solid var(--border);border-radius:4px;padding:0 5px;font-family:var(--mono);font-size:11px}
</style></head>
<body><div class="wrap">
<header>
  <div><h1>AgentFlow 工作流控制台</h1>
    <div class="muted" style="font-size:12px">多 Agent 编排 · 实时状态 · 一键运行</div></div>
  <div id="head-status" class="muted">未运行</div>
</header>
<div class="cols">
  <div>
    <div class="card">
      <h2>① 流程图（运行时实时着色）</h2>
      <div id="graph"><div class="muted">选择一个工作流开始</div></div>
    </div>
    <div class="card">
      <h2>② 执行日志</h2>
      <div class="log" id="log"><div class="muted">等待运行…</div></div>
    </div>
  </div>
  <div>
    <div class="card">
      <h2>工作流</h2>
      <div id="workflows"><div class="muted">加载中…</div></div>
      <div id="form"></div>
      <button class="run" id="run-btn" disabled>运行</button>
      <div class="muted" style="font-size:11.5px;margin-top:8px">
        提示：故障注入填节点 id，如 <kbd>coder:1</kbd>，用于演示重试与恢复。
      </div>
    </div>
    <div class="card">
      <h2>历史运行</h2>
      <div class="runs" id="runs"><div class="muted">加载中…</div></div>
    </div>
  </div>
</div>
</div>
<script>
let WORKFLOWS = [], CURRENT = null, RUN = null, TIMER = null, SEEN = 0, EVENTS = [];

const STATUS = {ok:'#22c55e',passed:'#22c55e',failed:'#ef4444',skipped:'#f59e0b',partial:'#f59e0b',running:'#3b82f6',pending:'#64748b'};
const TEXT = {ok:'通过',passed:'通过',failed:'失败',skipped:'跳过',partial:'部分完成',running:'运行中',pending:'未执行'};

function esc(s){ return String(s===undefined||s===null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function api(path, opts){ const r = await fetch(path, opts||{}); if(!r.ok) throw new Error(await r.text()); return r.json(); }

async function boot(){
  WORKFLOWS = await api('/api/workflows');
  const box = document.getElementById('workflows');
  box.innerHTML = WORKFLOWS.map(w =>
    `<button class="wf-btn" data-id="${esc(w.id)}">${esc(w.name)}
      <span class="d">${w.node_count} 节点 / ${w.edge_count} 边 · ${esc((w.agents||[]).join(', '))}</span></button>`).join('');
  box.querySelectorAll('.wf-btn').forEach(btn => btn.onclick = () => {
    RUN = null; SEEN = 0; EVENTS = [];          // 切走时退出回放模式
    if (TIMER) { clearTimeout(TIMER); TIMER = null; }
    selectWorkflow(btn.dataset.id);
  });
  loadRuns();
  if (WORKFLOWS.length) await selectWorkflow(WORKFLOWS[0].id);
  // 深链回放：/?run=<run_id> 直接展示某次运行（内存里的或 runs/ 目录下的历史运行）
  const preset = new URLSearchParams(location.search).get('run');
  if (preset) { RUN = {run_id: preset}; poll(); }
}

async function selectWorkflow(id){
  CURRENT = WORKFLOWS.find(w => w.id === id);
  document.querySelectorAll('.wf-btn').forEach(b => b.classList.toggle('on', b.dataset.id === id));
  const spec = await api('/api/workflows/' + id);
  const form = document.getElementById('form');
  const inputs = spec.inputs || {};
  form.innerHTML = Object.keys(inputs).map(k => {
    const meta = inputs[k] || {};
    const val = meta.default !== undefined ? meta.default : '';
    return `<label>${esc(k)} <span class="muted">${esc(meta.description||'')}</span></label>
      <input data-input="${esc(k)}" value="${esc(val)}">`;
  }).join('') + `<label>故障注入 <span class="muted">可选，如 coder:1</span></label>
      <input id="fault" placeholder="node:times">`;
  document.getElementById('run-btn').disabled = false;
  if (RUN) return;   // 正在回放/运行某次运行时，不要覆盖它的图与日志
  document.getElementById('graph').innerHTML = `<img src="/api/workflows/${id}/graph.svg" alt="workflow graph">`;
  document.getElementById('log').innerHTML = '<div class="muted">等待运行…</div>';
}

async function startRun(){
  if (!CURRENT) return;
  const inputs = {};
  document.querySelectorAll('[data-input]').forEach(el => {
    const raw = el.value;
    try { inputs[el.dataset.input] = JSON.parse(raw); } catch(e) { inputs[el.dataset.input] = raw; }
  });
  const faultRaw = (document.getElementById('fault')||{}).value || '';
  const faults = {};
  faultRaw.split(',').map(s => s.trim()).filter(Boolean).forEach(pair => {
    const [node, n] = pair.split(':'); faults[node] = parseInt(n || '1', 10) || 1;
  });
  const btn = document.getElementById('run-btn');
  btn.disabled = true; btn.textContent = '运行中…';
  EVENTS = []; SEEN = 0;
  try {
    RUN = await api('/api/runs', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({workflow: CURRENT.id, inputs, faults})});
    poll();
  } catch(err) {
    document.getElementById('log').innerHTML = '<div class="log"><div class="r"><span class="m">启动失败: ' + esc(err.message) + '</span></div></div>';
    btn.disabled = false; btn.textContent = '运行';
  }
}

async function poll(){
  if (!RUN) return;
  try {
    const data = await api('/api/runs/' + RUN.run_id + '?since=' + SEEN);
    data.events.forEach(e => { EVENTS.push(e); SEEN = Math.max(SEEN, e.seq); });
    renderLog();
    document.getElementById('graph').innerHTML =
      `<img src="/api/runs/${RUN.run_id}/graph.svg?seq=${SEEN}" alt="workflow graph">`;
    const color = STATUS[data.status] || '#64748b';
    document.getElementById('head-status').innerHTML =
      `<span class="pill" style="background:${color}22;color:${color};border:1px solid ${color}66">
        <span class="dot" style="background:${color}"></span>${esc(TEXT[data.status]||data.status)}
        · ${data.duration_s ? data.duration_s + 's' : '运行中'}</span>`;
    if (data.finished) {
      const btn = document.getElementById('run-btn');
      btn.disabled = false; btn.textContent = '再次运行';
      document.getElementById('head-status').innerHTML +=
        ` <a href="/api/runs/${RUN.run_id}/report" target="_blank" style="margin-left:8px">查看完整报告 →</a>`;
      loadRuns();
      return;
    }
  } catch(err) { /* 轮询失败忽略 */ }
  TIMER = setTimeout(poll, 600);
}

function renderLog(){
  const el = document.getElementById('log');
  if (!EVENTS.length) { el.innerHTML = '<div class="muted">启动中…</div>'; return; }
  const colors = {node_end:'#7dd3fc',node_start:'#93c5fd',edge:'#94a3b8',retry:'#fbbf24',
    message:'#c084fc',error:'#f87171',wave_start:'#38bdf8',run_start:'#22c55e',run_end:'#22c55e',
    checkpoint:'#475569'};
  el.innerHTML = EVENTS.map(e =>
    `<div class="r"><span class="t">${(e.ts? new Date(e.ts*1000).toLocaleTimeString('zh-CN',{hour12:false}):'')}</span>
     <span class="k" style="color:${colors[e.type]||'#94a3b8'}">${esc(e.type)}</span>
     <span class="m">${esc(detail(e))}</span></div>`).join('');
  el.scrollTop = el.scrollHeight;
}

function detail(e){
  switch(e.type){
    case 'node_start': return `${e.node_id} (${e.agent}) 第 ${e.visit} 次`;
    case 'node_end': return `${e.node_id} → ${e.status} ${e.duration_ms}ms ${e.detail||''}`;
    case 'edge': return `${e.fired?'✓':'✗'} ${e.source} → ${e.target}${e.label?' ('+e.label+')':''}`;
    case 'wave_start': return `波次 ${e.wave}: ${(e.nodes||[]).join(', ')}`;
    case 'message': return e.summary||'';
    case 'retry': return `${e.node_id} 重试 #${e.attempt}: ${e.error||''}`;
    case 'error': return e.message||'';
    case 'run_end': return `${e.status} ${e.duration_s}s`;
    case 'run_start': return `${e.workflow} (${e.node_count} 节点)`;
    default: return '';
  }
}

async function loadRuns(){
  const runs = await api('/api/runs');
  const el = document.getElementById('runs');
  if (!runs.length) { el.innerHTML = '<div class="muted">暂无运行</div>'; return; }
  el.innerHTML = runs.slice(0, 12).map(r => {
    const c = STATUS[r.status] || '#64748b';
    return `<a href="/api/runs/${r.run_id}/report" target="_blank">
      <span class="dot" style="background:${c}"></span> ${esc(r.run_id)}
      <span class="s">${esc(r.status)} · ${r.duration_s}s</span></a>`;
  }).join('');
}

document.getElementById('run-btn').onclick = startRun;
boot();
</script>
</body></html>
"""


class RunSession:
    """一次运行的会话（供轮询接口读取）。"""

    def __init__(self, run_id: str, spec: WorkflowSpec, inputs: Mapping[str, Any],
                 faults: Mapping[str, int], run_root: Path) -> None:
        self.run_id = run_id
        self.spec = spec
        self.inputs = dict(inputs)
        self.faults = dict(faults)
        self.run_root = run_root
        self.status = "running"
        self.error: str | None = None
        self.events: list[dict[str, Any]] = []
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.summary: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._execute, name=f"run-{run_id}", daemon=True)

    # ---- 事件收集 --------------------------------------------------
    def on_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.events.append(event)
            if event["type"] == "run_end":
                self.status = event.get("status", "failed")
                self.error = event.get("error")
                self.finished_at = time.time()

    def events_since(self, since: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self.events if e.get("seq", 0) > since]

    @property
    def finished(self) -> bool:
        return self.finished_at is not None

    @property
    def duration_s(self) -> float:
        return round((self.finished_at or time.time()) - self.started_at, 3)

    def node_statuses(self) -> dict[str, dict[str, Any]]:
        """从事件流推导每个节点的当前状态（用于实时渲染流程图）。"""
        with self._lock:
            events = list(self.events)
        return statuses_from_events(self.spec, events)

    # ---- 执行 ------------------------------------------------------
    def _execute(self) -> None:
        try:
            engine = Engine(
                default_registry,
                run_root=self.run_root,
                parallelism=int(self.spec.settings.get("parallelism", 4)),
                max_steps=int(self.spec.settings.get("max_steps", 200)),
                echo=False,
                on_event=self.on_event,
                fault_injection=dict(self.faults),
            )
            result = engine.run(self.spec, self.inputs, run_id=self.run_id)
            self.summary = result.summary
            self.status = result.status
            self.error = result.error
        except Exception as exc:  # noqa: BLE001 - 任何异常都要反馈给前端
            self.status = "failed"
            self.error = f"{type(exc).__name__}: {exc}"
            self.on_event({"seq": len(self.events) + 1, "ts": time.time(), "type": "error",
                           "run_id": self.run_id, "message": self.error})
        finally:
            self.finished_at = self.finished_at or time.time()


class RunManager:
    """内存中的运行注册表（进程级，重启即清空；历史运行从 runs/ 目录读取）。"""

    def __init__(self, run_root: Path) -> None:
        self.run_root = run_root
        self.sessions: dict[str, RunSession] = {}
        self._lock = threading.Lock()

    def create(self, workflow_id: str, inputs: Mapping[str, Any],
               faults: Mapping[str, int]) -> RunSession:
        spec = load_workflow(workflow_id, registry=default_registry)
        resolved = dict(inputs)
        for key, schema in (spec.inputs_schema or {}).items():
            if key not in resolved and isinstance(schema, Mapping) and "default" in schema:
                resolved[key] = schema["default"]
        run_id = f"{spec.id}-{time.strftime('%Y%m%d-%H%M%S')}-" + "".join(
            random.choices(string.ascii_lowercase + string.digits, k=4)
        )
        session = RunSession(run_id, spec, resolved, faults, self.run_root)
        with self._lock:
            self.sessions[run_id] = session
        session.thread.start()
        return session

    def get(self, run_id: str) -> RunSession | None:
        with self._lock:
            return self.sessions.get(run_id)


def statuses_from_events(spec: WorkflowSpec, events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """把事件流折算成每个节点的状态（实时渲染与历史回放共用）。"""
    statuses: dict[str, dict[str, Any]] = {
        node_id: {"status": "pending", "visits": 0} for node_id in spec.node_ids
    }
    for event in events:
        node_id = event.get("node_id")
        if node_id is None:
            continue
        statuses.setdefault(node_id, {"status": "pending", "visits": 0})
        if event["type"] == "node_start":
            statuses[node_id]["status"] = "running"
            statuses[node_id]["visits"] = statuses[node_id].get("visits", 0) + 1
        elif event["type"] == "node_end":
            statuses[node_id]["status"] = event.get("status", "ok")
            statuses[node_id]["duration_ms"] = event.get("duration_ms")
    return statuses


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentFlow/1.0"
    manager: RunManager = None  # type: ignore[assignment]

    # ---- 路由 ------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/":
                return self._html(INDEX_HTML)
            if path == "/api/workflows":
                return self._json(list_workflows())
            if path.startswith("/api/workflows/") and path.endswith("/graph.svg"):
                workflow_id = path[len("/api/workflows/"):-len("/graph.svg")]
                spec = load_workflow(workflow_id, registry=default_registry)
                statuses = {nid: {"status": "pending", "visits": 0} for nid in spec.node_ids}
                svg = render_graph_svg(spec, compute_layout(spec, "TB"), statuses)
                return self._svg(svg)
            if path.startswith("/api/workflows/"):
                workflow_id = path[len("/api/workflows/"):]
                spec = load_workflow(workflow_id, registry=default_registry)
                payload = spec.to_dict()
                payload["agents"] = default_registry.describe()
                return self._json(payload)
            if path == "/api/agents":
                return self._json(default_registry.describe())
            if path == "/api/rules":
                from ..agents.linting import rule_catalog

                return self._json(rule_catalog())
            if path == "/api/runs":
                return self._json(self._list_runs())
            if path.startswith("/api/runs/"):
                tail = path[len("/api/runs/"):]
                if tail.endswith("/graph.svg"):
                    run_id = tail[:-len("/graph.svg")]
                    session = self.manager.get(run_id)
                    if session is not None:
                        svg = render_graph_svg(
                            session.spec, compute_layout(session.spec, "TB"), session.node_statuses()
                        )
                        return self._svg(svg)
                    replay = self._load_historical(run_id)
                    if replay is None:
                        return self._error(404, "运行不存在")
                    spec, events, _ = replay
                    svg = render_graph_svg(spec, compute_layout(spec, "TB"),
                                           statuses_from_events(spec, events))
                    return self._svg(svg)
                if tail.endswith("/report"):
                    run_id = tail[:-len("/report")]
                    return self._report(run_id)
                run_id = tail
                since = int(query.get("since", ["0"])[0] or 0)
                session = self.manager.get(run_id)
                if session is None:
                    return self._historical_payload(run_id, since)
                return self._json({
                    "run_id": run_id,
                    "workflow": session.spec.id,
                    "status": session.status,
                    "error": session.error,
                    "finished": session.finished,
                    "duration_s": session.duration_s,
                    "events": session.events_since(since),
                    "summary": session.summary,
                    "report": f"/api/runs/{run_id}/report",
                })
            return self._error(404, "未知路径")
        except Exception as exc:  # noqa: BLE001 - 接口层兜底
            traceback.print_exc()
            return self._error(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/runs":
            return self._error(404, "未知路径")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            workflow_id = body.get("workflow")
            if not workflow_id:
                return self._error(400, "缺少 workflow")
            session = self.manager.create(
                workflow_id,
                body.get("inputs") or {},
                {str(k): int(v) for k, v in (body.get("faults") or {}).items()},
            )
            return self._json({"run_id": session.run_id, "status": session.status}, status=202)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._error(500, f"{type(exc).__name__}: {exc}")

    # ---- 输出 ------------------------------------------------------
    def _load_historical(self, run_id: str):
        """从 runs/<id>/ 读取一次历史运行（workflow.json + events.jsonl + summary.json）。"""
        if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
            return None
        run_dir = self.manager.run_root / run_id
        workflow_file = run_dir / "workflow.json"
        events_file = run_dir / "events.jsonl"
        if not workflow_file.exists():
            return None
        try:
            spec = WorkflowSpec.from_dict(json.loads(workflow_file.read_text(encoding="utf-8-sig")))
            events: list[dict[str, Any]] = []
            if events_file.exists():
                for line in events_file.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            summary: dict[str, Any] = {}
            summary_file = run_dir / "summary.json"
            if summary_file.exists():
                summary = json.loads(summary_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        return spec, events, summary

    def _historical_payload(self, run_id: str, since: int) -> None:
        replay = self._load_historical(run_id)
        if replay is None:
            return self._error(404, "运行不存在")
        spec, events, summary = replay
        return self._json({
            "run_id": run_id,
            "workflow": spec.id,
            "status": summary.get("status", "passed"),
            "error": summary.get("error"),
            "finished": True,
            "duration_s": summary.get("duration_s", 0),
            "events": [e for e in events if e.get("seq", 0) > since],
            "summary": summary,
            "history": True,
            "report": f"/api/runs/{run_id}/report",
        })

    def _list_runs(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        with self.manager._lock:  # noqa: SLF001 - 同一模块内
            sessions = list(self.manager.sessions.values())
        for session in sorted(sessions, key=lambda s: s.started_at, reverse=True):
            seen.add(session.run_id)
            rows.append({
                "run_id": session.run_id,
                "workflow": session.spec.id,
                "status": session.status,
                "duration_s": session.duration_s,
            })
        if self.manager.run_root.exists():
            for path in sorted(self.manager.run_root.iterdir(), reverse=True):
                summary_file = path / "summary.json"
                if not path.is_dir() or not summary_file.exists() or path.name in seen:
                    continue
                try:
                    data = json.loads(summary_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                rows.append({
                    "run_id": data.get("run_id", path.name),
                    "workflow": data.get("workflow", ""),
                    "status": data.get("status", ""),
                    "duration_s": data.get("duration_s", 0),
                })
        return rows

    def _report(self, run_id: str) -> None:
        if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
            return self._error(400, "非法的运行 id")
        target = (self.manager.run_root / run_id / "report.html")
        try:
            resolved = target.resolve()
            root = self.manager.run_root.resolve()
            if not str(resolved).startswith(str(root)) or not resolved.exists():
                return self._error(404, "报告不存在")
        except OSError:
            return self._error(404, "报告不存在")
        body = resolved.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _svg(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message, "status": code}, status=code)

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音访问日志，保持终端干净
        return


def serve(host: str = "127.0.0.1", port: int = 7860, run_root: str | Path = "runs") -> None:
    """启动控制台（阻塞）。"""
    Handler.manager = RunManager(Path(run_root))
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
