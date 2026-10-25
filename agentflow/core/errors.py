"""AgentFlow 的异常体系。

异常分层的目的：让“可恢复错误”和“不可恢复错误”在引擎里有明确边界。
Agent 抛出的 *AgentExecutionError 子类* 会触发节点级重试策略；
其他异常一律视为不可恢复，直接让整个运行失败（仍会保存断点）。
"""

from __future__ import annotations


class AgentFlowError(Exception):
    """所有 AgentFlow 异常的基类。"""

    #: 是否属于“重试可能成功”的瞬时错误
    retryable: bool = False


class GraphValidationError(AgentFlowError):
    """工作流定义非法（节点/边引用不存在、表达式语法错误等）。"""


class ExpressionError(AgentFlowError):
    """条件表达式求值失败（变量缺失、使用了不允许的语法等）。"""


class AgentExecutionError(AgentFlowError):
    """Agent 执行失败。默认视为可重试，交由节点的 retry 策略处理。"""

    retryable = True


class OutputValidationError(AgentExecutionError):
    """Agent 返回值不满足节点声明的 outputs 契约（缺字段/类型不符）。"""


class TimeoutError_(AgentExecutionError):
    """Agent 执行超时（节点级 timeout_s）。"""


class FlowControlError(AgentFlowError):
    """流程控制异常，如节点访问次数超过 max_visits（死循环保护）。"""


class CheckpointError(AgentFlowError):
    """断点写入/读取失败。"""


class DeadlockError(FlowControlError):
    """调度器找不到任何可执行的节点，但流程尚未结束（join 永远无法满足）。"""
