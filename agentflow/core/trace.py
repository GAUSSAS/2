"""事件追踪：结构化执行日志 + 终端输出。

每一次运行都会产生一条 **事件流**（写入 ``events.jsonl``），包含：
运行开始/结束、节点开始/结束（含耗时、重试次数）、边的判定结果、消息、
检查点、错误。它同时服务于三个消费者：

* 终端实时输出（``echo=True``）
* 运行报告 ``report.html`` 的时间线
* Web UI 的轮询接口（``GET /api/runs/<id>``）
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

RESET = "\033[0m"
COLORS = {
    "gray": "\033[90m",
    "blue": "\033[94m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "red": "\033[91m",
    "cyan": "\033[96m",
    "magenta": "\033[95m",
    "bold": "\033[1m",
}


def _supports_color() -> bool:
    if os_environ_no_color():
        return False
    import sys

    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def os_environ_no_color() -> bool:
    import os

    return bool(os.environ.get("NO_COLOR"))


def paint(text: str, color: str, *, enabled: bool = True) -> str:
    if not enabled or color not in COLORS:
        return text
    return f"{COLORS[color]}{text}{RESET}"


@dataclass
class Tracer:
    """收集并输出运行事件。"""

    run_id: str = ""
    echo: bool = True
    color: bool = field(default_factory=_supports_color)
    #: 事件回调（Web UI 用它做实时推送）
    on_event: Callable[[dict[str, Any]], None] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    _lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)

    # ---- 写入 ------------------------------------------------------
    def emit(self, event_type: str, **data: Any) -> dict[str, Any]:
        with self._lock:
            event = {
                "seq": len(self.events) + 1,
                "ts": time.time(),
                "type": event_type,
                "run_id": self.run_id,
                **data,
            }
            self.events.append(event)
            if self.on_event is not None:
                try:
                    self.on_event(event)
                except Exception:  # noqa: BLE001 - 回调不能影响主流程
                    pass
            if self.echo:
                self._print(event)
            return event

    def _print(self, event: dict[str, Any]) -> None:
        etype = event["type"]
        if etype == "run_start":
            print(paint(f"\n▶ 运行 {event.get('run_id')} · {event.get('workflow_name')}", "bold", enabled=self.color))
            print(paint(f"  {event.get('node_count')} 个节点 / {event.get('edge_count')} 条边", "gray", enabled=self.color))
        elif etype == "wave_start":
            nodes = ", ".join(event.get("nodes", []))
            parallel = "  ⇉ 并行" if len(event.get("nodes", [])) > 1 else ""
            print(paint(f"\n[波次 {event.get('wave')}]{parallel} {nodes}", "cyan", enabled=self.color))
        elif etype == "node_start":
            print(f"  ├─ {paint(event.get('node_id', ''), 'blue', enabled=self.color)} "
                  f"({event.get('agent')}) 开始…")
        elif etype == "node_end":
            status = event.get("status")
            color = {"ok": "green", "failed": "red", "skipped": "yellow"}.get(status, "gray")
            extra = event.get("detail") or ""
            print(f"  │  {paint(status.upper().ljust(7), color, enabled=self.color)} "
                  f"{event.get('node_id')} ({event.get('duration_ms')}ms) {extra}")
            for line in event.get("logs", [])[:8]:
                print(paint(f"  │    {line}", "gray", enabled=self.color))
        elif etype == "retry":
            print(paint(f"  │  ↻ 重试 {event.get('node_id')} 第 {event.get('attempt')} 次：{event.get('error')}", "yellow", enabled=self.color))
        elif etype == "edge":
            if event.get("fired"):
                label = f" ({event['label']})" if event.get("label") else ""
                print(paint(f"  ├→ {event.get('source')} → {event.get('target')}{label}", "gray", enabled=self.color))
        elif etype == "message":
            print(paint(f"  ✉  {event.get('summary')}", "magenta", enabled=self.color))
        elif etype == "warning":
            print(paint(f"  ⚠  {event.get('message')}", "yellow", enabled=self.color))
        elif etype == "error":
            print(paint(f"  ✖  {event.get('message')}", "red", enabled=self.color))
        elif etype == "run_end":
            status = event.get("status")
            color = "green" if status == "passed" else ("yellow" if status == "partial" else "red")
            print(paint(f"\n■ 结束：{status.upper()}  用时 {event.get('duration_s')}s", color + ("\033[1m" if color else ""), enabled=self.color))
            if event.get("error"):
                print(paint(f"  {event.get('error')}", "red", enabled=self.color))

    # ---- 输出 ------------------------------------------------------
    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(e, ensure_ascii=False) for e in self.events)

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_jsonl() + ("\n" if self.events else ""), encoding="utf-8")
        return path

    def by_type(self, event_type: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] == event_type]

    def stats(self) -> dict[str, Any]:
        nodes = [e for e in self.events if e["type"] == "node_end"]
        return {
            "waves": max((e.get("wave", 0) for e in self.by_type("wave_start")), default=0),
            "node_runs": len(nodes),
            "retries": len(self.by_type("retry")),
            "messages": len(self.by_type("message")),
            "edges_fired": len([e for e in self.by_type("edge") if e.get("fired")]),
            "errors": len(self.by_type("error")),
        }
