"""调度引擎：顺序 / 条件分支 / 循环 / 并行 / 重试 / 断点恢复。

调度模型（一句话）
------------------
**波次 (wave) 调度**：把当前所有“已激活且依赖满足”的节点收集成一个波次，
同一波次内并行执行（线程池），波次之间是一道栅栏；每个节点执行完就根据
``when`` 条件激活后继节点，回到等待队列。

这样可以用很小的代码量覆盖课程要求的所有流程控制：

=========================  ==================================================
顺序执行                    ``A -> B``：A 完成后激活 B
条件分支                    ``when`` 表达式决定边是否触发
并行 fan-out / join        一个节点触发多条边 → 同一波次并行；join 节点用
                            ``needs`` 声明前置，等所有分支完成才执行
循环                       回边 + 节点 ``max_visits`` 死循环保护
重试                       节点 ``retry``（指数退避），只对可重试异常生效
错误隔离                   节点 ``on_error: skip`` + ``on="skip"`` 边
断点恢复                   每波次写 checkpoint，``resume`` 从断点继续
=========================  ==================================================

状态追踪
--------
引擎本身**几乎不持有业务状态**，所有数据都在 ``SharedState`` 里；
引擎只维护调度器自己的簿记（``_Runtime``：节点代数、已消费的依赖代数、
访问次数、待激活队列），并把它和状态一起写进断点。
"""

from __future__ import annotations

import dataclasses
import json
import random
import string
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .agent import AgentContext, AgentRegistry, AgentResult, default_registry
from .errors import (
    AgentExecutionError,
    AgentFlowError,
    DeadlockError,
    ExpressionError,
    FlowControlError,
    GraphValidationError,
    OutputValidationError,
)
from .expr import safe_eval
from .graph import EdgeSpec, InputResolutionError, NodeSpec, WorkflowSpec, resolve_mapping
from .state import NodeRun, SharedState
from .trace import Tracer


class _NodeFailure(AgentFlowError):
    """内部信号：不可恢复的节点失败，需要终止整个运行。"""


@dataclass
class _Outcome:
    node: NodeSpec
    status: str  # ok | failed | skipped
    error: str | None = None
    result: AgentResult | None = None


@dataclass
class _Runtime:
    """调度器簿记（会被写进断点）。"""

    #: 每个节点已完成的次数（“代数”），用于 join 判定
    gen: dict[str, int] = field(default_factory=dict)
    #: 每个节点上次执行时，其 needs 的代数快照
    seen: dict[str, dict[str, int]] = field(default_factory=dict)
    #: 每个节点已执行的次数（max_visits 保护）
    visits: dict[str, int] = field(default_factory=dict)
    #: 待激活队列
    frontier: list[str] = field(default_factory=list)
    wave: int = 0
    step: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "_Runtime":
        return cls(
            gen=dict(data.get("gen", {})),
            seen={k: dict(v) for k, v in data.get("seen", {}).items()},
            visits=dict(data.get("visits", {})),
            frontier=list(data.get("frontier", [])),
            wave=int(data.get("wave", 0)),
            step=int(data.get("step", 0)),
        )


@dataclass
class RunResult:
    """一次运行的完整结果。"""

    run_id: str
    workflow_id: str
    workflow_name: str
    status: str  # passed | failed | partial
    state: SharedState
    run_dir: Path
    duration_s: float
    error: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    tracer: Tracer | None = None

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    @property
    def report_path(self) -> Path:
        return self.run_dir / "report.html"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "status": self.status,
            "duration_s": self.duration_s,
            "error": self.error,
            "run_dir": str(self.run_dir),
            "summary": self.summary,
        }


class Engine:
    """工作流执行引擎。

    Args:
        registry: Agent 注册表，默认使用全局注册表。
        run_root: 运行产物的根目录（每次运行一个子目录）。
        parallelism: 同一波次内允许的并发节点数。
        max_steps: 调度步数上限（防死循环的第二重保险）。
        echo: 是否把事件打印到终端。
        on_event: 事件回调（Web UI 实时推送用）。
        fault_injection: ``{node_id: 剩余注入次数}``，用于演示重试与错误恢复。
    """

    def __init__(
        self,
        registry: AgentRegistry | None = None,
        *,
        run_root: str | Path = "runs",
        parallelism: int = 4,
        max_steps: int = 200,
        echo: bool = True,
        on_event=None,
        fault_injection: Mapping[str, int] | None = None,
    ) -> None:
        self.registry = registry or default_registry
        self.run_root = Path(run_root)
        self.parallelism = max(1, parallelism)
        self.max_steps = max_steps
        self.echo = echo
        self.on_event = on_event
        self.fault_injection = dict(fault_injection or {})
        self._msg_lock = threading.Lock()
        self._emitted_messages: set[str] = set()

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    def run(
        self,
        spec: WorkflowSpec,
        inputs: Mapping[str, Any] | None = None,
        *,
        run_id: str | None = None,
        echo: bool | None = None,
    ) -> RunResult:
        """从头执行一个工作流。"""
        spec.validate(self.registry)
        run_id = run_id or _new_run_id(spec.id)
        run_dir = self.run_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "workflow.json").write_text(spec.to_json(), encoding="utf-8")

        resolved_inputs, unknown_inputs = self._prepare_inputs(spec, inputs)
        tracer = Tracer(run_id=run_id, echo=self.echo if echo is None else echo, on_event=self.on_event)
        if unknown_inputs:
            message = (
                "未知入参被忽略: "
                + ", ".join(unknown_inputs)
                + f"（该工作流可用入参: {', '.join(spec.inputs_schema) or '无'}）"
            )
            tracer.emit("warning", message=message)
            if not tracer.echo:
                # --quiet 时也不该让“传错参数被静默忽略”发生，始终提示一次
                import sys

                print(f"[agentflow] 警告: {message}", file=sys.stderr)
        state = SharedState(run_id=run_id, workflow_id=spec.id, workflow_name=spec.name, inputs=resolved_inputs)
        runtime = _Runtime(frontier=[spec.start])
        return self._drive(spec, state, runtime, tracer, run_dir, begun_at=time.time())

    def resume(self, run_id: str, *, echo: bool | None = None) -> RunResult:
        """从断点恢复一个被中断/失败的运行。"""
        run_dir = self.run_root / run_id
        state_path = run_dir / "state.json"
        runtime_path = run_dir / "checkpoint.json"
        if not state_path.exists() or not runtime_path.exists():
            raise GraphValidationError(f"运行 {run_id!r} 没有可用的断点（{run_dir}）")

        spec = WorkflowSpec.from_dict(json.loads((run_dir / "workflow.json").read_text(encoding="utf-8")))
        state = SharedState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
        checkpoint = json.loads(runtime_path.read_text(encoding="utf-8"))
        runtime = _Runtime.from_dict(checkpoint.get("runtime", {}))
        tracer = Tracer(run_id=run_id, echo=self.echo if echo is None else echo, on_event=self.on_event)
        events_path = run_dir / "events.jsonl"
        if events_path.exists():
            for line in events_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    tracer.events.append(json.loads(line))
        state.status = "running"
        state.error = None
        tracer.emit("run_resumed", stage=checkpoint.get("stage"), wave=runtime.wave)
        return self._drive(spec, state, runtime, tracer, run_dir, begun_at=state.created_at)

    def list_runs(self) -> list[dict[str, Any]]:
        """扫描 run_root 下所有运行摘要（新的在前）。"""
        rows: list[dict[str, Any]] = []
        if not self.run_root.exists():
            return rows
        for path in self.run_root.iterdir():
            summary_file = path / "summary.json"
            if path.is_dir() and summary_file.exists():
                try:
                    rows.append(json.loads(summary_file.read_text(encoding="utf-8")))
                except (json.JSONDecodeError, OSError):
                    continue
        rows.sort(key=lambda r: r.get("started_at", 0), reverse=True)
        return rows

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def _drive(
        self,
        spec: WorkflowSpec,
        state: SharedState,
        runtime: _Runtime,
        tracer: Tracer,
        run_dir: Path,
        *,
        begun_at: float,
    ) -> RunResult:
        status = "passed"
        error: str | None = None
        tracer.emit(
            "run_start",
            run_id=state.run_id,
            workflow=spec.id,
            workflow_name=spec.name,
            node_count=len(spec.nodes),
            edge_count=len(spec.edges),
            inputs=state.inputs,
            resumed=runtime.step > 0,
        )
        try:
            self._loop(spec, state, runtime, tracer, run_dir)
        except _NodeFailure as exc:
            status, error = "failed", str(exc)
        except DeadlockError as exc:
            status, error = "failed", f"调度死锁: {exc}"
        except FlowControlError as exc:
            status, error = "failed", f"流程控制异常: {exc}"
        except AgentFlowError as exc:
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - 兜底，保证报告仍会生成
            status, error = "failed", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        else:
            if state.get("escalated"):
                status = "partial"

        self._flush_messages(state, tracer)
        duration = round(time.time() - begun_at, 3)
        state.status = status
        state.error = error
        if error:
            tracer.emit("error", message=error)

        summary = self._write_outputs(spec, state, runtime, tracer, run_dir, status, error, duration)
        tracer.emit("run_end", status=status, duration_s=duration, error=error, **tracer.stats())
        self._write_run_files(spec, state, runtime, tracer, run_dir, summary)

        return RunResult(
            run_id=state.run_id,
            workflow_id=spec.id,
            workflow_name=spec.name,
            status=status,
            state=state,
            run_dir=run_dir,
            duration_s=duration,
            error=error,
            summary=summary,
            tracer=tracer,
        )

    def _loop(self, spec: WorkflowSpec, state: SharedState, runtime: _Runtime,
              tracer: Tracer, run_dir: Path) -> None:
        while runtime.frontier:
            runtime.step += 1
            state.counters["step"] = runtime.step
            if runtime.step > self.max_steps:
                raise FlowControlError(
                    f"超过最大调度步数 {self.max_steps}（疑似无限循环）。"
                    f"待执行节点: {runtime.frontier}"
                )

            ready = [nid for nid in runtime.frontier if self._deps_satisfied(spec, nid, runtime)]
            if not ready:
                waiting = {nid: list(spec.node(nid).needs) for nid in runtime.frontier}
                raise DeadlockError(f"节点等待的前置条件永远无法满足: {waiting}")

            runtime.wave += 1
            tracer.emit("wave_start", wave=runtime.wave, nodes=ready)
            runtime.frontier = [nid for nid in runtime.frontier if nid not in ready]

            for nid in ready:
                node = spec.node(nid)
                if node.max_visits is not None and runtime.visits.get(nid, 0) >= node.max_visits:
                    raise FlowControlError(
                        f"节点 {nid!r} 已执行 {runtime.visits.get(nid, 0)} 次，"
                        f"达到 max_visits={node.max_visits} 上限"
                    )

            outcomes = self._run_wave(spec, state, [spec.node(n) for n in ready], runtime, tracer)

            fatal: _Outcome | None = None
            for outcome in outcomes:
                node = outcome.node
                runtime.gen[node.id] = runtime.gen.get(node.id, 0) + 1
                runtime.visits[node.id] = runtime.visits.get(node.id, 0) + 1
                runtime.seen[node.id] = {need: runtime.gen.get(need, 0) for need in node.needs}
                self._fire_edges(spec, state, runtime, tracer, node, outcome.status)
                if outcome.status == "failed":
                    # 失败的节点放回队列并把记账回滚一格，这样 resume 能从它继续重试
                    runtime.gen[node.id] = max(0, runtime.gen[node.id] - 1)
                    runtime.visits[node.id] = max(0, runtime.visits[node.id] - 1)
                    if node.id not in runtime.frontier:
                        runtime.frontier.insert(0, node.id)
                    fatal = outcome

            self._checkpoint(spec, state, runtime, tracer, run_dir)
            if fatal is not None:
                raise _NodeFailure(
                    f"节点 {fatal.node.id!r} 执行失败且 on_error=fail: {fatal.error}"
                )

    # ------------------------------------------------------------------
    # 依赖 / 边
    # ------------------------------------------------------------------
    def _deps_satisfied(self, spec: WorkflowSpec, node_id: str, runtime: _Runtime) -> bool:
        node = spec.node(node_id)
        if not node.needs:
            return True
        consumed = runtime.seen.get(node_id, {})
        for need in node.needs:
            if runtime.gen.get(need, 0) <= consumed.get(need, 0):
                return False
        return True

    def _fire_edges(self, spec: WorkflowSpec, state: SharedState, runtime: _Runtime,
                    tracer: Tracer, node: NodeSpec, status: str) -> None:
        edge_kind = "success" if status == "ok" else "skip"
        context = state.expr_context()
        for edge in spec.outgoing(node.id, on=edge_kind):
            fired, reason = self._edge_decision(edge, context)
            tracer.emit(
                "edge",
                source=edge.source,
                target=edge.target,
                label=edge.label,
                when=edge.when,
                fired=fired,
                reason=reason,
            )
            if not fired:
                continue
            target = spec.node(edge.target)
            if target.max_visits is not None and runtime.visits.get(target.id, 0) >= target.max_visits:
                raise FlowControlError(
                    f"边 {edge.source}->{edge.target} 试图再次激活节点 {target.id!r}，"
                    f"但已达 max_visits={target.max_visits}"
                )
            if edge.target not in runtime.frontier:
                runtime.frontier.append(edge.target)
            if edge.exclusive:
                # 独占分支：后面的边不再评估（按声明顺序即优先级）
                tracer.emit("edge_exclusive", source=edge.source, target=edge.target)
                break

    def _edge_decision(self, edge: EdgeSpec, context: Mapping[str, Any]) -> tuple[bool, str]:
        if not edge.when:
            return True, "无条件"
        try:
            value = safe_eval(edge.when, context)
        except ExpressionError as exc:
            raise FlowControlError(f"边 {edge.source}->{edge.target} 条件求值失败: {exc}") from exc
        return bool(value), f"{edge.when} => {bool(value)}"

    # ------------------------------------------------------------------
    # 波次执行
    # ------------------------------------------------------------------
    def _run_wave(self, spec: WorkflowSpec, state: SharedState, nodes: list[NodeSpec],
                  runtime: _Runtime, tracer: Tracer) -> list[_Outcome]:
        if len(nodes) == 1 or self.parallelism == 1:
            return [self._run_node(spec, state, node, runtime, tracer) for node in nodes]

        outcomes: list[_Outcome] = []
        with ThreadPoolExecutor(max_workers=min(self.parallelism, len(nodes))) as pool:
            futures = {
                pool.submit(self._run_node, spec, state, node, runtime, tracer): node
                for node in nodes
            }
            for future in futures:
                outcomes.append(future.result())
        order = {node.id: i for i, node in enumerate(nodes)}
        outcomes.sort(key=lambda o: order.get(o.node.id, 0))
        return outcomes

    def _run_node(self, spec: WorkflowSpec, state: SharedState, node: NodeSpec,
                  runtime: _Runtime, tracer: Tracer) -> _Outcome:
        visit = runtime.visits.get(node.id, 0) + 1
        node_run = NodeRun(node_id=node.id, agent=node.agent, visit=visit, wave=runtime.wave)
        with state._lock:  # noqa: SLF001 - 引擎与状态是同一层，允许直接持锁写入
            state.node_runs.append(node_run)

        tracer.emit(
            "node_start",
            node_id=node.id,
            agent=node.agent,
            name=node.name,
            visit=visit,
            wave=runtime.wave,
            kind=node.kind,
        )
        started = time.time()
        try:
            inputs = {**node.params, **resolve_mapping(node.inputs, state, node_id=node.id)}
        except InputResolutionError as exc:
            node_run.status = "failed"
            node_run.error = str(exc)
            node_run.ended_at = time.time()
            tracer.emit("node_end", node_id=node.id, agent=node.agent, status="failed",
                        duration_ms=node_run.duration_ms, detail=str(exc), logs=[])
            return _Outcome(node=node, status="failed" if node.on_error == "fail" else "skipped",
                            error=str(exc))

        attempt = 0
        last_error: Exception | None = None
        while True:
            attempt += 1
            node_run.attempt = attempt
            try:
                if self._fault_due(node.id):
                    raise AgentExecutionError(
                        f"[故障注入] Agent {node.agent!r} 第 {attempt} 次执行故意失败"
                    )
                result = self._invoke(node, state, inputs, attempt, visit, runtime)
                missing = [key for key in node.outputs if key not in result.outputs]
                if missing:
                    raise OutputValidationError(
                        f"节点 {node.id!r} 的输出缺少 {missing}（声明为 {list(node.outputs)}）"
                    )
                # ---- 成功 ----
                node_run.status = "ok"
                node_run.outputs = dict(result.outputs)
                node_run.logs = list(result.logs)
                node_run.meta = dict(result.meta)
                node_run.ended_at = time.time()
                for name in result.artifacts:
                    if state.artifact(name) is None:
                        state.put(name, "", author=node.agent, note="agent 声明但未写入")
                self._flush_messages(state, tracer)
                tracer.emit(
                    "node_end",
                    node_id=node.id,
                    agent=node.agent,
                    status="ok",
                    duration_ms=node_run.duration_ms,
                    visit=visit,
                    attempt=attempt,
                    detail=self._detail(result.outputs),
                    outputs=_jsonable(result.outputs),
                    logs=node_run.logs,
                    meta=_jsonable(result.meta),
                )
                return _Outcome(node=node, status="ok", result=result)
            except Exception as exc:  # noqa: BLE001 - 分类处理
                last_error = exc
                retryable = isinstance(exc, AgentExecutionError) and _retry_allowed(node, exc)
                if retryable and attempt < node.retry.max_attempts:
                    delay = node.retry.delay_ms(attempt)
                    tracer.emit(
                        "retry",
                        node_id=node.id,
                        attempt=attempt,
                        max_attempts=node.retry.max_attempts,
                        delay_ms=delay,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    if delay:
                        time.sleep(delay / 1000.0)
                    continue

                status = "skipped" if node.on_error == "skip" else "failed"
                node_run.status = status
                node_run.error = f"{type(exc).__name__}: {exc}"
                node_run.ended_at = time.time()
                tracer.emit(
                    "node_end",
                    node_id=node.id,
                    agent=node.agent,
                    status=status,
                    duration_ms=node_run.duration_ms,
                    visit=visit,
                    attempt=attempt,
                    detail=node_run.error,
                    logs=[],
                    traceback=traceback.format_exc() if status == "failed" else "",
                )
                return _Outcome(node=node, status=status, error=node_run.error)

    def _invoke(self, node: NodeSpec, state: SharedState, inputs: Mapping[str, Any],
                attempt: int, visit: int, runtime: _Runtime) -> AgentResult:
        agent = self.registry.get(node.agent)
        ctx = AgentContext(
            run_id=state.run_id,
            workflow_id=state.workflow_id,
            node_id=node.id,
            agent_name=node.agent,
            state=state,
            inputs=inputs,
            attempt=attempt,
            visit=visit,
            settings=node.params,
        )
        if node.timeout_s:
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(agent.__call__, ctx, inputs)
            try:
                return future.result(timeout=node.timeout_s)
            except FutureTimeout as exc:
                raise AgentExecutionError(
                    f"节点 {node.id!r} 超时（{node.timeout_s}s）"
                ) from exc
            finally:
                pool.shutdown(wait=False)
        return agent(ctx, inputs)

    def _fault_due(self, node_id: str) -> bool:
        remaining = self.fault_injection.get(node_id, 0)
        if remaining > 0:
            self.fault_injection[node_id] = remaining - 1
            return True
        return False

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------
    def _checkpoint(self, spec: WorkflowSpec, state: SharedState, runtime: _Runtime,
                    tracer: Tracer, run_dir: Path) -> None:
        _atomic_write(run_dir / "state.json", json.dumps(state.to_dict(), ensure_ascii=False, indent=2))
        _atomic_write(
            run_dir / "checkpoint.json",
            json.dumps(
                {"stage": "wave", "wave": runtime.wave, "step": runtime.step,
                 "runtime": runtime.to_dict(), "saved_at": time.time()},
                ensure_ascii=False,
                indent=2,
            ),
        )
        tracer.emit("checkpoint", wave=runtime.wave, step=runtime.step,
                    frontier=list(runtime.frontier))

    def _write_outputs(self, spec: WorkflowSpec, state: SharedState, runtime: _Runtime,
                       tracer: Tracer, run_dir: Path, status: str, error: str | None,
                       duration: float) -> dict[str, Any]:
        artifacts_dir = run_dir / "artifacts"
        written: list[str] = []
        for name, artifact in state.artifacts.items():
            filename = artifact.meta.get("filename") or _default_filename(artifact.content, name)
            text = artifact.content if isinstance(artifact.content, str) else json.dumps(
                artifact.content, ensure_ascii=False, indent=2
            )
            if not isinstance(artifact.content, str):
                filename = filename if filename.endswith(".json") else f"{filename}.json"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            (artifacts_dir / filename).write_text(text, encoding="utf-8")
            written.append(f"artifacts/{filename}")
            if artifact.version > 1:
                versions_dir = artifacts_dir / "versions"
                versions_dir.mkdir(parents=True, exist_ok=True)
                for entry in artifact.history:
                    content = entry.get("content")
                    vtext = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2)
                    stem = Path(filename).stem
                    suffix = Path(filename).suffix
                    (versions_dir / f"{stem}.v{entry['version']}{suffix}").write_text(vtext, encoding="utf-8")

        summary = {
            "run_id": state.run_id,
            "workflow": spec.id,
            "workflow_name": spec.name,
            "status": status,
            "error": error,
            "duration_s": duration,
            "started_at": state.created_at,
            "finished_at": time.time(),
            "inputs": state.inputs,
            "variables": state.variables,
            "artifacts": {
                name: {"version": art.version, "author": art.author,
                       "length": len(art.content) if isinstance(art.content, (str, list, dict)) else None}
                for name, art in state.artifacts.items()
            },
            "nodes": [
                {
                    "node_id": run.node_id,
                    "agent": run.agent,
                    "status": run.status,
                    "visit": run.visit,
                    "attempt": run.attempt,
                    "duration_ms": run.duration_ms,
                    "wave": run.wave,
                    "error": run.error,
                }
                for run in state.node_runs
            ],
            "stats": tracer.stats(),
            "waves": runtime.wave,
            "steps": runtime.step,
            "outputs_written": written,
        }
        return summary

    def _write_run_files(self, spec: WorkflowSpec, state: SharedState, runtime: _Runtime,
                         tracer: Tracer, run_dir: Path, summary: dict[str, Any]) -> None:
        _atomic_write(run_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        _atomic_write(run_dir / "state.json", json.dumps(state.to_dict(), ensure_ascii=False, indent=2, default=str))
        tracer.write(run_dir / "events.jsonl")
        try:
            from .report import build_report

            html = build_report(spec=spec, state=state, events=tracer.events, summary=summary)
            _atomic_write(run_dir / "report.html", html)
        except Exception as exc:  # noqa: BLE001 - 报告失败不应影响运行结果
            tracer.emit("error", message=f"报告生成失败: {exc}")

    # ------------------------------------------------------------------
    def _prepare_inputs(self, spec: WorkflowSpec,
                        inputs: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
        """合并默认值；返回 (解析后的入参, 未知入参列表)。

        未知入参不会让运行失败，但会在事件流与报告里留下 `warning` ——
        避免“以为传了参数、其实被静默忽略”的坑。
        """
        resolved: dict[str, Any] = {}
        schema = spec.inputs_schema or {}
        for key, item in schema.items():
            if isinstance(item, Mapping) and "default" in item:
                resolved[key] = item["default"]
        provided = dict(inputs or {})
        resolved.update(provided)
        missing = [key for key, item in schema.items() if item.get("required") and key not in resolved]
        if missing:
            raise GraphValidationError(f"缺少必需入参: {', '.join(missing)}")
        unknown = [key for key in provided if schema and key not in schema]
        return resolved, unknown

    def _flush_messages(self, state: SharedState, tracer: Tracer) -> None:
        with self._msg_lock:
            for message in state.bus.all():
                if message.id in self._emitted_messages:
                    continue
                self._emitted_messages.add(message.id)
                tracer.emit(
                    "message",
                    message_id=message.id,
                    sender=message.sender,
                    recipient=message.recipient,
                    kind=message.kind,
                    summary=message.summary(),
                    payload=_jsonable(message.payload),
                )

    @staticmethod
    def _detail(outputs: Mapping[str, Any]) -> str:
        parts = []
        for key, value in list(outputs.items())[:4]:
            if isinstance(value, (list, tuple)):
                parts.append(f"{key}={len(value)}项")
            elif isinstance(value, str):
                text = value.replace("\n", " ")
                parts.append(f"{key}={text[:40]}{'…' if len(text) > 40 else ''}")
            else:
                parts.append(f"{key}={value}")
        return " ".join(parts)


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def _retry_allowed(node: NodeSpec, exc: Exception) -> bool:
    if not node.retry.enabled:
        return False
    names = node.retry.retry_on
    if "*" in names or not names:
        return True
    return any(name in (type(exc).__name__, type(exc).__mro__[1].__name__) for name in names)


def _new_run_id(workflow_id: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{workflow_id}-{stamp}-{suffix}"


def _default_filename(content: Any, name: str) -> str:
    if isinstance(content, str):
        stripped = content.lstrip()
        if stripped.startswith(("{", "[")):
            return f"{name}.json"
        if "def " in content or "import " in content or "class " in content:
            return f"{name}.py"
        if stripped.startswith("#"):
            return f"{name}.md"
        return f"{name}.txt"
    return f"{name}.json"


def _jsonable(value: Any) -> Any:
    """把任意对象转成可 JSON 序列化的结构（报告/日志用）。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return repr(value)


def _atomic_write(path: Path, text: str) -> None:
    """原子写：先写临时文件再替换，避免断点文件被读到一半。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
