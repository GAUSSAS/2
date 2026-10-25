"""Agent 抽象、注册表与执行上下文。

Agent 的职责边界刻意划得很窄：
**输入一份只读输入 dict，输出一份结果 dict，不碰调度、不碰文件、不碰时间。**
这样才能被独立测试、被并行执行、被替换（甚至换成真实 LLM 调用）而不影响流程定义。

所有与引擎的交互都通过 ``AgentContext``：

    ctx.artifact_text("code")      读取共享作品
    ctx.put_artifact(...)          写入作品（产生新版本）
    ctx.set(...) / ctx.get(...)    读写标量黑板
    ctx.send(...)                  给其他 Agent 发消息（协作轨迹）
    ctx.log(...)                   记录日志（进入运行报告）

新增 Agent 只需三步::

    @register_agent
    class MyAgent(BaseAgent):
        name = "my_agent"
        inputs = ("code",)        # 声明契约（可读性 + 校验）
        outputs = ("score",)
        def run(self, ctx, inputs): return AgentResult(outputs={"score": 1})

然后在工作流 JSON 里写 ``"agent": "my_agent"`` 即可，引擎无需改动。
"""

from __future__ import annotations

import abc
import importlib
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Iterable, Mapping

from .errors import AgentExecutionError, OutputValidationError
from .state import SharedState


@dataclass
class AgentResult:
    """Agent 的返回值。"""

    #: 结构化输出：会写入 ``nodes.<node_id>.*`` 供后续节点与条件表达式使用
    outputs: dict[str, Any] = field(default_factory=dict)
    #: 本 Agent 写出/更新的作品名，引擎会记录来源
    artifacts: list[str] = field(default_factory=list)
    #: 便捷消息：[(接收者, 消息类型, payload), ...]
    messages: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    #: 附加元信息（耗时、token 数、模型名等），写入运行报告
    meta: dict[str, Any] = field(default_factory=dict)
    #: 人类可读的日志行
    logs: list[str] = field(default_factory=list)


class AgentContext:
    """Agent 与引擎之间的唯一通道（门面模式）。"""

    def __init__(
        self,
        *,
        run_id: str,
        workflow_id: str,
        node_id: str,
        agent_name: str,
        state: SharedState,
        inputs: Mapping[str, Any],
        attempt: int = 1,
        visit: int = 1,
        settings: Mapping[str, Any] | None = None,
    ) -> None:
        self.run_id = run_id
        self.workflow_id = workflow_id
        self.node_id = node_id
        self.agent_name = agent_name
        self.state = state
        self.inputs: dict[str, Any] = dict(inputs)
        self.attempt = attempt
        self.visit = visit
        self.settings: dict[str, Any] = dict(settings or {})
        self.logs: list[str] = []

    # ---- 日志 ------------------------------------------------------
    def log(self, message: str) -> None:
        self.logs.append(str(message))

    # ---- 共享状态 --------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self.state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.state.set(key, value)

    def incr(self, key: str, amount: int = 1) -> int:
        return self.state.incr(key, amount)

    def counter(self, key: str) -> int:
        return self.state.counter(key)

    # ---- 作品仓库 --------------------------------------------------
    def get_artifact(self, name: str):
        return self.state.artifact(name)

    def get_content(self, name: str, default: Any = None) -> Any:
        return self.state.get_content(name, default)

    def artifact_text(self, name: str, default: str = "") -> str:
        content = self.state.get_content(name)
        if content is None:
            return default
        return content if isinstance(content, str) else str(content)

    def put_artifact(self, name: str, content: Any, *, note: str = "") -> Any:
        return self.state.put(name, content, author=self.agent_name, note=note)

    # ---- 消息 ------------------------------------------------------
    def send(self, recipient: str, kind: str, payload: dict[str, Any] | None = None, *,
             tags: Iterable[str] = ()):
        """给其他 Agent（或 ``"*"``）发消息。

        发送者记录为**节点 id**：同一个 Agent 类可能在工作流里出现多次
        （如两路并行审查），用节点 id 才能区分是谁在说话。
        """
        return self.state.send(self.node_id, recipient, kind, payload, tags=tuple(tags))

    def inbox(self) -> list:
        """发给当前 Agent/节点的消息（只读）。"""
        return [m for m in self.state.bus.all() if m.recipient in (self.node_id, self.agent_name, "*")]


class BaseAgent(abc.ABC):
    """Agent 基类。"""

    #: 在工作流 JSON 里引用这个名字
    name: ClassVar[str] = ""
    #: 人类可读描述（`agentflow list` 会打印）
    description: ClassVar[str] = ""
    #: 声明的输入/输出契约：用于自检、文档生成与输出校验
    inputs: ClassVar[tuple[str, ...]] = ()
    outputs: ClassVar[tuple[str, ...]] = ()
    version: ClassVar[str] = "1.0"

    def __init__(self, **config: Any) -> None:
        self.config: dict[str, Any] = config

    @abc.abstractmethod
    def run(self, ctx: AgentContext, inputs: Mapping[str, Any]) -> AgentResult:
        """执行主体。实现者可以抛 ``AgentExecutionError`` 触发重试。"""

    def __call__(self, ctx: AgentContext, inputs: Mapping[str, Any]) -> AgentResult:
        result = self.run(ctx, inputs)
        if not isinstance(result, AgentResult):
            raise AgentExecutionError(
                f"Agent {self.name!r} 必须返回 AgentResult，实际返回 {type(result).__name__}"
            )
        self.validate(result)
        return result

    def validate(self, result: AgentResult) -> None:
        """校验声明的 outputs 是否齐全，缺失即视为可重试的契约违约。"""
        missing = [key for key in self.outputs if key not in result.outputs]
        if missing:
            raise OutputValidationError(
                f"Agent {self.name!r} 输出缺少必需字段: {', '.join(missing)}"
            )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name} v{self.version}>"


class AgentRegistry:
    """Agent 注册表：名称 -> 实例工厂。"""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], BaseAgent]] = {}
        self._classes: dict[str, type[BaseAgent]] = {}
        self._lock = threading.RLock()

    def register(self, agent_cls: type[BaseAgent]) -> type[BaseAgent]:
        if not agent_cls.name:
            raise ValueError(f"{agent_cls.__name__} 缺少 name 类属性")
        with self._lock:
            if agent_cls.name in self._classes and self._classes[agent_cls.name] is not agent_cls:
                # 允许覆盖（插件热替换），但记录下来便于排查
                self._classes[agent_cls.name] = agent_cls
            self._classes[agent_cls.name] = agent_cls
            self._factories[agent_cls.name] = agent_cls
        return agent_cls

    def add(self, agent: BaseAgent) -> BaseAgent:
        """注册一个已实例化的 Agent（便于注入 mock / 配置化实例）。"""
        with self._lock:
            self._factories[agent.name] = lambda: agent  # type: ignore[assignment]
        return agent

    def get(self, name: str) -> BaseAgent:
        with self._lock:
            factory = self._factories.get(name)
        if factory is None:
            available = ", ".join(sorted(self._factories)) or "(空)"
            raise AgentExecutionError(
                f"未注册的 Agent: {name!r}。已注册: {available}"
            )
        return factory()

    def resolve(self, name: str) -> BaseAgent:
        return self.get(name)

    def has(self, name: str) -> bool:
        return name in self._factories

    def names(self) -> list[str]:
        return sorted(self._factories)

    def describe(self) -> list[dict[str, Any]]:
        with self._lock:
            classes = dict(self._classes)
        rows = []
        for name in sorted(self._factories):
            cls = classes.get(name)
            rows.append(
                {
                    "name": name,
                    "description": cls.description if cls else "",
                    "inputs": list(cls.inputs) if cls else [],
                    "outputs": list(cls.outputs) if cls else [],
                    "version": cls.version if cls else "",
                }
            )
        return rows


#: 进程级默认注册表。插件也可以通过 ``AGENTFLOW_AGENTS`` 环境变量追加模块。
default_registry = AgentRegistry()


def register_agent(agent_cls: type[BaseAgent]) -> type[BaseAgent]:
    """类装饰器：把 Agent 注册进默认注册表。"""
    return default_registry.register(agent_cls)


def load_plugin_agents(module_paths: Iterable[str] | None = None) -> list[str]:
    """从模块路径导入插件（模块内使用 ``@register_agent`` 即可生效）。

    默认读取环境变量 ``AGENTFLOW_AGENTS``（逗号分隔），例如::

        set AGENTFLOW_AGENTS=my_team.agents
    """
    if module_paths is None:
        raw = os.environ.get("AGENTFLOW_AGENTS", "")
        module_paths = [p.strip() for p in raw.split(",") if p.strip()]
    loaded: list[str] = []
    for path in module_paths:
        importlib.import_module(path)
        loaded.append(path)
    return loaded
