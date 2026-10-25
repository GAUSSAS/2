"""命令行入口：``python -m agentflow <命令>``。

命令一览::

    list                        列出内置工作流、已注册 Agent 与审查规则
    validate  --workflow ID     静态校验工作流定义
    run       --workflow ID     执行工作流（支持 --input key=value、故障注入、JSON 输出）
    resume    --run-id ID       从断点恢复运行
    visualize --workflow ID     导出流程图 SVG
    serve                       启动 Web 控制台（可视化 + 一键运行）
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path
from typing import Any, Sequence

from .core.agent import default_registry, load_plugin_agents
from .core.engine import Engine
from .core.graph import list_workflows, load_workflow
from .core.report import render_graph_svg
from .core.layout import compute_layout
from .core.trace import paint


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentflow",
        description="AgentFlow —— 多 Agent 工作流编排引擎（编码 → 审查 → 修复 等流程）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：python -m agentflow run --workflow code_review --input task=\"实现用户档案服务\"",
    )
    parser.add_argument("--run-root", default="runs", help="运行产物目录（默认 runs/）")
    parser.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出工作流 / Agent / 审查规则")

    validate = sub.add_parser("validate", help="静态校验工作流")
    validate.add_argument("--workflow", required=True, help="工作流 id 或 JSON 路径")

    run = sub.add_parser("run", help="执行工作流")
    run.add_argument("--workflow", required=True, help="工作流 id 或 JSON 路径")
    run.add_argument("--input", action="append", default=[], metavar="KEY=VALUE",
                     help="运行入参，可重复；VALUE 会尝试按 JSON 解析")
    run.add_argument("--target", metavar="PATH",
                     help="直接审查指定的源码文件（等价于 --input path=PATH --input mode=file）")
    run.add_argument("--parallelism", type=int, default=None, help="并行度（覆盖工作流设置）")
    run.add_argument("--fault", action="append", default=[], metavar="NODE[:N]",
                     help="故障注入（演示重试），如 --fault coder:1")
    run.add_argument("--json", action="store_true", help="以 JSON 输出结果摘要")
    run.add_argument("--quiet", action="store_true", help="不打印执行过程")
    run.add_argument("--no-report", action="store_true", help="不生成 HTML 报告")
    run.add_argument("--open", action="store_true", help="结束后用浏览器打开报告")
    run.add_argument("--run-id", default=None, help="指定运行 id（默认自动生成）")

    resume = sub.add_parser("resume", help="从断点恢复运行")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--json", action="store_true")
    resume.add_argument("--quiet", action="store_true", help="不打印执行过程")
    resume.add_argument("--open", action="store_true")

    visualize = sub.add_parser("visualize", help="导出流程图 SVG")
    visualize.add_argument("--workflow", required=True)
    visualize.add_argument("--out", default=None, help="输出路径（默认 docs/<id>.svg）")
    visualize.add_argument("--html", action="store_true", help="导出 HTML 而不是 SVG")

    serve = sub.add_parser("serve", help="启动 Web 控制台")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7860)
    serve.add_argument("--no-browser", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_plugin_agents()
    run_root = Path(args.run_root)

    if args.command == "list":
        return _cmd_list()
    if args.command == "validate":
        return _cmd_validate(args, run_root)
    if args.command == "run":
        return _cmd_run(args, run_root)
    if args.command == "resume":
        return _cmd_resume(args, run_root)
    if args.command == "visualize":
        return _cmd_visualize(args)
    if args.command == "serve":
        return _cmd_serve(args, run_root)
    return 2


# ---------------------------------------------------------------------------
# 各子命令
# ---------------------------------------------------------------------------
def _cmd_list() -> int:
    workflows = list_workflows()
    print(paint("工作流", "bold"))
    for row in workflows:
        print(f"  · {paint(row['id'], 'cyan')}  {row['name']}")
        print(f"      {row['node_count']} 节点 / {row['edge_count']} 边 · Agent: {', '.join(row['agents'])}")
        if row["description"]:
            print(f"      {paint(row['description'][:100], 'gray')}")
    print()
    print(paint("已注册 Agent", "bold"))
    for agent in default_registry.describe():
        print(f"  · {paint(agent['name'], 'green'):<20} {agent['description']}")
        print(f"      输入: {', '.join(agent['inputs']) or '-'} → 输出: {', '.join(agent['outputs'])}")
    print()
    from .agents.linting import rule_catalog

    rules = rule_catalog()
    print(paint(f"审查规则（{len(rules)} 条，{sum(1 for r in rules if r['fixable'])} 条可自动修复）", "bold"))
    for rule in rules:
        print(f"  · {rule['category']:<8} {rule['severity']:<8} {rule['rule_id']:<26} {rule['message']}")
    return 0


def _cmd_validate(args, run_root: Path) -> int:
    try:
        spec = load_workflow(args.workflow, registry=default_registry)
    except Exception as exc:  # noqa: BLE001
        print(paint(f"✖ 校验失败: {exc}", "red"))
        return 1
    print(paint(f"✔ 工作流 {spec.id} 校验通过", "green"))
    print(f"  节点 {len(spec.nodes)} / 边 {len(spec.edges)} / 入口 {spec.start}")
    reachable = spec.reachable()
    unreachable = [n for n in spec.node_ids if n not in reachable]
    if unreachable:
        print(paint(f"  ⚠ 不可达节点: {unreachable}", "yellow"))
    for warning in spec.settings.get("_warnings", []):
        print(paint(f"  ⚠ {warning}", "yellow"))
    return 0


def _cmd_run(args, run_root: Path) -> int:
    spec = load_workflow(args.workflow, registry=default_registry)
    inputs = _parse_inputs(args.input)
    if getattr(args, "target", None):
        # 便捷入口：--target 等价于 path + mode=file（显式 --input 优先）
        inputs.setdefault("path", args.target)
        inputs.setdefault("mode", "file")
    faults = _parse_faults(args.fault)
    engine = Engine(
        default_registry,
        run_root=run_root,
        parallelism=args.parallelism or int(spec.settings.get("parallelism", 4)),
        max_steps=int(spec.settings.get("max_steps", 200)),
        echo=not args.quiet and not args.json,
        fault_injection=faults,
    )
    result = engine.run(spec, inputs, run_id=args.run_id)
    if args.no_report:
        report = result.run_dir / "report.html"
        if report.exists():
            report.unlink()
    return _finish(result, args)


def _cmd_resume(args, run_root: Path) -> int:
    engine = Engine(default_registry, run_root=run_root, echo=not getattr(args, "quiet", False) and not args.json)
    result = engine.resume(args.run_id)
    return _finish(result, args)


def _finish(result, args) -> int:
    if args.json:
        payload = {
            "run_id": result.run_id,
            "workflow": result.workflow_id,
            "status": result.status,
            "duration_s": result.duration_s,
            "error": result.error,
            "run_dir": str(result.run_dir),
            "report": str(result.report_path),
            "summary": result.summary,
            "variables": result.state.variables,
            "artifacts": {name: art.version for name, art in result.state.artifacts.items()},
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        _print_banner(result)
    if getattr(args, "open", False) and result.report_path.exists():
        webbrowser.open(result.report_path.resolve().as_uri())
    return 0 if result.status == "passed" else (1 if result.status == "failed" else 0)


def _print_banner(result) -> None:
    color = {"passed": "green", "partial": "yellow", "failed": "red"}.get(result.status, "gray")
    print()
    print(paint("─" * 64, "gray"))
    print(f"  运行 {paint(result.run_id, 'bold')}")
    print(f"  状态 {paint(result.status.upper(), color)}   用时 {result.duration_s}s   "
          f"节点执行 {len(result.state.node_runs)} 次")
    review = result.state.artifact("review")
    if review and isinstance(review.content, dict):
        content = review.content
        print(f"  质量 {content.get('score', '-')} 分   审查 {content.get('total', '-')} 项   "
              f"阻塞 {content.get('error', 0)} / 警告 {content.get('warning', 0)} / 提示 {content.get('info', 0)}")
    delivery = result.state.artifact("delivery")
    if delivery and isinstance(delivery.content, dict):
        print(f"  交付 {delivery.content.get('filename', '-')} · {delivery.content.get('summary', '')}")
    final_doc = result.state.artifact("final_document")
    if final_doc is not None:
        print(f"  终稿 {final_doc.meta.get('filename', 'final_document.md')}（{len(final_doc.text())} 字符）")
    print(f"  产物 {result.run_dir}")
    if result.report_path.exists():
        print(f"  报告 {result.report_path}")
    print(paint("─" * 64, "gray"))


def _cmd_visualize(args) -> int:
    spec = load_workflow(args.workflow, registry=default_registry)
    layout = compute_layout(spec, "TB")
    # 文档配图：按节点类型着色，第三行显示 kind · id（而不是运行状态）
    if args.html:
        from .core.report import CSS, _legend

        svg = render_graph_svg(spec, layout, {}, show_status=False)
        out = Path(args.out or f"docs/{spec.id}.html")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<title>{spec.name}</title><style>{CSS}</style></head><body><div class=\"wrap\">"
            f"<h1>{spec.name}</h1><div class=\"card\">{svg}{_legend()}</div></div></body></html>",
            encoding="utf-8",
        )
    else:
        out = Path(args.out or f"docs/{spec.id}.svg")
        out.parent.mkdir(parents=True, exist_ok=True)
        svg = render_graph_svg(spec, layout, {}, show_conditions=True, show_status=False)
        out.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            + svg.replace("<svg ", '<svg style="background:#0b1120" ', 1),
            encoding="utf-8",
        )
    print(paint(f"✔ 已导出 {out}", "green"))
    return 0


def _cmd_serve(args, run_root: Path) -> int:
    from .web.server import serve

    url = f"http://{args.host}:{args.port}"
    print(paint(f"AgentFlow 控制台: {url}", "bold"))
    print(paint("  按 Ctrl+C 停止", "gray"))
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    serve(host=args.host, port=args.port, run_root=run_root)
    return 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def _parse_inputs(pairs: Sequence[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--input 需要 KEY=VALUE 形式，收到: {pair!r}")
        key, raw = pair.split("=", 1)
        try:
            value: Any = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        result[key.strip()] = value
    return result


def _parse_faults(pairs: Sequence[str]) -> dict[str, int]:
    faults: dict[str, int] = {}
    for pair in pairs:
        node, _, count = pair.partition(":")
        try:
            faults[node.strip()] = int(count or 1)
        except ValueError:
            raise SystemExit(f"--fault 需要 NODE[:N] 形式，收到: {pair!r}")
    return faults


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
