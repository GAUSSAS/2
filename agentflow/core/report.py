"""运行报告生成：单文件 HTML（内嵌 SVG 流程图 + 时间线 + 消息 + 作品）。

不依赖任何前端框架或 CDN，双击即可离线打开 —— 这对课程作业的演示与提交很重要。

包含四块内容：

1. **流程图**：节点按运行状态着色（通过/失败/跳过/未执行），边上标注条件；
2. **执行时间线**：每个波次、节点的开始/结束、边判定、重试与消息；
3. **协作明细**：Agent 之间的消息表 + 作品仓库的版本演进（代码带 diff）；
4. **原始数据**：summary / 工作流定义 / 状态快照，便于复盘。
"""

from __future__ import annotations

import difflib
import html
import json
from typing import Any, Iterable, Mapping

from .graph import WorkflowSpec
from .layout import Layout, compute_layout, node_status
from .state import SharedState

STATUS_COLORS = {
    "ok": "#22c55e",
    "passed": "#22c55e",
    "failure": "#ef4444",
    "failed": "#ef4444",
    "skipped": "#f59e0b",
    "partial": "#f59e0b",
    "running": "#3b82f6",
    "pending": "#64748b",
}
STATUS_TEXT = {
    "ok": "通过",
    "passed": "通过",
    "failed": "失败",
    "failure": "失败",
    "skipped": "跳过",
    "partial": "部分完成",
    "running": "运行中",
    "pending": "未执行",
}
KIND_COLORS = {
    "author": "#6366f1",
    "review": "#0ea5e9",
    "gate": "#a855f7",
    "fix": "#f97316",
    "deliver": "#22c55e",
    "risk": "#ef4444",
    "task": "#64748b",
}

CSS = """
:root {
  --bg: #0b1120; --panel: #111a2e; --panel-2: #0f172a; --border: #1f2b45;
  --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8; --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; font-size: 14px; line-height: 1.6; }
a { color: var(--accent); }
.wrap { max-width: 1240px; margin: 0 auto; padding: 28px 20px 64px; }
header.top { display: flex; flex-wrap: wrap; gap: 16px; align-items: center; justify-content: space-between;
  border-bottom: 1px solid var(--border); padding-bottom: 18px; margin-bottom: 22px; }
h1 { font-size: 21px; margin: 0 0 6px; }
h2 { font-size: 16px; margin: 0 0 12px; display: flex; align-items: center; gap: 8px; }
h3 { font-size: 14px; margin: 16px 0 8px; color: var(--muted); font-weight: 600; }
.sub { color: var(--muted); font-size: 12.5px; }
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px; border-radius: 999px;
  font-weight: 600; font-size: 13px; border: 1px solid transparent; }
.grid { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr); gap: 18px; align-items: start; }
@media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; }
.card + .card { margin-top: 18px; }
.metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(118px, 1fr)); gap: 12px; }
.metric { background: var(--panel-2); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; }
.metric .v { font-size: 20px; font-weight: 700; font-variant-numeric: tabular-nums; }
.metric .k { color: var(--muted); font-size: 11.5px; letter-spacing: .04em; text-transform: uppercase; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 9px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; white-space: nowrap; }
tr:last-child td { border-bottom: none; }
code, pre, .mono { font-family: var(--mono); }
code { background: rgba(56,189,248,.09); color: #7dd3fc; padding: 1px 5px; border-radius: 4px; font-size: 12.5px; }
pre { background: var(--panel-2); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px;
  overflow-x: auto; font-size: 12.5px; line-height: 1.55; margin: 0; }
pre.diff .add { color: #4ade80; } pre.diff .del { color: #f87171; } pre.diff .hunk { color: #7dd3fc; }
details { border: 1px solid var(--border); border-radius: 10px; padding: 10px 14px; background: var(--panel-2); }
details + details { margin-top: 10px; }
summary { cursor: pointer; color: var(--muted); font-size: 13px; }
svg { width: 100%; max-width: 720px; height: auto; display: block; margin: 0 auto; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--muted); margin-top: 10px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.timeline { max-height: 460px; overflow-y: auto; font-family: var(--mono); font-size: 12.3px; }
.tl-row { display: flex; gap: 10px; padding: 3px 0; border-bottom: 1px dashed rgba(148,163,184,.12); }
.tl-t { color: var(--muted); min-width: 62px; text-align: right; font-variant-numeric: tabular-nums; }
.tl-k { min-width: 86px; font-weight: 600; }
.tl-m { color: var(--muted); overflow-wrap: anywhere; }
.sev-error { color: #f87171; font-weight: 600; }
.sev-warning { color: #fbbf24; font-weight: 600; }
.sev-info { color: #94a3b8; }
.tabs { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
.tab { padding: 4px 11px; border-radius: 999px; border: 1px solid var(--border); background: var(--panel-2);
  color: var(--muted); font-size: 12px; cursor: pointer; }
.tab.on { border-color: var(--accent); color: var(--accent); }
.panel { display: none; } .panel.on { display: block; }
.note { border-left: 3px solid var(--accent); padding: 8px 12px; background: rgba(56,189,248,.07);
  border-radius: 0 8px 8px 0; color: var(--muted); font-size: 13px; }
footer { margin-top: 28px; color: var(--muted); font-size: 12px; text-align: center; }
"""

JS = """
function showTab(group, id) {
  document.querySelectorAll('[data-group="' + group + '"]').forEach(function (el) {
    el.classList.toggle('on', el.dataset.tab === id);
  });
  document.querySelectorAll('.panel[data-' + 'panel="' + group + '"]').forEach(function (el) {
    el.classList.toggle('on', el.dataset.panelId === id);
  });
}
"""


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------
def build_report(*, spec: WorkflowSpec, state: SharedState, events: Iterable[Mapping[str, Any]],
                 summary: Mapping[str, Any]) -> str:
    """生成完整报告 HTML 字符串。"""
    event_list = list(events)
    status = str(summary.get("status") or state.status)
    layout = compute_layout(spec, "TB")
    statuses = node_status(spec, state)
    stats = summary.get("stats") or {}
    duration = summary.get("duration_s", 0)

    graph = render_graph_svg(spec, layout, statuses, show_conditions=True)
    review = _review_payload(state)
    findings = list(review.get("findings") or [])
    artifacts = _render_artifacts(state)
    review_title = "④ 审查发现与修复" if review.get("notes") is None else "④ 审校意见与修订"

    body = f"""
<header class="top">
  <div>
    <h1>{html.escape(spec.name or spec.id)}</h1>
    <div class="sub">工作流 <code>{html.escape(spec.id)}</code> v{html.escape(spec.version)} ·
      运行 <code>{html.escape(state.run_id)}</code> · {_fmt_time(summary.get("started_at"))}</div>
  </div>
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    {_status_pill(status)}
    <span class="sub">用时 {duration}s</span>
  </div>
</header>

<div class="metrics">
  {_metric(len(state.node_runs), "节点执行次数")}
  {_metric(stats.get("waves", 0), "波次数")}
  {_metric(len(state.bus), "Agent 消息")}
  {_metric(len(state.artifacts), "作品数")}
  {_metric(stats.get("retries", 0), "重试次数")}
  {_metric(_findings_metric_value(findings, review), _findings_metric_label(review))}
  {_metric(_score_text(summary, state), "质量评分")}
  {_metric(stats.get("edges_fired", 0), "边触发次数")}
</div>

<div class="grid" style="margin-top:18px">
  <div class="card">
    <h2>① 工作流执行图</h2>
    {graph}
    {_legend()}
    <div class="note" style="margin-top:12px">
      同层节点表示同一波次并行执行（如两路审查）；虚线为回边（循环）；箭头上的标签是边条件。
    </div>
  </div>
  <div class="card">
    <h2>② 运行概览</h2>
    <table>
      <tr><th>结论</th><td>{_status_text(status)}</td></tr>
      <tr><th>入参</th><td class="mono">{_kv_preview(state.inputs)}</td></tr>
      <tr><th>循环轮次</th><td>{_counter_text(state)}</td></tr>
      <tr><th>终态变量</th><td class="mono">{_kv_preview({k: v for k, v in state.variables.items() if not isinstance(v, (dict, list))})}</td></tr>
      <tr><th>错误</th><td>{html.escape(str(summary.get('error') or '无'))}</td></tr>
    </table>
    <h3>作品版本</h3>
    <table>
      <thead><tr><th>作品</th><th>版本</th><th>作者</th></tr></thead>
      <tbody>
        {''.join(f"<tr><td><code>{html.escape(name)}</code></td><td>v{art.version}</td>"
                 f"<td>{html.escape(art.author)}</td></tr>"
                 for name, art in state.artifacts.items())}
      </tbody>
    </table>
    <h3>执行日志</h3>
    <div class="timeline">{_render_timeline(event_list, summary)}</div>
  </div>
</div>

<div class="card" style="margin-top:18px">
  <h2>③ Agent 协作</h2>
  {_render_messages(state)}
</div>

<div class="card" style="margin-top:18px">
  <h2>{review_title}</h2>
  {_render_findings(findings, review, state)}
</div>

<div class="card" style="margin-top:18px">
  <h2>⑤ 作品与代码演进</h2>
  {artifacts}
</div>

<div class="card" style="margin-top:18px">
  <h2>⑥ 原始数据</h2>
  <details><summary>summary.json</summary><pre>{html.escape(json.dumps(summary, ensure_ascii=False, indent=2, default=str))}</pre></details>
  <details><summary>workflow 定义</summary><pre>{html.escape(spec.to_json())}</pre></details>
  <details><summary>state 快照（含作品历史）</summary><pre>{html.escape(json.dumps(state.to_dict(with_artifact_content=False), ensure_ascii=False, indent=2, default=str))}</pre></details>
  <details><summary>完整事件流 (events)</summary><pre>{html.escape(json.dumps(event_list, ensure_ascii=False, indent=2, default=str))}</pre></details>
</div>

<footer>AgentFlow · 生成于 {_fmt_time(summary.get('finished_at'))} · 本文件离线自包含，可直接归档</footer>
"""
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>AgentFlow · {html.escape(spec.name or spec.id)} · {html.escape(state.run_id)}</title>"
        f"<style>{CSS}</style></head><body><div class=\"wrap\">{body}</div>"
        f"<script>{JS}</script></body></html>"
    )


def render_graph_svg(spec: WorkflowSpec, layout: Layout, statuses: Mapping[str, Mapping[str, Any]],
                     *, show_conditions: bool = True, show_status: bool = True,
                     width: float | None = None) -> str:
    """把工作流渲染成 SVG。

    Args:
        show_status: True 时节点按运行状态着色（报告/控制台用）；
            False 时按节点类型着色、第三行显示 ``kind · id``（文档配图用）。
    """
    svg_w = width or layout.width
    svg_h = layout.height
    parts: list[str] = [
        f'<svg viewBox="0 0 {svg_w:.0f} {svg_h:.0f}" xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="workflow graph">',
        "<defs>",
        f'<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="#64748b"/></marker>',
        f'<marker id="arrow-back" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="#f59e0b"/></marker>',
        "</defs>",
    ]

    for edge_index, edge in enumerate(spec.edges):
        if edge.source not in layout.boxes or edge.target not in layout.boxes:
            continue
        (x1, y1), (x2, y2), is_back = layout.edge_geometry(edge.source, edge.target)
        if is_back:
            bulge = 58 + (edge_index % 3) * 22
            path = (f"M {x1:.1f} {y1:.1f} C {x1 + bulge:.1f} {y1:.1f}, "
                    f"{x2 + bulge:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}")
            color, dash, marker = "#f59e0b", "stroke-dasharray=\"6 4\"", "arrow-back"
        elif layout.direction == "TB":
            dy = max(28.0, (y2 - y1) * 0.5)
            path = (f"M {x1:.1f} {y1:.1f} C {x1:.1f} {y1 + dy:.1f}, "
                    f"{x2:.1f} {y2 - dy:.1f}, {x2:.1f} {y2:.1f}")
            color, dash, marker = "#475569", "", "arrow"
        else:
            dx = max(40.0, abs(x2 - x1) * 0.45)
            path = (f"M {x1:.1f} {y1:.1f} C {x1 + dx:.1f} {y1:.1f}, "
                    f"{x2 - dx:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}")
            color, dash, marker = "#475569", "", "arrow"
        tooltip = f"{edge.source} → {edge.target}" + (f"  [{edge.when}]" if edge.when else "")
        parts.append(
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.6" {dash} '
            f'marker-end="url(#{marker})"><title>{html.escape(tooltip)}</title></path>'
        )
        if show_conditions and edge.label:
            if is_back:
                mx = max(x1, x2) + 12
                my = (y1 + y2) / 2 + 4
                anchor = "start"
            elif layout.direction == "TB":
                mx, my = (x1 + x2) / 2 + 6, (y1 + y2) / 2 + 4
                anchor = "start"
            else:
                mx, my, anchor = (x1 + x2) / 2, (y1 + y2) / 2 - 6, "middle"
            parts.append(
                f'<text x="{mx:.0f}" y="{my:.0f}" fill="#94a3b8" font-size="10" text-anchor="{anchor}" '
                f'font-family="ui-monospace, monospace">{html.escape(_fit(edge.label, 96, 10))}</text>'
            )

    for node in spec.nodes:
        box = layout.boxes[node.id]
        info = statuses.get(node.id, {})
        status = str(info.get("status", "pending"))
        visits = int(info.get("visits") or 0)
        duration = info.get("duration_ms")
        if show_status:
            color = STATUS_COLORS.get(status, "#64748b")
            kind_color = KIND_COLORS.get(node.kind, "#64748b")
            third_line = STATUS_TEXT.get(status, status) + (f" ×{visits}" if visits > 1 else "")
            tooltip_suffix = f" · {STATUS_TEXT.get(status, status)}" + (
                f" · {duration}ms" if duration is not None else ""
            )
        else:
            kind_color = KIND_COLORS.get(node.kind, "#64748b")
            color = kind_color
            third_line = f"{node.kind} · {node.id}"
            tooltip_suffix = f" · {node.kind}"
        parts.append(
            f'<g><title>{html.escape(node.name or node.id)} · {html.escape(node.agent)}'
            + html.escape(tooltip_suffix) + '</title>'
            f'<rect x="{box.x:.1f}" y="{box.y:.1f}" width="{box.width}" height="{box.height}" rx="10" '
            f'fill="#111a2e" stroke="{color}" stroke-width="2"/>'
            f'<rect x="{box.x:.1f}" y="{box.y:.1f}" width="5" height="{box.height}" rx="2.5" fill="{kind_color}"/>'
        )
        parts.append(
            f'<text x="{box.x + 16:.0f}" y="{box.y + 25:.0f}" fill="#e2e8f0" font-size="13" font-weight="600">'
            f'{html.escape(_fit(node.name or node.id, box.width - 32, 13))}</text>'
            f'<text x="{box.x + 16:.0f}" y="{box.y + 44:.0f}" fill="#94a3b8" font-size="11">'
            f'{html.escape(_fit(node.agent, box.width - 32, 11))}</text>'
            f'<text x="{box.x + 16:.0f}" y="{box.y + 63:.0f}" fill="{color}" font-size="10.5" '
            f'font-family="ui-monospace, monospace">{html.escape(_fit(third_line, box.width - 32, 10.5))}</text>'
        )
        if box.width > 60:
            parts.append(
                f'<circle cx="{box.right - 14:.0f}" cy="{box.y + 13:.0f}" r="5" fill="{color}"/>'
            )
        parts.append("</g>")

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 片段渲染
# ---------------------------------------------------------------------------
def _status_pill(status: str) -> str:
    color = STATUS_COLORS.get(status, "#64748b")
    return (
        f'<span class="pill" style="background:{color}22;border-color:{color}66;color:{color}">'
        f'<span class="dot" style="background:{color}"></span>{_status_text(status)}</span>'
    )


def _status_text(status: str) -> str:
    return STATUS_TEXT.get(status, status)


def _findings_metric_value(findings: list[Mapping[str, Any]], review: Mapping[str, Any]) -> Any:
    if review.get("notes") is not None:
        return len(review.get("notes") or [])
    return len(findings)


def _findings_metric_label(review: Mapping[str, Any]) -> str:
    return "审校意见" if review.get("notes") is not None else "末轮审查发现"


def _metric(value: Any, label: str) -> str:
    return f'<div class="metric"><div class="v">{html.escape(str(value))}</div><div class="k">{html.escape(label)}</div></div>'


def _legend() -> str:
    items = "".join(
        f'<span><span class="dot" style="background:{STATUS_COLORS[key]}"></span>{label}</span>'
        for key, label in (("ok", "通过"), ("failed", "失败"), ("skipped", "跳过"),
                           ("running", "运行中"), ("pending", "未执行"))
    )
    kinds = "".join(
        f'<span><span class="dot" style="background:{color}"></span>{label}</span>'
        for label, color in (("编码/写作", KIND_COLORS["author"]), ("审查", KIND_COLORS["review"]),
                             ("门禁", KIND_COLORS["gate"]), ("修复", KIND_COLORS["fix"]),
                             ("交付", KIND_COLORS["deliver"]), ("升级", KIND_COLORS["risk"]))
    )
    return f'<div class="legend">{items}</div><div class="legend">{kinds}</div>'


def _counter_text(state: SharedState) -> str:
    """把计数器渲染成“审查 3 轮 / 修复 2 轮”这类可读文本。"""
    labels = {
        "review_round": "审查轮次",
        "fix_round": "修复轮次",
        "draft_round": "稿件版本",
        "iteration": "门禁轮次",
    }
    parts = [f"{labels.get(key, key)} {value}" for key, value in state.counters.items() if key != "step"]
    return html.escape(" / ".join(parts)) if parts else "-"


def _score_text(summary: Mapping[str, Any], state: SharedState) -> Any:
    review = _review_payload(state)
    if review.get("score") is not None:
        return review["score"]
    variables = state.variables.get("score")
    return variables if variables is not None else "-"


def _review_payload(state: SharedState) -> dict[str, Any]:
    for name in ("review", "editorial_review"):
        artifact = state.artifact(name)
        if artifact is not None and isinstance(artifact.content, Mapping):
            return dict(artifact.content)
    return {}


def _render_timeline(events: Iterable[Mapping[str, Any]], summary: Mapping[str, Any]) -> str:
    rows: list[str] = []
    started = float(summary.get("started_at") or 0)
    colors = {
        "node_end": "#7dd3fc", "node_start": "#93c5fd", "edge": "#94a3b8",
        "retry": "#fbbf24", "message": "#c084fc", "error": "#f87171", "warning": "#fbbf24",
        "wave_start": "#38bdf8", "run_start": "#22c55e", "run_end": "#22c55e",
        "checkpoint": "#475569", "run_resumed": "#fbbf24",
    }
    for event in events:
        etype = str(event.get("type"))
        offset = float(event.get("ts") or started) - started
        detail = _event_detail(event)
        rows.append(
            f'<div class="tl-row"><span class="tl-t">+{offset:.2f}s</span>'
            f'<span class="tl-k" style="color:{colors.get(etype, "#94a3b8")}">{html.escape(etype)}</span>'
            f'<span class="tl-m">{html.escape(detail)}</span></div>'
        )
    return "".join(rows) if rows else '<div class="sub">无事件</div>'


def _event_detail(event: Mapping[str, Any]) -> str:
    etype = event.get("type")
    if etype == "node_start":
        return f"{event.get('node_id')} ({event.get('agent')}) 第 {event.get('visit')} 次执行"
    if etype == "node_end":
        extra = f" · {event.get('detail')}" if event.get("detail") else ""
        return f"{event.get('node_id')} → {event.get('status')} {event.get('duration_ms')}ms{extra}"
    if etype == "edge":
        mark = "✓" if event.get("fired") else "✗"
        label = f" ({event.get('label')})" if event.get("label") else ""
        reason = f" [{event.get('reason')}]" if event.get("when") else ""
        return f"{mark} {event.get('source')} → {event.get('target')}{label}{reason}"
    if etype == "wave_start":
        return f"波次 {event.get('wave')}: {', '.join(event.get('nodes') or [])}"
    if etype == "message":
        return str(event.get("summary"))
    if etype == "retry":
        return f"{event.get('node_id')} 第 {event.get('attempt')} 次失败：{event.get('error')}"
    if etype in ("error", "warning"):
        return str(event.get("message"))
    if etype in ("run_start", "run_end"):
        return f"{event.get('status', '')} {event.get('workflow', '')}".strip()
    return ""


def _render_messages(state: SharedState) -> str:
    if not len(state.bus):
        return '<div class="sub">本次运行没有消息</div>'
    rows = []
    for message in state.bus.all():
        rows.append(
            f"<tr><td class='mono'>{html.escape(message.id)}</td>"
            f"<td>{html.escape(message.sender)} → {html.escape(message.recipient)}</td>"
            f"<td><code>{html.escape(message.kind)}</code></td>"
            f"<td>{html.escape(_payload_text(message.payload))}</td></tr>"
        )
    return (
        '<table><thead><tr><th>#</th><th>方向</th><th>类型</th><th>内容</th></tr></thead><tbody>'
        + "".join(rows) + "</tbody></table>"
    )


def _payload_text(payload: Mapping[str, Any]) -> str:
    for key in ("summary", "message", "reason"):
        value = payload.get(key)
        if value:
            return _clip(str(value), 160)
    return _clip(json.dumps(payload, ensure_ascii=False), 160)


def _render_findings(findings: list[Mapping[str, Any]], review: Mapping[str, Any],
                     state: SharedState) -> str:
    if not findings and not review:
        return '<div class="sub">没有审查发现（该工作流可能不含审查节点）</div>'
    if not findings and review.get("notes") is not None:
        return _render_editorial(review, state)
    by_severity: dict[str, list[Mapping[str, Any]]] = {}
    for finding in findings:
        by_severity.setdefault(str(finding.get("severity")), []).append(finding)
    tabs = "".join(
        f'<button class="tab {"on" if index == 0 else ""}" data-group="sev" data-tab="{key}" '
        f'onclick="showTab(&quot;sev&quot;,&quot;{key}&quot;)">{label}（{len(by_severity.get(key, []))}）</button>'
        for index, (key, label) in enumerate((("error", "阻塞"), ("warning", "警告"), ("info", "提示")))
    )
    panels = []
    for index, key in enumerate(("error", "warning", "info")):
        items = by_severity.get(key, [])
        if items:
            body = (
                "<table><thead><tr><th>规则</th><th>行</th><th>说明</th><th>建议</th></tr></thead><tbody>"
                + "".join(
                    f"<tr><td><code>{html.escape(str(f.get('rule_id')))}</code>"
                    f"{' · ' + html.escape(str(f.get('symbol'))) if f.get('symbol') else ''}</td>"
                    f"<td>{f.get('line')}</td><td>{html.escape(str(f.get('message')))}</td>"
                    f"<td class='sub'>{html.escape(str(f.get('hint') or ''))}</td></tr>"
                    for f in items
                )
                + "</tbody></table>"
            )
        else:
            body = '<div class="sub">无</div>'
        panels.append(
            f'<div class="panel {"on" if index == 0 else ""}" data-panel="sev" data-panel-id="{key}">{body}</div>'
        )
    return (f'<div class="tabs">{tabs}</div>{"".join(panels)}'
            f"{_render_rounds(state)}{_render_patches(state)}")


def _render_editorial(review: Mapping[str, Any], state: SharedState) -> str:
    """内容创作流程：渲染审校意见与各节指标。"""
    notes = list(review.get("notes") or [])
    metrics = list(review.get("metrics") or [])
    history = state.artifact("editorial_review")
    rounds = ""
    if history is not None and len(history.history) > 1:
        rows = "".join(
            f"<tr><td>v{entry['version']}</td><td>{html.escape(str((entry.get('content') or {}).get('verdict', '')))}</td>"
            f"<td>{(entry.get('content') or {}).get('blocking', 0)}</td>"
            f"<td>{(entry.get('content') or {}).get('score', '-')}</td>"
            f"<td>{(entry.get('content') or {}).get('char_count', '-')}</td></tr>"
            for entry in history.history
            if isinstance(entry.get("content"), Mapping)
        )
        rounds = ("<h3>审校轮次演进</h3><table><thead><tr><th>版本</th><th>结论</th>"
                  "<th>待改</th><th>评分</th><th>字数</th></tr></thead><tbody>" + rows + "</tbody></table>")
    body = ""
    if metrics:
        body += ("<h3>各节字数与要点覆盖</h3><table><thead><tr><th>小节</th><th>字数</th>"
                 "<th>目标</th><th>达标</th><th>缺失要点</th></tr></thead><tbody>"
                 + "".join(
                     f"<tr><td>{html.escape(str(m.get('heading')))}</td><td>{m.get('chars')}</td>"
                     f"<td>{m.get('target')}</td>"
                     f"<td>{'✅' if m.get('chars', 0) >= m.get('target', 1) * 0.8 else '❌'}</td>"
                     f"<td class='sub'>{html.escape(', '.join(m.get('missing_points') or []) or '无')}</td></tr>"
                     for m in metrics
                 ) + "</tbody></table>")
    if notes:
        body += ("<h3>修改意见</h3><table><thead><tr><th>类型</th><th>小节</th><th>意见</th></tr></thead><tbody>"
                 + "".join(
                     f"<tr><td><code>{html.escape(str(n.get('type')))}</code></td>"
                     f"<td>{html.escape(str(n.get('heading') or '-'))}</td>"
                     f"<td>{html.escape(str(n.get('message')))}</td></tr>"
                     for n in notes
                 ) + "</tbody></table>")
    else:
        body += '<div class="note">审校通过，无修改意见。</div>'
    return rounds + body


def _render_rounds(state: SharedState) -> str:
    """用汇总作品的版本历史还原“每一轮的评审结论”。"""
    artifact = state.artifact("review")
    if artifact is None or len(artifact.history) == 0:
        return ""
    rows = []
    for entry in artifact.history:
        content = entry.get("content") or {}
        if not isinstance(content, Mapping):
            continue
        if "verdict" in content:
            rows.append(
                f"<tr><td>v{entry['version']}</td><td>{content.get('iteration', '-')}</td>"
                f"<td>{content.get('error', 0)}</td><td>{content.get('warning', 0)}</td>"
                f"<td>{content.get('info', 0)}</td><td>{content.get('score', '-')}</td>"
                f"<td><code>{html.escape(str(content.get('verdict')))}</code></td></tr>"
            )
        elif "verdict" not in content and "blocking" in content:
            rows.append(
                f"<tr><td>v{entry['version']}</td><td>{content.get('revision', '-')}</td>"
                f"<td colspan='4'>{html.escape(str(content.get('verdict')))}"
                f"（{content.get('blocking', 0)} 条待改）</td>"
                f"<td><code>{html.escape(str(content.get('verdict')))}</code></td></tr>"
            )
    if not rows:
        return ""
    return (
        "<h3>评审轮次演进（作品版本）</h3><table><thead><tr><th>版本</th><th>轮次</th>"
        "<th>阻塞</th><th>警告</th><th>提示</th><th>评分</th><th>结论</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table>"
    )


def _render_patches(state: SharedState) -> str:
    """把每次修复的明细汇总成表。"""
    artifact = state.artifact("patch")
    if artifact is None:
        return ""
    rows: list[str] = []
    for entry in artifact.history:
        content = entry.get("content") or {}
        if not isinstance(content, Mapping):
            continue
        for item in content.get("applied") or []:
            rows.append(
                f"<tr><td>v{entry['version']}</td><td>{html.escape(str(content.get('role', '')))}</td>"
                f"<td><code>{html.escape(str(item.get('rule_id')))}</code></td>"
                f"<td>{item.get('line', 0)}</td>"
                f"<td class='sub'>{html.escape(str(item.get('message', '')))}</td></tr>"
            )
    if not rows:
        return ""
    return (
        "<h3>自动修复明细</h3><table><thead><tr><th>版本</th><th>修复角色</th><th>规则</th>"
        "<th>行</th><th>说明</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _render_artifacts(state: SharedState) -> str:
    if not state.artifacts:
        return '<div class="sub">没有作品</div>'
    blocks: list[str] = []
    names = list(state.artifacts)
    tabs = "".join(
        f'<button class="tab {"on" if index == 0 else ""}" data-group="art" data-tab="{html.escape(name)}" '
        f'onclick="showTab(&quot;art&quot;,&quot;{html.escape(name)}&quot;)">{html.escape(name)}'
        f' <span class="sub">v{state.artifacts[name].version}</span></button>'
        for index, name in enumerate(names)
    )
    for index, name in enumerate(names):
        artifact = state.artifacts[name]
        content = artifact.content
        if name == "code" and isinstance(content, str) and len(artifact.history) > 1:
            inner = _render_code_evolution(artifact, state)
        elif isinstance(content, str):
            inner = f"<pre>{html.escape(_clip(content, 6000))}</pre>"
        else:
            inner = f"<pre>{html.escape(json.dumps(content, ensure_ascii=False, indent=2, default=str))}</pre>"
        blocks.append(f'<div class="panel {"on" if index == 0 else ""}" data-panel="art" data-panel-id="{html.escape(name)}">{inner}</div>')
    return f'<div class="tabs">{tabs}</div>{"".join(blocks)}'


def _render_code_evolution(artifact, state: SharedState) -> str:
    history = artifact.history
    first, last = str(history[0]["content"]), str(history[-1]["content"])
    diff = list(
        difflib.unified_diff(
            first.splitlines(), last.splitlines(),
            fromfile="v1 编码 Agent", tofile=f"v{history[-1]['version']} 修复 Agent", lineterm="",
        )
    )
    diff_html = "".join(
        f'<span class="{"add" if line.startswith("+") and not line.startswith("+++") else ("del" if line.startswith("-") and not line.startswith("---") else ("hunk" if line.startswith("@@") else ""))}">'
        f"{html.escape(line)}\n</span>"
        for line in diff
    ) or "(无差异)"
    versions = "".join(
        f"<details><summary>v{entry['version']} · {html.escape(str(entry.get('author') or ''))} · "
        f"{html.escape(str(entry.get('note') or ''))}</summary><pre>{html.escape(_clip(str(entry.get('content')), 6000))}</pre></details>"
        for entry in reversed(history)
    )
    return (
        f'<h3>首版 → 终版 diff（v{history[0]["version"]} → v{history[-1]["version"]}）</h3>'
        f'<pre class="diff">{diff_html}</pre>'
        f'<h3>终版代码</h3><pre>{html.escape(_clip(last, 8000))}</pre>'
        f'<h3>版本历史</h3>{versions}'
    )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _clip(text: Any, limit: int) -> str:
    value = str(text)
    return value if len(value) <= limit else value[:limit] + "\n… (已截断)"


def _fit(text: Any, max_px: float, font_size: float) -> str:
    """按估算宽度裁剪文字（中文按 1em、西文按 0.56em），避免 SVG 文本溢出节点。"""
    value = str(text or "")
    width = 0.0
    out: list[str] = []
    for char in value:
        char_width = font_size if ord(char) > 0x2E7F else font_size * 0.56
        if width + char_width > max_px:
            return "".join(out) + "…"
        out.append(char)
        width += char_width
    return "".join(out)


def _kv_preview(data: Mapping[str, Any], limit: int = 220) -> str:
    if not data:
        return "-"
    text = json.dumps(data, ensure_ascii=False, default=str)
    return html.escape(_clip(text, limit))


def _fmt_time(ts: Any) -> str:
    import time

    if not ts:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "-"


def write_report(path, **kwargs) -> str:
    """生成并写盘，返回路径。"""
    from pathlib import Path

    html_text = build_report(**kwargs)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html_text, encoding="utf-8")
    return str(target)
