"""端到端测试：两个内置工作流 + 审查规则 + 自动修复。

这些测试是“流程完整性”的直接证据：
``python -m unittest discover tests`` 能证明编码→审查→修复→复审能收敛、
第二条工作流能复用同一引擎、自动修复不会把代码改坏。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentflow.agents.codemods import FIXERS, apply_fixes
from agentflow.agents.linting import analyze, rule_catalog, score, summarize, verdict_of
from agentflow.core.agent import default_registry
from agentflow.core.engine import Engine
from agentflow.core.graph import load_workflow
from agentflow.core.layout import compute_layout
from agentflow.core.report import build_report, render_graph_svg

BUGGY_SOURCE = '''import hashlib
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

#: 外部代码审查用例（模拟一个“历史遗留模块”）
EXTERNAL_SOURCE = '''import hashlib
import json
import os

API_TOKEN = "ghp_9f3c1d4e9a2b"
DEFAULT_FILTERS = {}

def fetch_orders(conn, status):
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE status = '%s'" % status)
        return cur.fetchall()
    except:
        return []

def save_receipt(order, path):
    with open(path, "w") as f:
        json.dump(order, f)

def checksum(payload):
    return hashlib.sha1(payload.encode()).hexdigest()

def normalize(items, filters=DEFAULT_FILTERS):
    out = []
    for item in items:
        if item.get("status") in filters.get("hide", []):
            continue
        out.append(item)
    return out
'''


class TempDirMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


# ---------------------------------------------------------------------------
# 审查规则
# ---------------------------------------------------------------------------
class TestRulePacks(unittest.TestCase):
    def test_catalog_has_both_categories(self):
        rules = rule_catalog()
        self.assertTrue(rules)
        categories = {rule["category"] for rule in rules}
        self.assertEqual({"style", "security"} - categories, set())
        # 每条规则都应该有对应的自动修复（或明确标记为不可自动修复）
        for rule in rules:
            if rule["fixable"]:
                self.assertIn(rule["rule_id"], FIXERS, f"{rule['rule_id']} 声称可修复但没有 fixer")

    def test_style_rules_fire(self):
        findings = analyze(BUGGY_SOURCE, ["style"])
        rule_ids = {f.rule_id for f in findings}
        for expected in ("module-docstring", "function-docstring", "missing-type-hints",
                         "mutable-default-argument", "bare-except", "unused-import",
                         "open-without-encoding"):
            self.assertIn(expected, rule_ids)

    def test_security_rules_fire(self):
        findings = analyze(BUGGY_SOURCE, ["security"])
        rule_ids = {f.rule_id for f in findings}
        self.assertEqual({"sql-injection", "hardcoded-secret", "weak-hash"} - rule_ids, set())
        verdicts = {f.rule_id: f.severity for f in findings}
        self.assertEqual(verdicts["sql-injection"], "error")
        self.assertEqual(verdicts["hardcoded-secret"], "error")

    def test_clean_code_has_no_blockers(self):
        clean = '''"""已修复的模块。"""

def add(left: int, right: int) -> int:
    """返回两数之和。

    参数: left, right
    返回: int
    """
    return left + right
'''
        findings = analyze(clean)
        self.assertEqual(summarize(findings)["error"], 0)
        self.assertEqual(summarize(findings)["warning"], 0)
        self.assertEqual(verdict_of(findings), "pass")
        self.assertEqual(score(findings), 100)

    def test_summarize_and_verdict(self):
        findings = analyze(BUGGY_SOURCE)
        stats = summarize(findings)
        self.assertEqual(stats["error"], 3)
        self.assertGreater(stats["warning"], 5)
        self.assertEqual(verdict_of(findings), "fail")
        self.assertLess(score(findings), 100)

    def test_syntax_error_propagates(self):
        with self.assertRaises(SyntaxError):
            analyze("def broken(:\n")


# ---------------------------------------------------------------------------
# 自动修复
# ---------------------------------------------------------------------------
class TestCodemods(unittest.TestCase):
    def test_error_fix_only_touches_errors(self):
        outcome = apply_fixes(BUGGY_SOURCE, severities=("error",))
        self.assertTrue(outcome.compiled)
        self.assertEqual(outcome.after["error"], 0)
        self.assertGreater(outcome.after["warning"], 0, "只修 error 时 warning 应保留")
        self.assertNotIn("sk-live", outcome.source)
        self.assertIn("os.environ.get('API_KEY'", outcome.source)
        self.assertIn("sha256", outcome.source)
        # 参数化查询：占位符不应被引号包住
        self.assertIn("WHERE name = ?", outcome.source)
        self.assertNotIn("'?'", outcome.source)

    def test_warning_fix_reaches_zero(self):
        first = apply_fixes(BUGGY_SOURCE, severities=("error",))
        second = apply_fixes(first.source, severities=("warning",))
        self.assertTrue(second.compiled)
        final = analyze(second.source)
        stats = summarize(final)
        self.assertEqual(stats["error"], 0)
        self.assertEqual(stats["warning"], 0)
        # 修完的代码必须仍然可以编译
        compile(second.source, "<final>", "exec")

    def test_keeps_os_import_when_secret_fix_needs_it(self):
        outcome = apply_fixes(BUGGY_SOURCE, severities=("error", "warning"))
        self.assertIn("import os", outcome.source)
        self.assertIn("os.environ", outcome.source)
        self.assertNotIn("\nimport os\nimport os", outcome.source)

    def test_mutable_default_becomes_none_guard(self):
        outcome = apply_fixes(BUGGY_SOURCE, severities=("warning",))
        self.assertIn("if tags is None:", outcome.source)
        self.assertIn("tags: list | None", outcome.source)

    def test_docstrings_are_not_double_quoted(self):
        outcome = apply_fixes(
            BUGGY_SOURCE,
            severities=("warning",),
            context={"module_docstring": "用户档案服务。\n\n职责：演示模块。"},
        )
        self.assertNotIn("'''\"\"\"", outcome.source)
        self.assertIn("用户档案服务", outcome.source)
        self.assertTrue(outcome.source.lstrip().startswith('"""'))
        compile(outcome.source, "<doc>", "exec")

    def test_broken_source_is_rejected_safely(self):
        outcome = apply_fixes("def broken(:\n", severities=("warning",))
        self.assertFalse(outcome.compiled)
        self.assertEqual(outcome.source, "def broken(:\n")


# ---------------------------------------------------------------------------
# 工作流 1：代码审查
# ---------------------------------------------------------------------------
class TestCodeReviewWorkflow(TempDirMixin, unittest.TestCase):
    def run_workflow(self, inputs=None, **engine_kwargs):
        spec = load_workflow("code_review", registry=default_registry)
        engine = Engine(default_registry, run_root=self.tmp, echo=False, **engine_kwargs)
        return engine.run(spec, inputs or {})

    def test_end_to_end_converges(self):
        result = self.run_workflow({"task": "实现用户档案服务"})
        self.assertEqual(result.status, "passed", result.error)

        state = result.state
        # 1) 三个及以上 Agent 参与
        agents = {run.agent for run in state.node_runs}
        self.assertGreaterEqual(len(agents), 3)
        self.assertTrue({"coder", "reviewer", "aggregator", "fixer"} <= agents)

        # 2) 两路审查并行（同一波次），join 只执行一次
        waves = {run.wave for run in state.node_runs if run.node_id == "review_style"}
        join_waves = {run.wave for run in state.node_runs if run.node_id == "aggregate"}
        first_review_wave = min(waves)
        self.assertEqual(len([r for r in state.node_runs
                              if r.node_id == "review_style" and r.wave == first_review_wave]), 1)
        self.assertGreater(min(join_waves), first_review_wave)

        # 3) 循环确实发生：至少两轮审查
        self.assertGreaterEqual(state.counter("review_round"), 3)
        self.assertEqual(state.get("verdict"), "pass")

        # 4) 终版代码干净，且版本递增（编码 → 修复）
        code = state.artifact("code")
        self.assertIsNotNone(code)
        self.assertGreaterEqual(code.version, 3)
        final_findings = analyze(code.text())
        self.assertEqual(summarize(final_findings)["error"], 0)
        self.assertEqual(summarize(final_findings)["warning"], 0)

        # 5) Agent 间消息形成完整协作轨迹
        bus = state.bus
        self.assertGreaterEqual(len(bus), 10)
        self.assertTrue(bus.between("coder", "review_style"))
        self.assertTrue(bus.between("fix_errors", "review_style"))
        self.assertTrue(bus.between("finalize", "user"))

        # 6) 产物齐全
        for name in ("report.html", "summary.json", "state.json", "events.jsonl",
                     "checkpoint.json", "workflow.json"):
            self.assertTrue((result.run_dir / name).exists(), f"缺少 {name}")
        self.assertTrue((result.run_dir / "artifacts" / "profile_service.py").exists())
        self.assertTrue((result.run_dir / "artifacts" / "review_report.md").exists())
        self.assertTrue((result.run_dir / "artifacts" / "versions").is_dir())

        # 7) 报告是自包含 HTML 且包含关键信息
        html = result.report_path.read_text(encoding="utf-8")
        self.assertIn("<svg", html)
        self.assertIn("工作流执行图", html)
        self.assertIn("Agent 协作", html)

    def test_retry_recovers_from_injected_fault(self):
        result = self.run_workflow({"task": "实现用户档案服务"},
                                   fault_injection={"coder": 1})
        self.assertEqual(result.status, "passed", result.error)
        retries = [e for e in result.tracer.events if e["type"] == "retry"]
        self.assertEqual(len(retries), 1)

    def test_escalation_when_round_budget_exceeded(self):
        # max_rounds=1：第一轮就有阻塞问题，应升级人工而不是无限修
        result = self.run_workflow({"task": "实现用户档案服务", "max_rounds": 1})
        self.assertEqual(result.status, "partial", result.error)
        self.assertTrue(result.state.get("escalated"))
        self.assertIn("escalation", result.state.artifacts)
        escalations = [m for m in result.state.bus.all() if m.kind == "escalation"]
        self.assertTrue(escalations)

    def test_report_has_status_colors(self):
        result = self.run_workflow({"task": "实现用户档案服务"})
        html = result.report_path.read_text(encoding="utf-8")
        self.assertIn("#22c55e", html)  # 通过态颜色
        self.assertIn("修复 Agent", html)

    # ---- 审查外部代码（不走内置模板） ------------------------------
    def test_review_external_file(self):
        """把任意文件交给流程审查：导入 → 修复 → 复审 → 交付。"""
        target = self.tmp / "legacy_billing.py"
        target.write_text(EXTERNAL_SOURCE, encoding="utf-8")
        result = self.run_workflow({"path": str(target)})
        self.assertEqual(result.status, "passed", result.error)

        # 文件名由路径推导（不是内置模板的 profile_service.py）
        self.assertEqual(result.state.get("filename"), "legacy_billing.py")
        self.assertEqual(result.state.get("code_origin"), "imported")
        self.assertTrue((result.run_dir / "artifacts" / "legacy_billing.py").exists())
        self.assertTrue((result.run_dir / "artifacts" / "final_legacy_billing.py").exists())

        # v1 是导入的原文，修复产生新版本；终版三项阻塞问题都被修掉
        code = result.state.artifact("code")
        self.assertGreaterEqual(code.version, 2)
        self.assertIn("os.environ.get('API_TOKEN'", code.text())
        self.assertIn("WHERE status = ?", code.text())
        self.assertIn("sha256", code.text())
        self.assertNotIn("ghp_9f3c1d4e9a2b", code.text())
        final = analyze(code.text())
        self.assertEqual(summarize(final)["error"], 0)
        self.assertEqual(summarize(final)["warning"], 0)

        # 报告里应出现被审文件名（Markdown 报告 + HTML 报告），而不是模板文件名
        report_md = result.state.artifact("report").text()
        self.assertIn("legacy_billing.py", report_md)
        self.assertNotIn("profile_service.py", report_md)
        self.assertIn("审查并加固已有模块 legacy_billing.py", report_md)
        html = result.report_path.read_text(encoding="utf-8")
        self.assertIn("legacy_billing.py", html)

    def test_review_inline_source(self):
        source = (
            "import hashlib\n"
            "def digest(text):\n"
            "    return hashlib.md5(text.encode()).hexdigest()\n"
        )
        result = self.run_workflow({"source": source})
        self.assertEqual(result.status, "passed", result.error)
        self.assertEqual(result.state.get("filename"), "snippet.py")
        self.assertIn("sha256", result.state.artifact("code").text())

    def test_missing_target_file_fails_clearly(self):
        result = self.run_workflow({"path": "definitely-not-here.py"})
        self.assertEqual(result.status, "failed")
        self.assertIn("待审文件不存在", result.error or "")

    def test_broken_target_file_fails_clearly(self):
        target = self.tmp / "broken.py"
        target.write_text("def broken(:\n    pass\n", encoding="utf-8")
        result = self.run_workflow({"path": str(target)})
        self.assertEqual(result.status, "failed")
        self.assertIn("语法错误", result.error or "")

    def test_unknown_input_warns_instead_of_silently_ignoring(self):
        result = self.run_workflow({"typo_key": "x"})
        warnings = [e for e in result.tracer.events if e["type"] == "warning"]
        self.assertTrue(warnings)
        self.assertIn("typo_key", warnings[0]["message"])


# ---------------------------------------------------------------------------
# 工作流 2：内容创作（证明引擎可复用）
# ---------------------------------------------------------------------------
class TestContentWorkflow(TempDirMixin, unittest.TestCase):
    def test_content_loop_converges(self):
        spec = load_workflow("content_creation", registry=default_registry)
        engine = Engine(default_registry, run_root=self.tmp, echo=False)
        result = engine.run(spec, {"topic": "多 Agent 工作流落地", "sections": 3})
        self.assertEqual(result.status, "passed", result.error)
        self.assertEqual(result.state.counter("draft_round"), 2, "应经历一次修订循环")

        draft = result.state.artifact("draft")
        self.assertIsNotNone(draft)
        final = result.state.artifact("final_document")
        self.assertIsNotNone(final)
        self.assertIn("背景与问题", final.text())
        self.assertGreater(len(final.text()), 400)

        # 审校意见被写作 Agent 接收（协作闭环）
        feedback = [m for m in result.state.bus.all() if m.kind == "feedback"]
        self.assertTrue(feedback)
        drafts = [r for r in result.state.node_runs if r.node_id == "draft"]
        self.assertGreaterEqual(len(drafts), 2)

    def test_missing_topic_fails_fast(self):
        spec = load_workflow("content_creation", registry=default_registry)
        engine = Engine(default_registry, run_root=self.tmp, echo=False)
        result = engine.run(spec, {"topic": ""})
        self.assertEqual(result.status, "failed")
        self.assertIn("topic", result.error or "")


# ---------------------------------------------------------------------------
# 可视化
# ---------------------------------------------------------------------------
class TestVisualization(unittest.TestCase):
    def test_layout_and_svg(self):
        for workflow_id in ("code_review", "content_creation"):
            spec = load_workflow(workflow_id)
            layout = compute_layout(spec, "TB")
            self.assertEqual(set(layout.boxes), set(spec.node_ids))
            self.assertGreater(layout.layers, 1)
            # 同一层不重叠
            boxes = sorted(layout.boxes.values(), key=lambda b: (b.layer, b.x))
            for left, right in zip(boxes, boxes[1:]):
                if left.layer == right.layer:
                    self.assertLess(left.right, right.x)
            svg = render_graph_svg(spec, layout, {nid: {"status": "pending"} for nid in spec.node_ids})
            self.assertIn("<svg", svg)
            self.assertTrue(svg.rstrip().endswith("</svg>"))
            for node in spec.nodes:
                self.assertIn(node.agent, svg)

    def test_report_builder_smoke(self):
        spec = load_workflow("code_review")
        from agentflow.core.state import SharedState

        state = SharedState(run_id="demo-run", workflow_id=spec.id, workflow_name=spec.name,
                            inputs={"task": "demo"})
        state.put("code", "def f():\n    return 1\n", author="coder")
        html = build_report(spec=spec, state=state, events=[], summary={"status": "passed",
                                                                       "duration_s": 0.1,
                                                                       "stats": {}})
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn(spec.name, html)


if __name__ == "__main__":
    unittest.main()
