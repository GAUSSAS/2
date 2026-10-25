"""AgentFlow —— 一个最小可用、可扩展的多 Agent 工作流编排引擎。

核心概念（与课程要求一一对应）::

    工作流 (Workflow)  ── 由 NodeSpec/EdgeSpec 组成的声明式图，见 agentflow/workflows/*.json
    智能体 (Agent)     ── 无状态执行单元，通过 AgentContext 读写共享黑板，见 core/agent.py
    通信 (MessageBus)  ── Agent 之间显式的消息传递，见 core/message.py
    状态 (SharedState) ── 全局运行状态 + 带版本的作品仓库，见 core/state.py
    调度 (Engine)      ── 顺序 / 条件分支 / 循环 / 并行 / 重试 / 断点恢复，见 core/engine.py

典型用法::

    from agentflow import load_workflow, Engine, default_registry

    engine = Engine(default_registry, run_root="runs")
    result = engine.run(load_workflow("code_review"), {"task": "..."})
    print(result.status, result.run_dir)
"""

from __future__ import annotations

__version__ = "1.0.0"

from .core.agent import AgentContext, AgentRegistry, AgentResult, BaseAgent, default_registry, register_agent
from .core.engine import Engine, RunResult
from .core.errors import (
    AgentExecutionError,
    AgentFlowError,
    ExpressionError,
    FlowControlError,
    GraphValidationError,
    OutputValidationError,
)
from .core.graph import EdgeSpec, NodeSpec, WorkflowSpec, load_workflow, list_workflows
from .core.message import Message, MessageBus
from .core.state import Artifact, NodeRun, SharedState

# 注册内置 Agent（import 即注册，工作流 JSON 通过 agent 名称引用）
from .agents import code_agents as _code_agents  # noqa: F401,E402
from .agents import content_agents as _content_agents  # noqa: F401,E402

__all__ = [
    "__version__",
    # 引擎
    "Engine",
    "RunResult",
    "WorkflowSpec",
    "NodeSpec",
    "EdgeSpec",
    "load_workflow",
    "list_workflows",
    # Agent
    "BaseAgent",
    "AgentResult",
    "AgentContext",
    "AgentRegistry",
    "default_registry",
    "register_agent",
    # 通信与状态
    "Message",
    "MessageBus",
    "SharedState",
    "Artifact",
    "NodeRun",
    # 异常
    "AgentFlowError",
    "GraphValidationError",
    "AgentExecutionError",
    "OutputValidationError",
    "FlowControlError",
    "ExpressionError",
]
