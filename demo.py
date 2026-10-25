"""一键演示脚本：跑通两个工作流、导出流程图、打印汇总。

用法::

    python demo.py                 # 跑两个流程 + 导出 docs/ 下的流程图
    python demo.py --fault coder:1 # 演示故障注入与重试
    python demo.py --web           # 打开可视化控制台（浏览器）
    python demo.py --json          # 只输出机器可读结果

不需要任何第三方依赖，也不需要 API Key。
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agentflow import Engine, default_registry, load_workflow  # noqa: E402
from agentflow.core.layout import compute_layout  # noqa: E402
from agentflow.core.report import render_graph_svg  # noqa: E402

DEMOS = {
    "code_review": {
        "title": "① 代码审查流程（编码 → 并行审查 → 汇总 → 修复 → 复审）",
        "inputs": {"task": "实现用户档案服务的保存、按名称查询、标签解析与文本指纹能力",
                   "filename": "profile_service.py"},
    },
    "content_creation": {
        "title": "② 内容创作流程（主题 → 大纲 → 初稿 → 审校 → 修订 → 发布）",
        "inputs": {"topic": "多 Agent 工作流在企业内部的落地", "audience": "工程团队", "sections": 3},
    },
}


def export_diagrams(out_dir: Path) -> list[Path]:
    """把工作流导出为 SVG（docs 里的流程图，按节点类型着色）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for workflow_id in DEMOS:
        spec = load_workflow(workflow_id, registry=default_registry)
        layout = compute_layout(spec, "TB")
        svg = render_graph_svg(spec, layout, {}, show_status=False)
        target = out_dir / f"{workflow_id}.svg"
        target.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            + svg.replace("<svg ", '<svg style="background:#0b1120" ', 1),
            encoding="utf-8",
        )
        written.append(target)
    return written


def run_demo(workflow_id: str, *, fault: dict[str, int] | None = None) -> dict:
    spec = load_workflow(workflow_id, registry=default_registry)
    engine = Engine(
        default_registry,
        run_root=ROOT / "runs",
        parallelism=int(spec.settings.get("parallelism", 4)),
        echo=True,
        fault_injection=fault or {},
    )
    result = engine.run(spec, DEMOS[workflow_id]["inputs"])
    artifacts = {name: art.version for name, art in result.state.artifacts.items()}
    return {
        "run_id": result.run_id,
        "workflow": workflow_id,
        "status": result.status,
        "duration_s": result.duration_s,
        "rounds": result.state.counter("review_round") or result.state.counter("draft_round"),
        "artifacts": artifacts,
        "report": str(result.report_path),
        "run_dir": str(result.run_dir),
        "variables": result.state.variables,
        "error": result.error,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AgentFlow 一键演示")
    parser.add_argument("--workflow", choices=sorted(DEMOS), default=None, help="只跑指定的流程")
    parser.add_argument("--fault", default=None, metavar="NODE[:N]", help="故障注入，如 coder:1")
    parser.add_argument("--web", action="store_true", help="启动 Web 控制台")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--no-diagrams", action="store_true", help="不导出 docs 下的流程图")
    args = parser.parse_args(argv)

    if args.web:
        from agentflow.web.server import serve

        url = "http://127.0.0.1:7860"
        print(f"AgentFlow 控制台: {url}（Ctrl+C 停止）")
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        serve(run_root=ROOT / "runs")
        return 0

    faults: dict[str, int] = {}
    if args.fault:
        node, _, count = args.fault.partition(":")
        faults[node] = int(count or 1)

    workflows = [args.workflow] if args.workflow else list(DEMOS)
    results = []
    for workflow_id in workflows:
        print(f"\n{'=' * 78}\n{DEMOS[workflow_id]['title']}\n{'=' * 78}")
        results.append(run_demo(workflow_id, fault=faults))

    diagrams: list[Path] = []
    if not args.no_diagrams:
        diagrams = export_diagrams(ROOT / "docs" / "assets")

    if args.json:
        print(json.dumps({"runs": results, "diagrams": [str(p) for p in diagrams]},
                         ensure_ascii=False, indent=2, default=str))
        return 0

    print(f"\n{'=' * 78}\n汇总\n{'=' * 78}")
    for item in results:
        badge = {"passed": "✅", "partial": "⚠️ ", "failed": "❌"}.get(item["status"], "·")
        print(f"{badge} {item['workflow']:<18} {item['status']:<8} {item['duration_s']}s  "
              f"轮次 {item['rounds']}  作品 {len(item['artifacts'])} 个")
        print(f"   报告: {item['report']}")
    if diagrams:
        print("\n流程图已导出:")
        for path in diagrams:
            print(f"   {path}")
    print("\n可用命令: python -m agentflow list | run | resume | visualize | serve")
    return 0 if all(r["status"] == "passed" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
