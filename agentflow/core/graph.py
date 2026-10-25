"""工作流定义（声明式图）与加载/校验。

一个工作流就是一个 **有向图**：

* ``nodes``：节点（id + 使用哪个 Agent + 输入映射 + 重试/超时/错误策略）
* ``edges``：边（``when`` 条件决定是否走这条边 → 条件分支；回边 → 循环；
  一个节点同时触发多条边 → 并行）

关键在于 **流程由数据描述，而不是由 Python 代码写死**：
新增 Agent 或调整流程只需要改 JSON（或将来由可视化编辑器生成 JSON），
引擎与 Agent 代码都不用动。这也是本项目“可扩展性”的主要答案。

输入引用语法（节点 ``inputs`` 的值）::

    "inputs.task"                   运行入参
    "vars.verdict"                  变量黑板
    "artifacts.code"                作品内容（字符串或 dict）
    "nodes.aggregate.verdict"       某个节点最近一次的输出
    "counters.iteration"            计数器
    "literal:error"                 字面量（避免歧义时使用）
    "code"                          裸名：依次在 inputs/vars/counters/作品/输出中查找

``params`` 则是**字面量配置**（不做引用解析），与 ``inputs`` 合并后交给 Agent。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import AgentFlowError, ExpressionError, GraphValidationError
from .expr import compile_expression
from .state import SharedState

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / "workflows"


class InputResolutionError(AgentFlowError):
    """节点输入引用无法解析（例如引用了不存在的作品）。"""


# ---------------------------------------------------------------------------
# 输入引用解析
# ---------------------------------------------------------------------------
_NAMESPACES = ("inputs", "vars", "state", "artifacts", "arts", "nodes", "counters")


def _namespace(name: str, state: SharedState) -> dict[str, Any] | None:
    if name == "inputs":
        return state.inputs
    if name in ("vars", "state"):
        return state.variables
    if name in ("artifacts", "arts"):
        return {key: art.content for key, art in state.artifacts.items()}
    if name == "nodes":
        return state.last_outputs()
    if name == "counters":
        return dict(state.counters)
    return None


def resolve_ref(ref: Any, state: SharedState, *, node_id: str = "", key: str = "") -> Any:
    """把 ``"artifacts.code"`` 之类的引用解析成真实值。"""
    if not isinstance(ref, str):
        return ref
    if ref.startswith("literal:"):
        return ref[len("literal:") :]

    parts = ref.split(".")
    base = _namespace(parts[0], state)
    rest = parts[1:]
    if base is None:
        # 裸名：按约定顺序回退。注意此处 parts[0] 本身就是键，直接取值。
        for candidate in (
            state.inputs,
            state.variables,
            state.counters,
            {k: a.content for k, a in state.artifacts.items()},
            state.last_outputs(),
        ):
            if parts[0] in candidate:
                value: Any = candidate[parts[0]]
                walked = parts[0]
                break
        else:
            raise InputResolutionError(
                f"节点 {node_id!r} 的输入 {key!r} 无法解析：引用 {ref!r} 不存在"
            )
    else:
        value = base
        walked = parts[0]

    for part in rest:
        if isinstance(value, Mapping) and part in value:
            value = value[part]
            walked += f".{part}"
        elif isinstance(value, (list, tuple)) and part.lstrip("-").isdigit():
            try:
                value = value[int(part)]
            except IndexError as exc:
                raise InputResolutionError(
                    f"节点 {node_id!r} 的输入 {key!r}：{walked}.{part} 下标越界"
                ) from exc
            walked += f".{part}"
        else:
            available = ", ".join(sorted(value)) if isinstance(value, Mapping) else type(value).__name__
            raise InputResolutionError(
                f"节点 {node_id!r} 的输入 {key!r} 无法解析：{walked!r} 没有 {part!r}（可用: {available}）"
            )
    return value


def resolve_mapping(mapping: Mapping[str, Any] | None, state: SharedState, *, node_id: str = "") -> dict[str, Any]:
    """解析节点的 inputs 映射。

    支持两种写法：

    * 直接写引用：``"code": "artifacts.code"``
    * 带默认值的对象：``"notes": {"ref": "nodes.edit.notes", "default": []}``
      —— 用于“首次进入循环时上游还没产出”的可选输入。
    """
    resolved: dict[str, Any] = {}
    for key, ref in (mapping or {}).items():
        if isinstance(ref, Mapping) and "ref" in ref:
            try:
                resolved[key] = resolve_ref(ref["ref"], state, node_id=node_id, key=key)
            except InputResolutionError:
                if "default" in ref:
                    resolved[key] = ref["default"]
                elif ref.get("optional"):
                    resolved[key] = None
                else:
                    raise
        else:
            resolved[key] = resolve_ref(ref, state, node_id=node_id, key=key)
    return resolved


# ---------------------------------------------------------------------------
# 规格对象
# ---------------------------------------------------------------------------
@dataclass
class RetryPolicy:
    """节点级重试策略：只对 ``AgentExecutionError`` 生效。"""

    max_attempts: int = 1
    backoff_ms: int = 0
    multiplier: float = 2.0
    max_backoff_ms: int = 5000
    retry_on: tuple[str, ...] = ("*",)

    @property
    def enabled(self) -> bool:
        return self.max_attempts > 1

    def delay_ms(self, attempt: int) -> int:
        """第 ``attempt`` 次失败后等待多久再试。"""
        if self.backoff_ms <= 0:
            return 0
        raw = self.backoff_ms * (self.multiplier ** max(0, attempt - 1))
        return int(min(raw, self.max_backoff_ms))

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "backoff_ms": self.backoff_ms,
            "multiplier": self.multiplier,
            "max_backoff_ms": self.max_backoff_ms,
            "retry_on": list(self.retry_on),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "RetryPolicy":
        data = dict(data or {})
        return cls(
            max_attempts=int(data.get("max_attempts", 1)),
            backoff_ms=int(data.get("backoff_ms", 0)),
            multiplier=float(data.get("multiplier", 2.0)),
            max_backoff_ms=int(data.get("max_backoff_ms", 5000)),
            retry_on=tuple(data.get("retry_on", ("*",))),
        )


@dataclass
class NodeSpec:
    id: str
    agent: str
    name: str = ""
    description: str = ""
    #: 引用映射（运行时解析）
    inputs: dict[str, Any] = field(default_factory=dict)
    #: 字面量配置
    params: dict[str, Any] = field(default_factory=dict)
    #: 期望输出字段（用于契约校验与文档）
    outputs: tuple[str, ...] = ()
    #: 前置节点：这些节点必须在本节点这一“波”之前完成（join 语义）
    needs: tuple[str, ...] = ()
    max_visits: int | None = None
    timeout_s: float | None = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    on_error: str = "fail"  # fail | skip
    #: 节点分类，用于可视化分组/颜色（可选）
    kind: str = "task"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "name": self.name or self.id,
            "description": self.description,
            "inputs": self.inputs,
            "params": self.params,
            "outputs": list(self.outputs),
            "needs": list(self.needs),
            "max_visits": self.max_visits,
            "timeout_s": self.timeout_s,
            "retry": self.retry.to_dict(),
            "on_error": self.on_error,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], defaults: Mapping[str, Any] | None = None) -> "NodeSpec":
        defaults = defaults or {}
        node_id = str(data.get("id", "")).strip()
        if not node_id:
            raise GraphValidationError("节点缺少 id")
        return cls(
            id=node_id,
            agent=str(data.get("agent", "")).strip(),
            name=data.get("name", node_id),
            description=data.get("description", ""),
            inputs=dict(data.get("inputs", {})),
            params=dict(data.get("params", {})),
            outputs=tuple(data.get("outputs", ())),
            needs=tuple(data.get("needs", ())),
            max_visits=data.get("max_visits", defaults.get("max_visits")),
            timeout_s=data.get("timeout_s", defaults.get("timeout_s")),
            retry=RetryPolicy.from_dict(data.get("retry") or defaults.get("retry")),
            on_error=data.get("on_error", defaults.get("on_error", "fail")),
            kind=data.get("kind", "task"),
        )


@dataclass
class EdgeSpec:
    source: str
    target: str
    when: str | None = None
    label: str = ""
    #: success（默认）| error | skip —— 决定该边在什么节点状态下才可能触发
    on: str = "success"
    #: 独占边：一旦命中，就不再评估同一源节点的后续边（用于“优先级最高”的分支）
    exclusive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.source,
            "to": self.target,
            "when": self.when,
            "label": self.label,
            "on": self.on,
            "exclusive": self.exclusive,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EdgeSpec":
        source = str(data.get("from", data.get("source", ""))).strip()
        target = str(data.get("to", data.get("target", ""))).strip()
        if not source or not target:
            raise GraphValidationError(f"边缺少 from/to: {dict(data)}")
        on = str(data.get("on", "success"))
        if on not in ("success", "error", "skip"):
            raise GraphValidationError(f"边 {source}->{target} 的 on={on!r} 非法（success/error/skip）")
        return cls(
            source=source,
            target=target,
            when=data.get("when"),
            label=data.get("label", ""),
            on=on,
            exclusive=bool(data.get("exclusive", False)),
        )

    def is_unconditional(self) -> bool:
        return not self.when


@dataclass
class WorkflowSpec:
    id: str
    name: str = ""
    description: str = ""
    version: str = "1.0"
    start: str = ""
    nodes: list[NodeSpec] = field(default_factory=list)
    edges: list[EdgeSpec] = field(default_factory=list)
    #: 运行入参的声明（默认值 + 说明），用于 CLI 与文档
    inputs_schema: dict[str, dict[str, Any]] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)

    # ---- 查询 ------------------------------------------------------
    def node(self, node_id: str) -> NodeSpec:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(f"工作流 {self.id!r} 中没有节点 {node_id!r}")

    def outgoing(self, node_id: str, on: str = "success") -> list[EdgeSpec]:
        return [e for e in self.edges if e.source == node_id and e.on == on]

    def incoming(self, node_id: str) -> list[EdgeSpec]:
        return [e for e in self.edges if e.target == node_id]

    @property
    def node_ids(self) -> list[str]:
        return [n.id for n in self.nodes]

    # ---- 序列化 ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name or self.id,
            "description": self.description,
            "version": self.version,
            "start": self.start,
            "inputs": self.inputs_schema,
            "defaults": self.defaults,
            "settings": self.settings,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkflowSpec":
        if not isinstance(data, Mapping):
            raise GraphValidationError("工作流定义必须是 JSON 对象")
        workflow_id = str(data.get("id", "")).strip()
        if not workflow_id:
            raise GraphValidationError("工作流缺少 id")
        defaults = dict(data.get("defaults", {}))
        nodes = [NodeSpec.from_dict(n, defaults) for n in data.get("nodes", [])]
        edges = [EdgeSpec.from_dict(e) for e in data.get("edges", [])]
        start = str(data.get("start", "")).strip() or (nodes[0].id if nodes else "")
        spec = cls(
            id=workflow_id,
            name=data.get("name", workflow_id),
            description=data.get("description", ""),
            version=str(data.get("version", "1.0")),
            start=start,
            nodes=nodes,
            edges=edges,
            inputs_schema=dict(data.get("inputs", {})),
            settings=dict(data.get("settings", {})),
            defaults=defaults,
        )
        return spec

    # ---- 校验 ------------------------------------------------------
    def validate(self, registry=None, *, strict_agents: bool = True) -> "WorkflowSpec":
        """静态校验：结构、引用、表达式、Agent 是否存在、图是否可达。"""
        problems: list[str] = []
        if not self.nodes:
            problems.append("工作流没有任何节点")
        ids = [n.id for n in self.nodes]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            problems.append(f"节点 id 重复: {sorted(duplicates)}")
        id_set = set(ids)

        if self.start not in id_set:
            problems.append(f"start 指向不存在的节点: {self.start!r}")

        for node in self.nodes:
            if not node.agent:
                problems.append(f"节点 {node.id!r} 未指定 agent")
            elif strict_agents and registry is not None and not registry.has(node.agent):
                problems.append(
                    f"节点 {node.id!r} 引用了未注册的 Agent {node.agent!r}"
                    f"（已注册: {', '.join(registry.names())}）"
                )
            if node.on_error not in ("fail", "skip"):
                problems.append(f"节点 {node.id!r} 的 on_error={node.on_error!r} 非法（fail/skip）")
            for need in node.needs:
                if need not in id_set:
                    problems.append(f"节点 {node.id!r} 的 needs 引用了不存在的节点 {need!r}")
            if node.max_visits is not None and node.max_visits < 1:
                problems.append(f"节点 {node.id!r} 的 max_visits 必须 >= 1")
            for key, ref in node.inputs.items():
                if not isinstance(ref, str):
                    continue
                head = ref.split(".")[0]
                if "." in ref and head in _NAMESPACES:
                    continue  # 运行期解析
                if "." in ref:
                    problems.append(
                        f"节点 {node.id!r} 的输入 {key!r} 使用了未知命名空间 {head!r}"
                        f"（可用: {', '.join(_NAMESPACES)}）"
                    )

        for edge in self.edges:
            if edge.source not in id_set:
                problems.append(f"边 {edge.source}->{edge.target} 的起点不存在")
            if edge.target not in id_set:
                problems.append(f"边 {edge.source}->{edge.target} 的终点不存在")
            if edge.when:
                try:
                    compile_expression(edge.when)
                except ExpressionError as exc:
                    problems.append(f"边 {edge.source}->{edge.target} 的条件非法: {exc}")

        if problems:
            raise GraphValidationError(
                f"工作流 {self.id!r} 校验失败（{len(problems)} 个问题）:\n  - "
                + "\n  - ".join(problems)
            )

        # 可达性（只警告，因为子图可能由 skip 边进入）
        reachable = self.reachable()
        unreachable = [i for i in ids if i not in reachable]
        if unreachable:
            self.settings.setdefault("_warnings", []).append(f"不可达节点: {unreachable}")
        return self

    def reachable(self, start: str | None = None) -> set[str]:
        start = start or self.start
        seen: set[str] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            for edge in self.edges:
                if edge.source == current and edge.target not in seen:
                    stack.append(edge.target)
        return seen

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------
def load_workflow(source: str | Path | Mapping[str, Any], *, validate: bool = True,
                  registry=None) -> WorkflowSpec:
    """从工作流 id、JSON 文件路径或 dict 加载工作流。"""
    if isinstance(source, Mapping):
        spec = WorkflowSpec.from_dict(source)
    else:
        path = Path(source)
        if not path.exists():
            candidate = WORKFLOW_DIR / f"{source}.json"
            if not candidate.exists():
                available = ", ".join(list_workflows()) or "(空)"
                raise GraphValidationError(
                    f"找不到工作流 {source!r}。可用: {available}"
                )
            path = candidate
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise GraphValidationError(f"工作流文件 {path} 不是合法 JSON: {exc}") from exc
        spec = WorkflowSpec.from_dict(data)
    if validate:
        spec.validate(registry)
    return spec


def list_workflows() -> list[dict[str, Any]]:
    """列出内置工作流（扫描 workflows/*.json）。"""
    rows: list[dict[str, Any]] = []
    if not WORKFLOW_DIR.exists():
        return rows
    for path in sorted(WORKFLOW_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            continue
        nodes = data.get("nodes", [])
        rows.append(
            {
                "id": data.get("id", path.stem),
                "name": data.get("name", path.stem),
                "description": data.get("description", ""),
                "version": data.get("version", ""),
                "node_count": len(nodes),
                "edge_count": len(data.get("edges", [])),
                "agents": sorted({n.get("agent", "") for n in nodes if n.get("agent")}),
                "path": str(path),
            }
        )
    return rows


def save_workflow(spec: WorkflowSpec, path: str | Path) -> Path:
    """把工作流写回 JSON（可视化编辑器保存时使用）。"""
    target = Path(path)
    if target.is_dir() or not target.suffix:
        target = target / f"{spec.id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(spec.to_json(), encoding="utf-8")
    return target


def diff_workflow(a: WorkflowSpec, b: WorkflowSpec) -> dict[str, list[str]]:
    """粗粒度比较两个工作流（用于报告“流程演进”一栏）。"""
    a_nodes, b_nodes = set(a.node_ids), set(b.node_ids)
    a_edges = {(e.source, e.target) for e in a.edges}
    b_edges = {(e.source, e.target) for e in b.edges}
    return {
        "added_nodes": sorted(b_nodes - a_nodes),
        "removed_nodes": sorted(a_nodes - b_nodes),
        "added_edges": sorted(f"{s}->{t}" for s, t in b_edges - a_edges),
        "removed_edges": sorted(f"{s}->{t}" for s, t in a_edges - b_edges),
    }
