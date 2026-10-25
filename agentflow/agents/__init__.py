"""Agent 包：内置的代码审查与内容创作两套 Agent。

导入本包即完成注册（``@register_agent`` 在导入时执行）。
外部团队可以用 ``@register_agent`` 或 ``AgentRegistry.add()`` 追加自己的 Agent，
也可以通过环境变量 ``AGENTFLOW_AGENTS=my_pkg.agents`` 让引擎自动加载。
"""

from __future__ import annotations

from .code_agents import (
    AggregateAgent,
    CoderAgent,
    EscalatorAgent,
    FixerAgent,
    ReporterAgent,
    ReviewerAgent,
    RuleCatalogAgent,
)

__all__ = [
    "CoderAgent",
    "ReviewerAgent",
    "AggregateAgent",
    "FixerAgent",
    "ReporterAgent",
    "EscalatorAgent",
    "RuleCatalogAgent",
]
