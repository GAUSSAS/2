"""核心引擎单元测试：表达式、图校验、顺序/条件/并行/循环/重试/跳过/断点恢复。

用 ``unittest``（标准库），直接 ``python -m unittest discover tests`` 即可运行。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentflow.core.agent import AgentRegistry, AgentResult, BaseAgent
from agentflow.core.engine import Engine
from agentflow.core.errors import ExpressionError, GraphValidationError
from agentflow.core.expr import safe_eval
from agentflow.core.graph import (
    EdgeSpec,
    WorkflowSpec,
    load_workflow,
    resolve_ref,
    resolve_mapping,
)
from agentflow.core.state import SharedState


# ---------------------------------------------------------------------------
# 测试用 Agent
# ---------------------------------------------------------------------------
class Recorder(BaseAgent):
    name = "recorder"
    outputs = ("value",)

    def run(self, ctx, inputs):
        value = inputs.get("value", "ok")
        ctx.log(f"recorder 收到 {value}")
        ctx.send("downstream", "handoff", {"summary": f"value={value}"})
        return AgentResult(outputs={"value": value})


class Gate(BaseAgent):
    """根据输入决定 verdict 的门禁 Agent。"""

    name = "gate"
    outputs = ("verdict", "iteration")

    def run(self, ctx, inputs):
        iteration = ctx.incr("iteration")
        verdict = inputs.get("verdict") or "pass"
        ctx.set("verdict", verdict)
        return AgentResult(outputs={"verdict": verdict, "iteration": iteration})


class CountingGate(BaseAgent):
    """第 n 次执行时给出不同结论：用于测试“循环直到通过”。"""

    name = "counting_gate"
    outputs = ("verdict", "iteration")

    def run(self, ctx, inputs):
        iteration = ctx.incr("iteration")
        verdict = "pass" if iteration >= 2 else "fail"
        ctx.set("verdict", verdict)
        return AgentResult(outputs={"verdict": verdict, "iteration": iteration})


class Flaky(BaseAgent):
    """前 n 次调用抛可重试异常（类级别计数，便于测试）。"""

    name = "flaky"
    outputs = ("value",)
    attempts = 0
    fail_times = 1

    def run(self, ctx, inputs):
        type(self).attempts += 1
        if type(self).attempts <= type(self).fail_times:
            from agentflow.core.errors import AgentExecutionError

            raise AgentExecutionError(f"第 {type(self).attempts} 次故意失败")
        return AgentResult(outputs={"value": "finally-ok"})


class Boom(BaseAgent):
    name = "boom"
    outputs = ()

    def run(self, ctx, inputs):
        raise RuntimeError("不可恢复错误")


class Slow(BaseAgent):
    name = "slow"
    outputs = ("value",)

    def __init__(self, delay: float = 0.2, **config):
        super().__init__(**config)
        self.delay = delay

    def run(self, ctx, inputs):
        time.sleep(self.delay)
        return AgentResult(outputs={"value": f"slept-{self.delay}"})


def registry_with(*agents: BaseAgent) -> AgentRegistry:
    registry = AgentRegistry()
    for agent in agents:
        registry.add(agent)
    return registry


def spec_from(nodes, edges, **kwargs) -> WorkflowSpec:
    data = {
        "id": kwargs.get("id", "test_wf"),
        "name": "测试工作流",
        "start": kwargs.get("start", nodes[0]["id"]),
        "defaults": kwargs.get("defaults", {"timeout_s": 10}),
        "nodes": nodes,
        "edges": edges,
    }
    return WorkflowSpec.from_dict(data)


class TempRunMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def engine(self, registry, **kwargs) -> Engine:
        return Engine(registry, run_root=self.run_root, echo=False, **kwargs)


# ---------------------------------------------------------------------------
# 表达式
# ---------------------------------------------------------------------------
class TestExpressions(unittest.TestCase):
    def test_comparisons_and_booleans(self):
        ctx = {"verdict": "fail", "attempts": 2, "findings": [{}, {}]}
        self.assertTrue(safe_eval("verdict == 'fail' and attempts < 3", ctx))
        self.assertFalse(safe_eval("verdict == 'pass'", ctx))
        self.assertTrue(safe_eval("len(findings) > 0", ctx))
        self.assertTrue(safe_eval("verdict in ('fail', 'blocked')", ctx))
        self.assertTrue(safe_eval("not verdict == 'pass'", ctx))
        self.assertTrue(safe_eval("'fail' if attempts == 2 else 'ok'", ctx))

    def test_dotted_paths(self):
        ctx = {"inputs.max_rounds": 3, "nodes.aggregate.iteration": 2, "artifacts.code.version": 2}
        self.assertTrue(safe_eval("nodes.aggregate.iteration >= inputs.max_rounds - 1", ctx))
        self.assertTrue(safe_eval("artifacts.code.version > 0", ctx))

    def test_unknown_variable_raises(self):
        with self.assertRaises(ExpressionError):
            safe_eval("missing_var > 0", {})

    def test_rejects_arbitrary_code(self):
        for expression in (
            "__import__('os').system('echo hi')",
            "open('/etc/passwd').read()",
            "(lambda: 1)()",
            "[x for x in range(3)]",
            "'.'.join(['a','b'])",
        ):
            with self.assertRaises(ExpressionError, msg=expression):
                safe_eval(expression, {})

    def test_syntax_error(self):
        with self.assertRaises(ExpressionError):
            safe_eval("verdict ==", {})

    def test_empty_expression_is_true(self):
        self.assertTrue(safe_eval("", {}))


# ---------------------------------------------------------------------------
# 图与引用
# ---------------------------------------------------------------------------
class TestGraph(unittest.TestCase):
    def test_builtin_workflows_load_and_validate(self):
        for workflow_id in ("code_review", "content_creation"):
            spec = load_workflow(workflow_id)
            self.assertTrue(spec.nodes)
            self.assertIn(spec.start, spec.node_ids)
            reachable = spec.reachable()
            self.assertEqual(set(spec.node_ids), reachable, f"{workflow_id} 存在不可达节点")

    def test_invalid_edge_target_rejected(self):
        spec = spec_from(
            [{"id": "a", "agent": "recorder"}],
            [{"from": "a", "to": "missing"}],
        )
        with self.assertRaises(GraphValidationError):
            spec.validate(registry_with(Recorder()))

    def test_unknown_agent_rejected(self):
        spec = spec_from([{"id": "a", "agent": "nope"}], [])
        with self.assertRaises(GraphValidationError):
            spec.validate(registry_with(Recorder()))

    def test_bad_expression_rejected(self):
        spec = spec_from(
            [{"id": "a", "agent": "recorder"}, {"id": "b", "agent": "recorder"}],
            [{"from": "a", "to": "b", "when": "verdict =="}],
        )
        with self.assertRaises(GraphValidationError):
            spec.validate(registry_with(Recorder()))

    def test_resolve_ref_variants(self):
        state = SharedState(run_id="r1", inputs={"topic": "x"})
        state.set("verdict", "pass")
        state.put("code", "print(1)")
        state.incr("iteration")
        self.assertEqual(resolve_ref("inputs.topic", state), "x")
        self.assertEqual(resolve_ref("vars.verdict", state), "pass")
        self.assertEqual(resolve_ref("verdict", state), "pass")          # 裸名回退
        self.assertEqual(resolve_ref("artifacts.code", state), "print(1)")
        self.assertEqual(resolve_ref("counters.iteration", state), 1)
        self.assertEqual(resolve_ref("literal:abc", state), "abc")

    def test_optional_input_default(self):
        state = SharedState(run_id="r2", inputs={})
        resolved = resolve_mapping({"notes": {"ref": "nodes.edit.notes", "default": []}}, state,
                                   node_id="draft")
        self.assertEqual(resolved["notes"], [])


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------
class TestEngine(TempRunMixin, unittest.TestCase):
    def test_sequential_and_state_sharing(self):
        registry = registry_with(Recorder())
        spec = spec_from(
            [
                {"id": "first", "agent": "recorder", "inputs": {"value": "literal:hello"},
                 "outputs": ["value"]},
                {"id": "second", "agent": "recorder",
                 "inputs": {"value": "nodes.first.value"}, "outputs": ["value"]},
            ],
            [{"from": "first", "to": "second"}],
        )
        result = self.engine(registry).run(spec, {})
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.state.last_outputs()["second"]["value"], "hello")
        # 消息总线记录了 Agent 之间的通信
        self.assertGreaterEqual(len(result.state.bus), 2)
        self.assertTrue(result.state.bus.between("first", "downstream"))

    def test_conditional_branch(self):
        registry = registry_with(Gate(), Recorder())
        spec = spec_from(
            [
                {"id": "gate", "agent": "gate", "params": {"verdict": "pass"},
                 "outputs": ["verdict", "iteration"]},
                {"id": "happy", "agent": "recorder", "params": {"value": "happy"}},
                {"id": "sad", "agent": "recorder", "params": {"value": "sad"}},
            ],
            [
                {"from": "gate", "to": "happy", "when": "verdict == 'pass'"},
                {"from": "gate", "to": "sad", "when": "verdict == 'fail'"},
            ],
        )
        result = self.engine(registry).run(spec, {})
        self.assertEqual(result.status, "passed")
        touched = [run.node_id for run in result.state.node_runs]
        self.assertIn("happy", touched)
        self.assertNotIn("sad", touched)

    def test_parallel_fanout_and_join(self):
        registry = registry_with(Slow(0.25), Gate())
        spec = spec_from(
            [
                {"id": "start", "agent": "slow", "outputs": ["value"]},
                {"id": "left", "agent": "slow", "outputs": ["value"]},
                {"id": "right", "agent": "slow", "outputs": ["value"]},
                {"id": "join", "agent": "gate", "needs": ["left", "right"],
                 "outputs": ["verdict", "iteration"]},
            ],
            [
                {"from": "start", "to": "left"},
                {"from": "start", "to": "right"},
                {"from": "left", "to": "join"},
                {"from": "right", "to": "join"},
            ],
        )
        started = time.time()
        result = self.engine(registry, parallelism=4).run(spec, {})
        elapsed = time.time() - started
        self.assertEqual(result.status, "passed")
        self.assertLess(elapsed, 0.75, f"并行未生效（耗时 {elapsed:.2f}s）")
        # join 只执行一次
        self.assertEqual(len([r for r in result.state.node_runs if r.node_id == "join"]), 1)

    def test_loop_runs_until_pass(self):
        registry = registry_with(CountingGate(), Recorder())
        spec = spec_from(
            [
                {"id": "gate", "agent": "counting_gate",
                 "outputs": ["verdict", "iteration"], "max_visits": 6},
                {"id": "fix", "agent": "recorder", "params": {"value": "fix"}, "outputs": []},
                {"id": "done", "agent": "recorder", "params": {"value": "done"}, "outputs": []},
            ],
            [
                {"from": "gate", "to": "fix", "when": "verdict == 'fail'"},
                {"from": "gate", "to": "done", "when": "verdict == 'pass'"},
                {"from": "fix", "to": "gate", "label": "复审"},
            ],
        )
        result = self.engine(registry).run(spec, {})
        self.assertEqual(result.status, "passed")
        gate_runs = [r for r in result.state.node_runs if r.node_id == "gate"]
        self.assertEqual(len(gate_runs), 2, "应经历一次循环回到 gate")
        self.assertEqual(len([r for r in result.state.node_runs if r.node_id == "done"]), 1)

    def test_retry_on_retryable_error(self):
        Flaky.attempts = 0
        Flaky.fail_times = 1
        registry = registry_with(Flaky())
        spec = spec_from(
            [{"id": "a", "agent": "flaky", "outputs": ["value"],
              "retry": {"max_attempts": 3, "backoff_ms": 1}}],
            [],
        )
        result = self.engine(registry).run(spec, {})
        self.assertEqual(result.status, "passed")
        self.assertEqual(Flaky.attempts, 2)
        self.assertEqual(len([e for e in result.tracer.events if e["type"] == "retry"]), 1)

    def test_skip_on_error_uses_skip_edge(self):
        registry = registry_with(Boom(), Recorder())
        spec = spec_from(
            [
                {"id": "risky", "agent": "boom", "on_error": "skip"},
                {"id": "fallback", "agent": "recorder", "params": {"value": "fallback"}},
            ],
            [
                {"from": "risky", "to": "fallback", "on": "skip", "label": "降级"},
            ],
        )
        result = self.engine(registry).run(spec, {})
        self.assertEqual(result.status, "passed")
        statuses = {r.node_id: r.status for r in result.state.node_runs}
        self.assertEqual(statuses["risky"], "skipped")
        self.assertEqual(statuses["fallback"], "ok")

    def test_fatal_error_marks_run_failed(self):
        registry = registry_with(Boom())
        spec = spec_from([{"id": "a", "agent": "boom"}], [])
        result = self.engine(registry).run(spec, {})
        self.assertEqual(result.status, "failed")
        self.assertIn("不可恢复错误", result.error or "")
        self.assertTrue((result.run_dir / "summary.json").exists())

    def test_max_visits_guard(self):
        registry = registry_with(Gate())
        spec = spec_from(
            [
                {"id": "loop", "agent": "gate", "outputs": ["verdict", "iteration"],
                 "max_visits": 2},
            ],
            [{"from": "loop", "to": "loop", "when": "iteration < 10"}],
        )
        result = self.engine(registry).run(spec, {})
        self.assertEqual(result.status, "failed")
        self.assertIn("max_visits", (result.error or ""))

    def test_output_contract_validation(self):
        registry = registry_with(Recorder())
        spec = spec_from(
            [{"id": "a", "agent": "recorder", "outputs": ["missing_field"],
              "retry": {"max_attempts": 2, "backoff_ms": 1}}],
            [],
        )
        result = self.engine(registry).run(spec, {})
        self.assertEqual(result.status, "failed")
        self.assertIn("missing_field", result.error or "")

    def test_fault_injection_triggers_retry(self):
        registry = registry_with(Recorder())
        spec = spec_from(
            [{"id": "a", "agent": "recorder", "outputs": ["value"],
              "retry": {"max_attempts": 2, "backoff_ms": 1}}],
            [],
        )
        result = self.engine(registry, fault_injection={"a": 1}).run(spec, {})
        self.assertEqual(result.status, "passed")
        self.assertEqual(len([e for e in result.tracer.events if e["type"] == "retry"]), 1)

    def test_checkpoint_and_resume(self):
        registry = registry_with(Recorder())
        spec = spec_from(
            [
                {"id": "a", "agent": "recorder", "params": {"value": "one"}, "outputs": ["value"]},
                {"id": "b", "agent": "recorder", "params": {"value": "two"}, "outputs": ["value"],
                 "retry": {"max_attempts": 1}},
            ],
            [{"from": "a", "to": "b"}],
        )
        engine = self.engine(registry, fault_injection={"b": 1})
        failed = engine.run(spec, {})
        self.assertEqual(failed.status, "failed")
        checkpoint = failed.run_dir / "checkpoint.json"
        self.assertTrue(checkpoint.exists())

        resumed = self.engine(registry).resume(failed.run_id)
        self.assertEqual(resumed.status, "passed")
        self.assertEqual(resumed.state.last_outputs()["b"]["value"], "two")

    def test_run_artifacts_and_report(self):
        registry = registry_with(Recorder())
        spec = spec_from([{"id": "a", "agent": "recorder", "outputs": ["value"]}], [])
        result = self.engine(registry).run(spec, {})
        self.assertTrue((result.run_dir / "report.html").exists())
        summary = json.loads((result.run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "passed")
        self.assertTrue((result.run_dir / "events.jsonl").exists())
        self.assertTrue(result.report_path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>"))

    def test_list_runs(self):
        registry = registry_with(Recorder())
        spec = spec_from([{"id": "a", "agent": "recorder", "outputs": ["value"]}], [])
        engine = self.engine(registry)
        engine.run(spec, {})
        engine.run(spec, {})
        self.assertEqual(len(engine.list_runs()), 2)


if __name__ == "__main__":
    unittest.main()
