"""代码审查流程的 Agent 实现（编码 → 审查 → 修复 → 交付）。

角色分工（对应工作流 ``workflows/code_review.json``）::

    coder       编码 Agent    依据需求生成首版模块，写作品 code/spec
    reviewer    审查 Agent    按维度（style / security）跑 AST 规则，写作品 review_*
    aggregator  汇总 Agent    合并两路审查结果，给出 verdict（fail / pass_with_warnings / pass）
    fixer       修复 Agent    按严重度定点修复（先修 error，再清 warning），产出新版本 code
    reporter    交付 Agent    生成终版代码与 Markdown 审查报告
    escalator   升级 Agent    自动修复失败/轮次超限时，输出人工介入清单

每个 Agent 只依赖 ``AgentContext``，彼此之间通过**作品 + 消息**协作，
所以任何一个都可以被替换成真实 LLM 版本而不影响流程定义。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..core.agent import AgentContext, AgentResult, BaseAgent, register_agent
from ..core.errors import AgentExecutionError
from ..core.message import KIND_ESCALATION, KIND_FEEDBACK, KIND_HANDOFF, KIND_RESULT
from .codemods import apply_fixes
from .linting import Finding, analyze, rule_catalog, score, summarize, verdict_of

# ---------------------------------------------------------------------------
# 编码 Agent 的模板：一个“朴素首版实现”
# 这是刻意保留的典型初稿缺陷（无文档字符串、无类型标注、裸 except、可变默认参数、
# SQL 字符串拼接、硬编码密钥、弱哈希），用于驱动“审查 → 修复 → 复审”闭环。
# 接入真实 LLM 时，把它换成模型输出即可（见 llm.py 与 CoderAgent 的 mode 参数）。
# ---------------------------------------------------------------------------
#: 模板模式的默认需求描述（`task` 入参可省略）
DEFAULT_TASK = "实现用户档案服务的保存、按名称查询、标签解析与文本指纹能力"

CODE_V1 = '''import hashlib
import json
import os

API_KEY = "sk-live-8f2c1d4e9a"
DEFAULT_TAGS = []

def save_profile(user, tags=DEFAULT_TAGS):
    try:
        data = {"name": user["name"], "tags": tags}
        with open("profile.json", "w") as f:
            json.dump(data, f)
        return True
    except:
        return False

def find_user(conn, name):
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE name = '%s'" % name)
    return cur.fetchall()

def fingerprint(text):
    return hashlib.md5(text.encode()).hexdigest()

def parse_tags(raw):
    tags = []
    for t in raw.split(","):
        tags.append(t.strip())
    return tags
'''

MODULE_DOCSTRING = (
    "用户档案服务。\n\n"
    "职责：\n"
    "    1. 读取/保存用户档案到本地 JSON 文件；\n"
    "    2. 提供按用户名查询数据库的辅助函数；\n"
    "    3. 提供标签解析与文本指纹工具。\n\n"
    "约束：\n"
    "    - 凭据一律从环境变量读取，禁止写入源码；\n"
    "    - 所有 SQL 必须使用参数化查询。"
)


@register_agent
class CoderAgent(BaseAgent):
    """源码准备 Agent：**生成**新实现，或**导入**已有源码作为待审对象。

    三种来源（``inputs``）：

    * ``template``（默认）——离线模板生成“朴素首版”，用于演示完整闭环；
    * ``llm`` —— 调用真实大模型生成（未配置 ``AGENTFLOW_LLM_*`` 时自动回退）；
    * ``file`` / 提供 ``path`` 或 ``source`` —— 直接审查**你自己的代码**
      （单文件；路径相对当前工作目录）。

    导入模式下不会改动源码语义，只是把它写进作品仓库 ``code``（v1），
    后续审查 → 修复 → 复审 → 交付全部照常工作。
    """

    name = "coder"
    description = "源码准备 Agent：生成首版实现（模板/LLM）或导入外部源码文件待审"
    inputs = ("task", "filename", "mode", "path", "source")
    outputs = ("code", "filename", "language", "lines", "generator")
    version = "1.2"

    def run(self, ctx: AgentContext, inputs: Mapping[str, Any]) -> AgentResult:
        task = str(inputs.get("task") or "").strip()
        mode = str(inputs.get("mode") or "template").strip().lower()
        path = str(inputs.get("path") or "").strip()
        inline = inputs.get("source")
        has_inline = isinstance(inline, str) and inline.strip() != ""

        if mode == "file" or path or has_inline:
            code, filename, generator = self._load_external(ctx, path, inline, inputs.get("filename"))
            origin = "imported"
            task = task or f"审查并加固已有模块 {filename}"
        else:
            filename = str(inputs.get("filename") or "profile_service.py")
            task = task or DEFAULT_TASK
            if mode == "llm":
                generator, code = self._generate_with_llm(ctx, task, filename)
            else:
                generator, code = "offline-template", CODE_V1
            origin = "generated"

        ctx.put_artifact("code", code, note=f"{generator} 提供的待审代码")
        # 把“真实文件名/来源”写进变量黑板，供下游节点与条件表达式复用
        ctx.set("filename", filename)
        ctx.set("code_origin", origin)
        ctx.put_artifact(
            "spec",
            {
                "task": task,
                "filename": filename,
                "generator": generator,
                "origin": origin,
                "source_path": path or ("<inline>" if has_inline else ""),
                "features": (
                    ["外部导入的待审模块"] if origin == "imported"
                    else ["保存用户档案", "按名称查询用户", "文本指纹", "标签解析"]
                ),
                "constraints": ["保持原有公共接口不变", "凭据从环境变量读取", "SQL 使用参数化查询"],
            },
            note="需求规格 / 待审对象说明",
        )
        lines = len(code.strip().splitlines())
        message = {"summary": f"提交《{filename}》（{lines} 行）供审查", "artifact": "code", "lines": lines}
        ctx.send("review_style", KIND_HANDOFF, message, tags=("handoff", "code"))
        ctx.send("review_security", KIND_HANDOFF, dict(message), tags=("handoff", "code"))
        ctx.log(f"{'导入' if origin == 'imported' else '生成'} {filename}：{lines} 行（generator={generator}）")

        return AgentResult(
            outputs={"code": code, "filename": filename, "language": "python", "lines": lines,
                     "generator": generator},
            artifacts=["code", "spec"],
            meta={"generator": generator, "lines": lines, "origin": origin},
        )

    def _load_external(self, ctx: AgentContext, path: str, inline: Any,
                       filename_input: Any) -> tuple[str, str, str]:
        """导入外部源码：支持内联字符串或文件路径，落库前做语法校验。"""
        from pathlib import Path

        if isinstance(inline, str) and inline.strip():
            code = inline if inline.endswith("\n") else inline + "\n"
            filename = str(filename_input or "snippet.py")
            generator = "inline-source"
        else:
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            if not candidate.exists():
                raise AgentExecutionError(f"待审文件不存在: {path}（当前目录 {Path.cwd()}）")
            if candidate.is_dir():
                raise AgentExecutionError(f"待审路径是目录，请指定单个源码文件: {path}")
            try:
                code = candidate.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                # 兼容 GBK 编码的历史文件
                code = candidate.read_text(encoding="gbk", errors="replace")
            filename = str(filename_input or candidate.name)
            generator = f"file:{candidate.name}"

        if not code.strip():
            raise AgentExecutionError(f"待审源码为空: {path or '<inline>'}")
        try:
            compile(code, filename, "exec")
        except SyntaxError as exc:
            raise AgentExecutionError(
                f"待审源码语法错误（{filename} 第 {exc.lineno} 行）: {exc.msg}"
            ) from exc
        ctx.log(f"导入外部源码：{filename}（{len(code.splitlines())} 行）")
        return code, filename, generator

    def _generate_with_llm(self, ctx: AgentContext, task: str, filename: str) -> tuple[str, str]:
        """可选路径：调用真实 LLM；未配置时自动回退到离线模板并记录原因。"""
        from .llm import get_provider

        provider = get_provider()
        if provider is None:
            ctx.log("未配置 AGENTFLOW_LLM_*，回退到离线模板生成")
            return "offline-template(fallback)", CODE_V1
        prompt = (
            "你是资深 Python 工程师。请依据需求编写一个单文件模块，直接输出代码，不要解释：\n"
            f"文件名：{filename}\n需求：{task}\n"
            "要求：使用标准库；包含 save_profile / find_user / fingerprint / parse_tags 四个函数。"
        )
        try:
            code = provider.complete(prompt, system="你只输出可运行的 Python 源码。")
        except Exception as exc:  # noqa: BLE001 - 外部依赖失败要能降级
            ctx.log(f"LLM 调用失败（{type(exc).__name__}），回退到离线模板")
            return "offline-template(fallback)", CODE_V1
        code = _strip_code_fence(code)
        try:
            compile(code, filename, "exec")
        except SyntaxError as exc:
            ctx.log(f"LLM 输出语法错误（{exc}），回退到离线模板")
            return "offline-template(fallback)", CODE_V1
        return f"llm:{getattr(provider, 'config', None) and provider.config.model or 'unknown'}", code


@register_agent
class ReviewerAgent(BaseAgent):
    """审查 Agent：按维度运行静态规则，产出结构化发现。

    同一个 Agent 类被工作流复用两次（风格 / 安全），只靠节点 ``params`` 区分 ——
    这就是“配置驱动”的复用：新增一个审查维度不需要新 Agent，只要新规则类别。
    """

    name = "reviewer"
    description = "审查 Agent：按类别运行 AST 规则，输出结构化发现与评分"
    inputs = ("code", "categories", "artifact", "review_name")
    outputs = ("findings", "error_count", "warning_count", "info_count", "verdict", "score", "summary")
    version = "1.1"

    def run(self, ctx: AgentContext, inputs: Mapping[str, Any]) -> AgentResult:
        code = str(inputs.get("code") or "")
        categories = list(inputs.get("categories") or ["style"])
        artifact_name = str(inputs.get("artifact") or f"review_{'_'.join(categories)}")
        review_name = str(inputs.get("review_name") or "代码审查")
        if not code.strip():
            raise AgentExecutionError(f"{review_name}: 没有可审查的代码（code 为空）")
        try:
            findings = analyze(code, categories)
        except SyntaxError as exc:
            raise AgentExecutionError(f"{review_name}: 源码无法解析（{exc.msg}，第 {exc.lineno} 行）") from exc

        stats = summarize(findings)
        current_score = score(findings)
        verdict = verdict_of(findings)
        round_no = max(1, ctx.counter("review_round") + 1)
        payload = {
            "review": review_name,
            "categories": categories,
            "round": round_no,
            "findings": [f.to_dict() for f in findings],
            "file_lines": len(code.splitlines()),
            **stats,
            "score": current_score,
            "verdict": verdict,
            "code_version": (ctx.get_artifact("code").version if ctx.get_artifact("code") else 0),
        }
        ctx.put_artifact(artifact_name, payload, note=f"{review_name}：{stats['error']} error / {stats['warning']} warning")

        message = {
            "summary": f"{review_name}完成：{stats['error']} 个阻塞、{stats['warning']} 个警告，评分 {current_score}",
            "artifact": artifact_name,
            "verdict": verdict,
            "error_count": stats["error"],
            "warning_count": stats["warning"],
        }
        ctx.send("aggregator", KIND_FEEDBACK, message, tags=("review", artifact_name))
        ctx.log(f"{review_name}：error={stats['error']} warning={stats['warning']} info={stats['info']} → {verdict}")

        return AgentResult(
            outputs={
                "findings": payload["findings"],
                "error_count": stats["error"],
                "warning_count": stats["warning"],
                "info_count": stats["info"],
                "verdict": verdict,
                "score": current_score,
                "summary": message["summary"],
                "artifact": artifact_name,
            },
            artifacts=[artifact_name],
            meta={"categories": categories, "rules": sorted({f.rule_id for f in findings})},
        )


@register_agent
class AggregateAgent(BaseAgent):
    """汇总 Agent：合并多路审查结果，做质量门禁判定。"""

    name = "aggregator"
    description = "汇总 Agent：合并多路审查，输出 verdict / 评分 / 规则分布"
    inputs = ("style_review", "security_review")
    outputs = ("findings", "error_count", "warning_count", "info_count", "verdict", "score",
               "by_rule", "summary", "iteration")
    version = "1.1"

    def run(self, ctx: AgentContext, inputs: Mapping[str, Any]) -> AgentResult:
        reviews = [r for r in (inputs.get("style_review"), inputs.get("security_review")) if isinstance(r, Mapping)]
        if not reviews:
            raise AgentExecutionError("汇总节点没有收到任何审查结果")

        merged: dict[tuple[str, int], dict[str, Any]] = {}
        for review in reviews:
            for raw in review.get("findings", []):
                finding = Finding.from_dict(raw)
                key = finding.key
                existing = merged.get(key)
                if existing is None or _severity_rank(finding.severity) < _severity_rank(existing["severity"]):
                    merged[key] = finding.to_dict()
        findings = sorted(merged.values(), key=lambda f: (_severity_rank(f["severity"]), f["line"], f["rule_id"]))

        stats = summarize(findings)
        iteration = ctx.incr("review_round")
        ctx.set("verdict", verdict_of(findings))
        ctx.set("error_count", stats["error"])
        ctx.set("warning_count", stats["warning"])

        by_rule = stats["by_rule"]
        summary = (
            f"第 {iteration} 轮汇总：{stats['error']} 个阻塞 / {stats['warning']} 个警告 / "
            f"{stats['info']} 条提示，评分 {score(findings)}，结论 {ctx.get('verdict')}"
        )
        payload = {
            "iteration": iteration,
            "findings": findings,
            "by_category": stats["by_category"],
            "by_rule": by_rule,
            "fixable": stats["fixable"],
            **{k: stats[k] for k in ("error", "warning", "info", "total")},
            "verdict": ctx.get("verdict"),
            "score": score(findings),
            "rounds": [{"review": r.get("review"), "verdict": r.get("verdict"),
                        "error": r.get("error"), "warning": r.get("warning")} for r in reviews],
        }
        ctx.put_artifact("review", payload, note=f"第 {iteration} 轮汇总")
        ctx.send("fixer", KIND_FEEDBACK, {"summary": summary, "verdict": ctx.get("verdict")},
                 tags=("summary", f"round-{iteration}"))
        ctx.log(summary)

        return AgentResult(
            outputs={
                "findings": findings,
                "error_count": stats["error"],
                "warning_count": stats["warning"],
                "info_count": stats["info"],
                "verdict": ctx.get("verdict"),
                "score": payload["score"],
                "by_rule": by_rule,
                "summary": summary,
                "iteration": iteration,
            },
            artifacts=["review"],
            meta={"rounds": len(reviews)},
        )


@register_agent
class FixerAgent(BaseAgent):
    """修复 Agent：按严重度定点修复当前代码，写回新版本作品。"""

    name = "fixer"
    description = "修复 Agent：按严重度（error/warning）应用 code mod，写回新版本 code"
    inputs = ("code", "findings", "severity", "role")
    outputs = ("code", "applied", "skipped", "fixed_count", "remaining_errors",
               "remaining_warnings", "compiled")
    version = "1.1"

    def run(self, ctx: AgentContext, inputs: Mapping[str, Any]) -> AgentResult:
        code = str(inputs.get("code") or "")
        severities = tuple(inputs.get("severity") or ("error",))
        role = str(inputs.get("role") or "修复")
        categories = inputs.get("categories")
        if not code.strip():
            raise AgentExecutionError(f"{role}: 没有可修复的代码")

        previous = ctx.get_artifact("code")
        previous_version = previous.version if previous else 0
        outcome = apply_fixes(
            code,
            severities=tuple(severities),
            categories=tuple(categories) if categories else None,
            context={"module_docstring": _module_docstring_for(ctx)},
        )
        if not outcome.compiled:
            raise AgentExecutionError(f"{role}: 修复后源码语法校验失败（{outcome.error}）")

        applied_count = len(outcome.applied)
        if applied_count:
            ctx.put_artifact("code", outcome.source, note=f"{role}：修复 {applied_count} 项 {severities}")
        ctx.put_artifact(
            "patch",
            {
                "role": role,
                "severities": list(severities),
                "from_version": previous_version,
                "to_version": previous_version + (1 if applied_count else 0),
                "applied": outcome.applied,
                "skipped": outcome.skipped,
                "before": outcome.before,
                "after": outcome.after,
            },
            note=f"{role}：{applied_count} 项",
        )
        ctx.incr("fix_round")
        ctx.set("last_fix_applied", applied_count)
        summary = f"{role}处理 {applied_count} 项（error {outcome.before.get('error', 0)} → {outcome.after.get('error', 0)}）"
        ctx.send("review_style", KIND_HANDOFF, {"summary": summary + "，请复审"}, tags=("handoff", "fix"))
        ctx.send("review_security", KIND_HANDOFF, {"summary": summary + "，请复审"}, tags=("handoff", "fix"))
        ctx.log(summary)
        for item in outcome.applied[:8]:
            ctx.log(f"  ✓ {item['rule_id']}"
                    f"{(' · ' + item['symbol']) if item.get('symbol') else ''}")

        return AgentResult(
            outputs={
                "code": outcome.source,
                "applied": outcome.applied,
                "skipped": outcome.skipped,
                "fixed_count": applied_count,
                "remaining_errors": outcome.after.get("error", 0),
                "remaining_warnings": outcome.after.get("warning", 0),
                "compiled": outcome.compiled,
            },
            artifacts=["code", "patch"],
            meta={"severities": list(severities), "applied": applied_count},
        )


@register_agent
class ReporterAgent(BaseAgent):
    """交付 Agent：产出终版代码与可提交的 Markdown 审查报告。"""

    name = "reporter"
    description = "交付 Agent：生成终版代码、审查报告与交付摘要"
    inputs = ("code", "findings", "verdict", "task", "filename")
    outputs = ("report_md", "verdict", "score", "summary", "final_code")
    version = "1.1"

    def run(self, ctx: AgentContext, inputs: Mapping[str, Any]) -> AgentResult:
        code = str(inputs.get("code") or "")
        findings = [Finding.from_dict(f) for f in (inputs.get("findings") or [])]
        verdict = str(inputs.get("verdict") or verdict_of(findings))
        filename = _resolve_filename(ctx, inputs)
        task = _resolve_task(ctx, inputs)
        stats = summarize(findings)
        code_artifact = ctx.get_artifact("code")
        history = code_artifact.history if code_artifact else []
        patch = ctx.get_artifact("patch")
        patch_data = patch.content if patch and isinstance(patch.content, Mapping) else {}

        # 首版 → 终版的 diff
        diff_lines: list[str] = []
        if len(history) >= 2:
            import difflib

            diff_lines = list(
                difflib.unified_diff(
                    str(history[0]["content"]).splitlines(),
                    str(history[-1]["content"]).splitlines(),
                    fromfile=f"{filename} (v1 / 编码 Agent)",
                    tofile=f"{filename} (v{code_artifact.version} / 修复 Agent)",
                    lineterm="",
                )
            )

        summary = (
            f"交付 {filename}：v{code_artifact.version if code_artifact else 1}，"
            f"剩余 {stats['error']} 阻塞 / {stats['warning']} 警告，结论 {verdict}"
        )
        report = _render_report(
            filename=filename, task=task, verdict=verdict, stats=stats,
            findings=findings, diff_lines=diff_lines, patch=patch_data,
            final_version=code_artifact.version if code_artifact else 1,
            final_code=code, rounds=ctx.counter("review_round"),
        )
        ctx.put_artifact("final_code", code, note="终版代码")
        if code_artifact is not None:
            code_artifact.meta["filename"] = filename
        final_artifact = ctx.get_artifact("final_code")
        if final_artifact is not None:
            final_artifact.meta["filename"] = f"final_{filename}"
        ctx.put_artifact("report", report, note="审查报告")
        report_artifact = ctx.get_artifact("report")
        if report_artifact is not None:
            report_artifact.meta["filename"] = "review_report.md"
        ctx.put_artifact(
            "delivery",
            {
                "filename": filename,
                "verdict": verdict,
                "score": score(findings),
                "versions": code_artifact.version if code_artifact else 1,
                "review_rounds": ctx.counter("review_round"),
                "fix_rounds": ctx.counter("fix_round"),
                "summary": summary,
            },
            note="交付摘要",
        )
        ctx.send("user", KIND_RESULT, {"summary": summary, "verdict": verdict}, tags=("delivery",))
        ctx.log(summary)

        return AgentResult(
            outputs={"report_md": report, "verdict": verdict, "score": score(findings),
                     "summary": summary, "final_code": code},
            artifacts=["report", "final_code", "delivery"],
            meta={"findings": stats["total"], "diff_hunks": len(diff_lines)},
        )


@register_agent
class EscalatorAgent(BaseAgent):
    """升级 Agent：自动流程处理不了时，给人一份可执行的清单。"""

    name = "escalator"
    description = "升级 Agent：自动修复失败或轮次超限时产出人工介入清单"
    inputs = ("findings", "verdict")
    outputs = ("escalated", "reason", "todo", "summary")
    version = "1.0"

    def run(self, ctx: AgentContext, inputs: Mapping[str, Any]) -> AgentResult:
        findings = [Finding.from_dict(f) for f in (inputs.get("findings") or [])]
        verdict = str(inputs.get("verdict") or ctx.get("verdict") or "unknown")
        reason = ctx.get("escalation_reason") or (
            f"自动修复未能收敛（review_round={ctx.counter('review_round')}, "
            f"fix_round={ctx.counter('fix_round')}）"
        )
        todo = [
            f"[{f.severity}] {f.rule_id}（第 {f.line} 行）{f.message} → {f.hint}"
            for f in findings
            if f.severity in ("error", "warning")
        ]
        ctx.set("escalated", True)
        ctx.set("escalation_reason", reason)
        payload = {"reason": reason, "verdict": verdict, "todo": todo,
                   "review_round": ctx.counter("review_round"), "fix_round": ctx.counter("fix_round")}
        ctx.put_artifact("escalation", payload, note="升级人工处理")
        ctx.send("*", KIND_ESCALATION, {"summary": f"需要人工介入：{reason}", "todo": len(todo)},
                 tags=("escalation",))
        ctx.log(f"升级：{reason}（待处理 {len(todo)} 项）")
        return AgentResult(
            outputs={"escalated": True, "reason": reason, "todo": todo,
                     "summary": f"{len(todo)} 项待人工处理"},
            artifacts=["escalation"],
            logs=[f"升级原因：{reason}"],
        )


@register_agent
class RuleCatalogAgent(BaseAgent):
    """工具 Agent：输出当前规则清单（演示“零改引擎新增 Agent”）。"""

    name = "rule_catalog"
    description = "规则清单 Agent：枚举可用的审查规则与可修复性"
    inputs = ()
    outputs = ("rules", "count", "fixable")
    version = "1.0"

    def run(self, ctx: AgentContext, inputs: Mapping[str, Any]) -> AgentResult:
        rules = rule_catalog()
        fixable = sum(1 for r in rules if r["fixable"])
        ctx.put_artifact("rule_catalog", rules, note="规则清单")
        ctx.log(f"共 {len(rules)} 条规则，其中 {fixable} 条支持自动修复")
        return AgentResult(outputs={"rules": rules, "count": len(rules), "fixable": fixable},
                           artifacts=["rule_catalog"])


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _resolve_filename(ctx: AgentContext, inputs: Mapping[str, Any]) -> str:
    """确定交付文件名：入参 → 黑板（coder 写入）→ spec 作品 → 兜底。

    审查外部文件时 ``inputs.filename`` 通常是空的（由 path 推导），
    必须回退到 coder 写进黑板/作品里的真实文件名，否则产物会被叫成 module.py。
    """
    explicit = str(inputs.get("filename") or "").strip()
    if explicit:
        return explicit
    from_var = str(ctx.get("filename") or "").strip()
    if from_var:
        return from_var
    spec_artifact = ctx.get_artifact("spec")
    if spec_artifact is not None and isinstance(spec_artifact.content, Mapping):
        derived = str(spec_artifact.content.get("filename") or "").strip()
        if derived:
            return derived
    return "module.py"


def _resolve_task(ctx: AgentContext, inputs: Mapping[str, Any]) -> str:
    """需求描述：入参 → spec 作品（coder 写入的最终版，含外部导入自动生成的描述）。"""
    explicit = str(inputs.get("task") or "").strip()
    if explicit:
        return explicit
    spec_artifact = ctx.get_artifact("spec")
    if spec_artifact is not None and isinstance(spec_artifact.content, Mapping):
        return str(spec_artifact.content.get("task") or "").strip()
    return ""


def _module_docstring_for(ctx: AgentContext) -> str:
    """按“待审对象”生成模块文档字符串。

    审查外部文件时不能套用内置模板的说明（否则给计费模块写上“用户档案服务”），
    因此从 spec 作品里取文件名/来源/需求描述来组装。
    """
    spec_artifact = ctx.get_artifact("spec")
    spec = spec_artifact.content if spec_artifact and isinstance(spec_artifact.content, Mapping) else {}
    filename = str(spec.get("filename") or "")
    generator = str(spec.get("generator") or "")
    if "file:" in generator or generator == "inline-source":
        task = str(spec.get("task") or "").strip()
        return (
            f"{filename or '该模块'} 的模块说明（由修复 Agent 自动补充）。\n\n"
            f"待审对象：{filename or '外部导入源码'}\n"
            f"审查目的：{task or '代码审查与加固'}\n"
            "约束：凭据一律从环境变量读取；SQL 使用参数化查询。\n"
            "TODO: 由修复 Agent 依据文件信息生成，请人工确认职责与使用方式。"
        )
    return MODULE_DOCSTRING


def _severity_rank(severity: str) -> int:
    return {"error": 0, "warning": 1, "info": 2}.get(severity, 9)


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip() + "\n"


def _render_report(*, filename: str, task: str, verdict: str, stats: Mapping[str, Any],
                   findings: list[Finding], diff_lines: list[str], patch: Mapping[str, Any],
                   final_version: int, final_code: str, rounds: int) -> str:
    badge = {"pass": "✅ 通过", "pass_with_warnings": "⚠️ 通过（有告警）", "fail": "❌ 未通过"}.get(verdict, verdict)
    lines = [
        f"# 代码审查报告 · {filename}",
        "",
        f"- 审查结论：**{badge}**",
        f"- 总分：**{score(findings)} / 100**",
        f"- 迭代轮次：{rounds} 轮审查",
        f"- 代码版本：v{final_version}",
        f"- 阻塞问题：{stats.get('error', 0)}　警告：{stats.get('warning', 0)}　提示：{stats.get('info', 0)}",
        "",
        "## 一、需求",
        "",
        task or "（未提供）",
        "",
        "## 二、剩余问题",
        "",
    ]
    blocking = [f for f in findings if f.severity in ("error", "warning")]
    if blocking:
        lines += ["| 严重度 | 规则 | 行 | 说明 | 建议 |", "| --- | --- | --- | --- | --- |"]
        for f in blocking:
            lines.append(f"| {f.severity} | `{f.rule_id}` | {f.line} | {f.message} | {f.hint} |")
    else:
        lines.append("无阻塞问题，无告警。")
    hints = [f for f in findings if f.severity == "info"]
    if hints:
        lines += ["", "## 三、提示项", "", "| 规则 | 行 | 说明 | 建议 |", "| --- | --- | --- | --- |"]
        for f in hints:
            lines.append(f"| `{f.rule_id}` | {f.line} | {f.message} | {f.hint} |")

    lines += ["", "## 四、自动修复记录", ""]
    applied = patch.get("applied") or []
    if applied:
        lines += ["| 规则 | 位置 | 处理 |", "| --- | --- | --- |"]
        for item in applied:
            symbol = f" · {item['symbol']}" if item.get("symbol") else ""
            lines.append(f"| `{item['rule_id']}` | L{item.get('line', 0)}{symbol} | 已自动修复 |")
    else:
        lines.append("本轮无自动修复项。")
    skipped = patch.get("skipped") or []
    if skipped:
        lines += ["", "未能自动修复：", ""]
        for item in skipped[:20]:
            lines.append(f"- `{item.get('rule_id')}`（L{item.get('line', 0)}）：{item.get('reason', '')}")

    lines += ["", "## 五、代码演进 (v1 → 终版)", "", "```diff"]
    lines += diff_lines[:200] if diff_lines else ["(仅一个版本，无差异)"]
    lines += ["```", "", "## 六、终版代码", "", f"```python\n{final_code.rstrip()}\n```", ""]
    return "\n".join(lines) + "\n"
