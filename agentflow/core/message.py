"""Agent 之间的消息传递。

设计取舍
--------
课程里“Agent 间通信”有两种常见做法：
1. **黑板模式**：Agent 只读写共享状态，彼此不知道对方存在；
2. **消息传递**：Agent 显式 ``send(recipient, ...)``，有收发双方与消息类型。

AgentFlow 两者都支持，且刻意区分用途：

* 结构化数据走 **作品仓库 (Artifact Store)** —— 代码、审查结果等大对象，
  带版本号，便于对比与回滚（见 ``state.py``）。
* 协作意图走 **消息总线 (MessageBus)** —— 「我把代码交给你审」「我发现 3 个
  阻塞问题」「这一轮修复完成了」这类事件，形成可审计的对话轨迹。

消息是**不可变的**值对象，写入后不会变化，因此可以安全地跨线程追加、
写入断点和报告。
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

# 消息类型常量：避免各处硬编码字符串
KIND_TASK = "task"              # 引擎 -> Agent，派发任务
KIND_RESULT = "result"          # Agent -> 引擎，任务完成
KIND_HANDOFF = "handoff"        # Agent -> 下游 Agent，移交产物
KIND_FEEDBACK = "feedback"      # 审查者 -> 修复者，缺陷反馈
KIND_ESCALATION = "escalation"  # 升级给人处理
KIND_NOTE = "note"              # 普通说明
KIND_EVENT = "event"            # 引擎内部事件（节点开始/结束等）

_seq = itertools.count(1)


@dataclass(frozen=True)
class Message:
    """一条不可变消息。"""

    id: str
    ts: float
    sender: str
    recipient: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    #: 同一次运行/同一条因果链的标识，便于在日志里把对话串起来
    correlation_id: str = ""
    #: 若这是对某条消息的回复
    reply_to: str | None = None
    #: 附加标签，可用于过滤（例如 ("round", "2")）
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "sender": self.sender,
            "recipient": self.recipient,
            "kind": self.kind,
            "payload": self.payload,
            "correlation_id": self.correlation_id,
            "reply_to": self.reply_to,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        return cls(
            id=data["id"],
            ts=data["ts"],
            sender=data["sender"],
            recipient=data["recipient"],
            kind=data["kind"],
            payload=data.get("payload", {}),
            correlation_id=data.get("correlation_id", ""),
            reply_to=data.get("reply_to"),
            tags=tuple(data.get("tags", ())),
        )

    def summary(self) -> str:
        """给控制台/报告用的单行摘要。"""
        body = self.payload
        detail = body.get("summary") or body.get("message") or ""
        if not detail:
            keys = ", ".join(list(body)[:4])
            detail = f"{{{keys}}}" if keys else ""
        return f"[{self.kind}] {self.sender} → {self.recipient}: {detail}"


class MessageBus:
    """线程安全的消息总线，带完整历史记录。

    与真实的消息中间件相比，这里刻意做成**进程内、无投递语义**的极简实现：
    订阅者不消费消息（不阻塞、不丢弃），随时可以按收件人/类型/轮次检索历史。
    对“可观测优先”的 Agent 工作流来说，可回放的历史比队列语义更有价值。
    """

    def __init__(self, correlation_id: str = "") -> None:
        self.correlation_id = correlation_id
        self._messages: list[Message] = []
        self._lock = threading.RLock()

    # ---- 写入 ------------------------------------------------------
    def send(
        self,
        sender: str,
        recipient: str,
        kind: str = KIND_NOTE,
        payload: dict[str, Any] | None = None,
        *,
        reply_to: str | None = None,
        tags: Iterable[str] = (),
    ) -> Message:
        with self._lock:
            seq = next(_seq)
            message = Message(
                id=f"m{seq:04d}",
                ts=time.time(),
                sender=sender,
                recipient=recipient,
                kind=kind,
                payload=dict(payload or {}),
                correlation_id=self.correlation_id,
                reply_to=reply_to,
                tags=tuple(tags),
            )
            self._messages.append(message)
            return message

    def extend(self, messages: Iterable[Message]) -> None:
        with self._lock:
            self._messages.extend(messages)

    # ---- 读取 ------------------------------------------------------
    def all(self) -> list[Message]:
        with self._lock:
            return list(self._messages)

    def between(self, sender: str, recipient: str) -> list[Message]:
        return [m for m in self.all() if m.sender == sender and m.recipient == recipient]

    def by_kind(self, kind: str) -> list[Message]:
        return [m for m in self.all() if m.kind == kind]

    def for_recipient(self, recipient: str) -> list[Message]:
        return [m for m in self.all() if m.recipient in (recipient, "*")]

    def __len__(self) -> int:
        with self._lock:
            return len(self._messages)

    def __iter__(self) -> Iterator[Message]:
        return iter(self.all())

    # ---- 序列化 ----------------------------------------------------
    def to_list(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.all()]

    def restore(self, data: list[dict[str, Any]]) -> None:
        with self._lock:
            self._messages = [Message.from_dict(d) for d in data]

    def conversation(self, limit: int | None = None) -> list[str]:
        """人类可读的对话轨迹（用于报告与终端输出）。"""
        msgs = self.all()
        if limit:
            msgs = msgs[-limit:]
        return [m.summary() for m in msgs]
