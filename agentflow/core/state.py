"""共享状态（黑板）+ 带版本的作品仓库。

Agent 之间的数据流有两条通道：

* **作品仓库 (Artifact Store)**：代码、审查结论、文档这类“大对象”，
  每次写入都会产生新版本，因此天然支持 diff —— 修复 Agent 前后两版代码
  可以直接对比，报告里也能看到演进过程。
* **变量黑板 (variables)**：`verdict`、`iteration`、`attempts` 这类标量，
  供边上的 ``when`` 条件读取，实现条件分支与循环。

两者都放进 ``SharedState``，并以 ``threading.RLock`` 保护 —— 并行分支里的
Agent 可以同时读写。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .message import Message, MessageBus


def _now() -> float:
    return time.time()


@dataclass
class Artifact:
    """一个有版本历史的数据对象。"""

    name: str
    content: Any = ""
    version: int = 1
    author: str = ""
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    meta: dict[str, Any] = field(default_factory=dict)
    #: 每个版本一条：{"version", "content", "author", "ts", "note"}
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.history:
            self.history.append(
                {
                    "version": self.version,
                    "content": self.content,
                    "author": self.author,
                    "ts": self.updated_at,
                    "note": "initial",
                }
            )

    def update(self, content: Any, author: str = "", note: str = "") -> "Artifact":
        """写入新版本并返回自身（便于链式调用）。"""
        self.version += 1
        self.content = content
        self.author = author or self.author
        self.updated_at = _now()
        self.history.append(
            {
                "version": self.version,
                "content": content,
                "author": author,
                "ts": self.updated_at,
                "note": note,
            }
        )
        return self

    # ---- 便捷访问 --------------------------------------------------
    def text(self) -> str:
        return self.content if isinstance(self.content, str) else str(self.content)

    def previous(self) -> Any:
        """上一个版本的内容（没有则返回 None）。"""
        if len(self.history) < 2:
            return None
        return self.history[-2]["content"]

    def version_at(self, version: int) -> Any:
        for entry in self.history:
            if entry["version"] == version:
                return entry["content"]
        raise KeyError(f"作品 {self.name!r} 不存在版本 {version}")

    def to_dict(self, *, with_content: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "meta": self.meta,
            "versions": [
                {k: v for k, v in entry.items() if k != "content"} for entry in self.history
            ],
            "length": len(self.content) if isinstance(self.content, (str, list, dict)) else None,
        }
        if with_content:
            data["content"] = self.content
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Artifact":
        artifact = cls(
            name=data["name"],
            content=data.get("content", ""),
            version=data.get("version", 1),
            author=data.get("author", ""),
            created_at=data.get("created_at", _now()),
            updated_at=data.get("updated_at", _now()),
            meta=data.get("meta", {}),
        )
        # 恢复完整历史（快照里保存了每个版本的 content）
        history = data.get("history")
        if history:
            artifact.history = history
        return artifact


@dataclass
class NodeRun:
    """一次节点执行记录 —— 状态追踪的最小单元。"""

    node_id: str
    agent: str
    visit: int = 1
    attempt: int = 1
    status: str = "running"  # running | ok | failed | skipped
    wave: int = 0
    started_at: float = field(default_factory=_now)
    ended_at: float | None = None
    error: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        end = self.ended_at if self.ended_at is not None else _now()
        return round((end - self.started_at) * 1000, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "agent": self.agent,
            "visit": self.visit,
            "attempt": self.attempt,
            "status": self.status,
            "wave": self.wave,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "outputs": self.outputs,
            "logs": self.logs,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NodeRun":
        return cls(
            node_id=data.get("node_id", ""),
            agent=data.get("agent", ""),
            visit=int(data.get("visit", 1)),
            attempt=int(data.get("attempt", 1)),
            status=data.get("status", "running"),
            wave=int(data.get("wave", 0)),
            started_at=data.get("started_at", _now()),
            ended_at=data.get("ended_at"),
            error=data.get("error"),
            outputs=data.get("outputs", {}),
            logs=data.get("logs", []),
            meta=data.get("meta", {}),
        )


class SharedState:
    """整个运行期间唯一的可变状态对象。"""

    def __init__(
        self,
        run_id: str,
        workflow_id: str = "",
        workflow_name: str = "",
        inputs: dict[str, Any] | None = None,
    ) -> None:
        self.run_id = run_id
        self.workflow_id = workflow_id
        self.workflow_name = workflow_name
        self.inputs: dict[str, Any] = dict(inputs or {})
        self.artifacts: dict[str, Artifact] = {}
        self.variables: dict[str, Any] = {}
        self.counters: dict[str, int] = {}
        self.node_runs: list[NodeRun] = []
        self.bus = MessageBus(correlation_id=run_id)
        self.status: str = "running"
        self.error: str | None = None
        self.notes: list[str] = []
        self.created_at = _now()
        self.updated_at = self.created_at
        self._lock = threading.RLock()

    # ---- 变量黑板 --------------------------------------------------
    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.variables[key] = value
            self.updated_at = _now()

    def set_many(self, values: dict[str, Any]) -> None:
        with self._lock:
            self.variables.update(values)
            self.updated_at = _now()

    def get(self, key: str, default: Any = None) -> Any:
        return self.variables.get(key, default)

    def incr(self, key: str, amount: int = 1) -> int:
        with self._lock:
            self.counters[key] = self.counters.get(key, 0) + amount
            return self.counters[key]

    def counter(self, key: str) -> int:
        return self.counters.get(key, 0)

    def note(self, text: str) -> None:
        with self._lock:
            self.notes.append(text)

    # ---- 作品仓库 --------------------------------------------------
    def put(self, name: str, content: Any, *, author: str = "", note: str = "") -> Artifact:
        """写入作品：不存在则创建（v1），已存在则追加新版本。"""
        with self._lock:
            artifact = self.artifacts.get(name)
            if artifact is None:
                artifact = Artifact(name=name, content=content, author=author)
                self.artifacts[name] = artifact
            else:
                artifact.update(content, author=author, note=note)
            self.updated_at = _now()
            return artifact

    def artifact(self, name: str) -> Artifact | None:
        return self.artifacts.get(name)

    def get_content(self, name: str, default: Any = None) -> Any:
        artifact = self.artifacts.get(name)
        return default if artifact is None else artifact.content

    def has(self, name: str) -> bool:
        return name in self.artifacts

    # ---- 消息 ------------------------------------------------------
    def send(self, sender: str, recipient: str, kind: str, payload: dict[str, Any] | None = None,
             *, reply_to: str | None = None, tags: tuple[str, ...] = ()) -> Message:
        return self.bus.send(sender, recipient, kind, payload, reply_to=reply_to, tags=tags)

    @property
    def messages(self) -> list[Message]:
        return self.bus.all()

    # ---- 表达式上下文 ----------------------------------------------
    def expr_context(self) -> dict[str, Any]:
        """构造扁平化的求值上下文。

        约定（``when`` 里可以直接引用）::

            inputs.topic              运行入参
            vars.verdict              变量黑板
            verdict                   变量黑板的裸名（方便书写）
            arts.code.version         作品元信息
            code.version              同上（裸名）
            nodes.review_style.error_count   节点最近一次输出
            review_style.error_count  同上（裸名）
            iteration / step / run_id / workflow
        """
        ctx: dict[str, Any] = {}
        for key, value in self.inputs.items():
            ctx[f"inputs.{key}"] = value
        for key, value in self.variables.items():
            ctx[key] = value
            ctx[f"vars.{key}"] = value
        for key, value in self.counters.items():
            ctx.setdefault(f"counters.{key}", value)
        for name, artifact in self.artifacts.items():
            ctx[f"arts.{name}.version"] = artifact.version
            ctx[f"arts.{name}.name"] = artifact.name
            ctx[f"{name}.version"] = artifact.version
            ctx[f"{name}.author"] = artifact.author
            if isinstance(artifact.content, dict):
                for k, v in artifact.content.items():
                    ctx[f"{name}.{k}"] = v
        for node_id, outputs in self.last_outputs().items():
            for key, value in outputs.items():
                ctx[f"nodes.{node_id}.{key}"] = value
                # 裸名回退：让 when 里可以直接写 verdict 而不是 nodes.aggregate.verdict
                ctx.setdefault(key, value)
        # 每个节点最近一次执行的状态（含 skipped），供 skip 分支判定
        last_status: dict[str, str] = {}
        for run in self.node_runs:
            last_status[run.node_id] = run.status
        for node_id, status in last_status.items():
            ctx[f"nodes.{node_id}.status"] = status
        ctx["run_id"] = self.run_id
        ctx["workflow"] = self.workflow_id
        ctx.setdefault("iteration", self.counter("iteration"))
        ctx.setdefault("step", self.counter("step"))
        return ctx

    def last_outputs(self) -> dict[str, dict[str, Any]]:
        """每个节点最近一次成功执行的输出。"""
        result: dict[str, dict[str, Any]] = {}
        for run in self.node_runs:
            if run.status == "ok":
                result[run.node_id] = run.outputs
        return result

    # ---- 序列化 ----------------------------------------------------
    def to_dict(self, *, with_artifact_content: bool = True) -> dict[str, Any]:
        with self._lock:
            return {
                "run_id": self.run_id,
                "workflow_id": self.workflow_id,
                "workflow_name": self.workflow_name,
                "inputs": self.inputs,
                "artifacts": {
                    name: art.to_dict(with_content=with_artifact_content)
                    for name, art in self.artifacts.items()
                },
                "variables": self.variables,
                "counters": self.counters,
                "node_runs": [r.to_dict() for r in self.node_runs],
                "messages": self.bus.to_list(),
                "status": self.status,
                "error": self.error,
                "notes": self.notes,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SharedState":
        state = cls(
            run_id=data["run_id"],
            workflow_id=data.get("workflow_id", ""),
            workflow_name=data.get("workflow_name", ""),
            inputs=data.get("inputs", {}),
        )
        state.artifacts = {
            name: Artifact.from_dict(art) for name, art in data.get("artifacts", {}).items()
        }
        state.variables = data.get("variables", {})
        state.counters = data.get("counters", {})
        state.node_runs = [NodeRun.from_dict(r) for r in data.get("node_runs", [])]
        state.bus.restore(data.get("messages", []))
        state.status = data.get("status", "running")
        state.error = data.get("error")
        state.notes = data.get("notes", [])
        state.created_at = data.get("created_at", _now())
        state.updated_at = data.get("updated_at", _now())
        return state

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<SharedState run={self.run_id} artifacts={list(self.artifacts)} "
            f"messages={len(self.bus)} status={self.status}>"
        )
