"""内容创作流程的 Agent 实现（主题 → 大纲 → 初稿 → 审校 → 修订 → 发布）。

这套 Agent 与代码审查流程**没有共享任何业务代码**，只用同一套 ``BaseAgent``
契约和同一份引擎配置即可跑通 —— 这是“可扩展性”最直接的证据：

* 换业务：只写 Agent，不改 ``core/``。
* 换流程：只改 ``workflows/*.json``，不改 Agent。
* 复用能力：审校 Agent 的“反馈 → 修订”闭环，与代码审查的“审查 → 修复”
  是同一套模式（结构化反馈 + 回边 + 次数上限）。

为了离线可复现，这里的“写作”是模板化的：初稿刻意写得很短、漏掉要点，
审校 Agent 用可计算的标准（每节字数、要点覆盖、重复句、口水词）打分，
再把**结构化修改意见**回传给写作 Agent —— 与真实 LLM 流程中的多轮改稿结构一致。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..core.agent import AgentContext, AgentResult, BaseAgent, register_agent
from ..core.errors import AgentExecutionError
from ..core.message import KIND_FEEDBACK, KIND_HANDOFF, KIND_RESULT

#: 审校标准（可调参数，放在这里便于演示“规则即数据”）
SECTION_CHAR_TARGET = 150
SHORT_RATIO = 0.8
FILLER_WORDS = ("非常", "真的", "其实", "基本上", "众所周知")
#: 扩写用的句式池。为避免“重复句”被审校再次打回，写作时会拼上小节标题做区分。
PAD_SENTENCES = (
    "实现上优先使用标准库，避免引入额外依赖。",
    "调用方需要关注返回值与异常分支，必要时记录日志。",
    "该能力在测试环境覆盖了典型输入与边界输入。",
    "如果输入不满足约束，应当尽早失败并给出可读的错误信息。",
    "上线前建议补充监控指标，便于定位问题。",
    "评审时应把风险点与回滚方案一并记录。",
    "文档需要说明调用示例，降低新同学的理解成本。",
    "变更发布后观察一周指标，再决定是否扩大范围。",
)


@register_agent
class PlannerAgent(BaseAgent):
    """规划 Agent：把主题拆成有目标字数与要点的大纲。"""

    name = "planner"
    description = "规划 Agent：根据主题/受众/语气生成结构化大纲"
    inputs = ("topic", "audience", "tone", "sections")
    outputs = ("outline", "title", "section_count")
    version = "1.0"

    def run(self, ctx: AgentContext, inputs: Mapping[str, Any]) -> AgentResult:
        topic = str(inputs.get("topic") or "").strip()
        if not topic:
            raise AgentExecutionError("缺少主题（inputs.topic）")
        audience = str(inputs.get("audience") or "工程团队")
        tone = str(inputs.get("tone") or "务实")
        count = int(inputs.get("sections") or 3)

        templates = [
            ("背景与问题", ["为什么现在要做这件事", "当前痛点与影响范围"]),
            ("方案与做法", ["核心设计取舍", "关键实现步骤"]),
            ("落地与验证", ["如何度量效果", "风险与回滚方案"]),
            ("进一步演进", ["下一步计划", "长期维护建议"]),
        ]
        sections = [
            {
                "heading": heading,
                "key_points": points,
                "char_target": SECTION_CHAR_TARGET,
            }
            for heading, points in templates[:count]
        ]
        outline = {
            "title": f"{topic}：面向{audience}的实践说明",
            "topic": topic,
            "audience": audience,
            "tone": tone,
            "thesis": f"围绕「{topic}」，说明问题、方案与验证方式。",
            "sections": sections,
        }
        ctx.put_artifact("outline", outline, note="规划 Agent 生成的大纲")
        ctx.send("writer", KIND_HANDOFF, {"summary": f"大纲完成：{len(sections)} 个小节", "title": outline["title"]},
                 tags=("outline",))
        ctx.log(f"大纲：{outline['title']}（{len(sections)} 节）")
        return AgentResult(
            outputs={"outline": outline, "title": outline["title"], "section_count": len(sections)},
            artifacts=["outline"],
        )


@register_agent
class WriterAgent(BaseAgent):
    """写作 Agent：依据大纲写稿；收到审校意见时按意见定向修订。"""

    name = "writer"
    description = "写作 Agent：按大纲写稿，能依据审校意见定向修订"
    inputs = ("outline", "notes", "revision")
    outputs = ("draft", "char_count", "draft_round", "revised", "addressed")
    version = "1.0"

    def run(self, ctx: AgentContext, inputs: Mapping[str, Any]) -> AgentResult:
        outline = inputs.get("outline")
        if not isinstance(outline, Mapping):
            raise AgentExecutionError("缺少大纲（inputs.outline）")
        notes = [n for n in (inputs.get("notes") or []) if isinstance(n, Mapping)]
        round_no = ctx.incr("draft_round")
        revised = round_no > 1

        notes_by_heading: dict[str, list[dict[str, Any]]] = {}
        for note in notes:
            notes_by_heading.setdefault(str(note.get("heading") or ""), []).append(note)

        lines = [f"# {outline.get('title')}", "",
                 f"> 读者：{outline.get('audience')}　语气：{outline.get('tone')}", "",
                 str(outline.get("thesis") or ""), ""]
        for section in outline.get("sections", []):
            heading = str(section.get("heading"))
            target = int(section.get("char_target") or SECTION_CHAR_TARGET)
            body = self._compose(section, notes_by_heading.get(heading, []), revised)
            lines += [f"## {heading}", "", body, ""]
        draft = "\n".join(lines).rstrip() + "\n"

        char_count = _char_count(draft)
        addressed = [str(n.get("message")) for n in notes]
        ctx.put_artifact("draft", draft, note=f"第 {round_no} 稿")
        ctx.send("editor", KIND_HANDOFF,
                 {"summary": f"第 {round_no} 稿完成（{char_count} 字）", "revised": revised},
                 tags=("draft", f"round-{round_no}"))
        ctx.log(f"第 {round_no} 稿：{char_count} 字" + ("（已按审校意见修订）" if revised else ""))
        return AgentResult(
            outputs={"draft": draft, "char_count": char_count, "draft_round": round_no,
                     "revised": revised, "addressed": addressed},
            artifacts=["draft"],
            meta={"notes_applied": len(notes)},
        )

    def _compose(self, section: Mapping[str, Any], notes: list[dict[str, Any]], revised: bool) -> str:
        heading = str(section.get("heading"))
        points = [str(p) for p in section.get("key_points", [])]
        target = int(section.get("char_target") or SECTION_CHAR_TARGET)

        if not revised:
            # 初稿：只有一句概述 + 第一个要点（故意不达标，给审校留出改进空间）
            first = points[0] if points else heading
            return f"{heading}是本方案的核心环节，重点在于{first}。"

        # 修订稿：按意见确定目标字数，并保证覆盖所有要点
        wanted_target = target
        extra_points: list[str] = []
        for note in notes:
            if note.get("type") == "expand":
                wanted_target = max(wanted_target, int(note.get("target") or target))
            elif note.get("type") == "add-point":
                point = str(note.get("point") or "")
                if point:
                    extra_points.append(point)
        all_points = list(dict.fromkeys([*points, *extra_points]))
        sentences = [f"{point}是本节需要说明的关键点。" for point in all_points]
        index = 0
        while _char_count("".join(sentences)) < int(wanted_target * 1.15):
            # 每条扩写句都带上小节名，保证全文不出现重复句
            sentences.append(f"关于{heading}，{PAD_SENTENCES[index % len(PAD_SENTENCES)]}")
            index += 1
            if index > 40:
                break
        return "".join(sentences)


@register_agent
class EditorAgent(BaseAgent):
    """审校 Agent：用可计算的编辑标准打分，并给出结构化修改意见。"""

    name = "editor"
    description = "审校 Agent：检查字数、要点覆盖、重复与口水词，输出结构化意见"
    inputs = ("draft", "outline")
    outputs = ("verdict", "notes", "score", "revision", "summary", "metrics")
    version = "1.0"

    def run(self, ctx: AgentContext, inputs: Mapping[str, Any]) -> AgentResult:
        draft = str(inputs.get("draft") or "")
        outline = inputs.get("outline") or {}
        if not draft.strip():
            raise AgentExecutionError("没有可审校的稿件")
        revision = ctx.counter("draft_round")

        sections = _split_sections(draft)
        notes: list[dict[str, Any]] = []
        metrics: list[dict[str, Any]] = []
        for section in outline.get("sections", []):
            heading = str(section.get("heading"))
            target = int(section.get("char_target") or SECTION_CHAR_TARGET)
            body = sections.get(heading, "")
            chars = _char_count(body)
            missing = [p for p in section.get("key_points", []) if str(p) not in body]
            metrics.append({"heading": heading, "chars": chars, "target": target,
                            "missing_points": missing})
            if chars < target * SHORT_RATIO:
                notes.append({"type": "expand", "heading": heading, "target": target,
                              "message": f"「{heading}」仅 {chars} 字，需扩写到 {target} 字左右"})
            for point in missing:
                notes.append({"type": "add-point", "heading": heading, "point": str(point),
                              "message": f"「{heading}」缺少要点：{point}"})

        repeated = _repeated_sentences(draft)
        for sentence in repeated:
            notes.append({"type": "dedupe", "heading": "", "message": f"重复句：{sentence[:30]}…"})
        fillers = [w for w in FILLER_WORDS if w in draft]
        for word in fillers:
            notes.append({"type": "tone", "heading": "", "message": f"口水词建议替换：{word}"})

        blocking_notes = [n for n in notes if n["type"] in ("expand", "add-point", "dedupe")]
        verdict = "needs_revision" if blocking_notes else "pass"
        # 分数：每节字数达标率 - 缺陷扣分
        coverage = sum(min(1.0, m["chars"] / max(1, m["target"])) for m in metrics) / max(1, len(metrics))
        value = int(round(60 * coverage + 40 * (1 - min(1.0, len(blocking_notes) / 6))))
        value = max(0, min(100, value - 3 * len(fillers)))
        ctx.set("editor_verdict", verdict)
        ctx.set("revision", revision)
        payload = {"verdict": verdict, "revision": revision, "notes": notes,
                   "blocking": len(blocking_notes), "metrics": metrics,
                   "fillers": fillers, "score": value, "char_count": _char_count(draft)}
        ctx.put_artifact("editorial_review", payload, note=f"第 {revision} 次审校：{verdict}")
        ctx.send("writer", KIND_FEEDBACK,
                 {"summary": f"审校结论 {verdict}（{len(blocking_notes)} 条阻塞意见）", "notes": len(notes)},
                 tags=("review", f"round-{revision}"))
        ctx.log(f"审校：{verdict}，阻塞意见 {len(blocking_notes)} 条，评分 {value}")
        summary = f"第 {revision} 次审校：{verdict}（{len(blocking_notes)} 条待改，评分 {value}）"
        return AgentResult(
            outputs={"verdict": verdict, "notes": notes, "score": value, "revision": revision,
                     "summary": summary, "metrics": metrics},
            artifacts=["editorial_review"],
            meta={"notes": len(notes)},
        )


@register_agent
class PublisherAgent(BaseAgent):
    """发布 Agent：产出终稿与发布说明。"""

    name = "publisher"
    description = "发布 Agent：整合终稿、审校结论与发布说明"
    inputs = ("draft", "verdict", "score", "notes")
    outputs = ("document", "verdict", "char_count", "summary")
    version = "1.0"

    def run(self, ctx: AgentContext, inputs: Mapping[str, Any]) -> AgentResult:
        draft = str(inputs.get("draft") or "")
        verdict = str(inputs.get("verdict") or "pass")
        note_count = len(inputs.get("notes") or [])
        char_count = _char_count(draft)
        status = "可直接发布" if verdict == "pass" else "带已知问题发布（已达修订次数上限）"
        if verdict != "pass":
            # 没能在限定轮次内收敛：标记为“部分完成”，让上层调度/看板能区分
            ctx.set("escalated", True)
            ctx.set("publish_warning", status)
        footer = (
            f"\n\n---\n\n> 审校结论：**{verdict}**（评分 {inputs.get('score', '')}）；"
            f"修订过程中处理意见 {note_count} 条；全文约 {char_count} 字。\n"
        )
        document = draft.rstrip() + footer
        ctx.put_artifact("final_document", document, note="终稿")
        final = ctx.get_artifact("final_document")
        if final is not None:
            final.meta["filename"] = "final_document.md"
        summary = f"终稿 {char_count} 字，{status}"
        ctx.put_artifact("publish_note", {"verdict": verdict, "status": status,
                                          "char_count": char_count, "notes": note_count}, note="发布说明")
        ctx.send("user", KIND_RESULT, {"summary": summary}, tags=("delivery",))
        ctx.log(summary)
        return AgentResult(
            outputs={"document": document, "verdict": verdict, "char_count": char_count,
                     "summary": summary},
            artifacts=["final_document", "publish_note"],
        )


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------
def _char_count(text: str) -> int:
    """按非空白字符计数（中文场景比“词数”更稳定）。"""
    return len(re.sub(r"\s", "", text))


def _split_sections(draft: str) -> dict[str, str]:
    """按 ``## 标题`` 切分正文。"""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in draft.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = line[3:].strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


def _repeated_sentences(text: str) -> list[str]:
    seen: set[str] = set()
    repeated: list[str] = []
    for sentence in re.split(r"[。！？\n]", text):
        sentence = sentence.strip()
        if len(sentence) < 8:
            continue
        if sentence in seen:
            repeated.append(sentence)
        seen.add(sentence)
    return repeated
