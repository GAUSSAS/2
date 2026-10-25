"""工作流图的自动布局（分层有向图）。

给可视化用的极简布局算法，不依赖 graphviz：

1. 用 DFS 找出**回边**（指向当前递归栈上节点的边），这些边属于循环；
2. 去掉回边后图是有向无环图，用 Kahn 拓扑序做最长路径分层（``layer``）；
3. 同层节点均匀分布；回边单独走一侧的曲线。

支持两种方向：``TB``（自上而下，报告默认，链式流程更易读）与
``LR``（自左向右，适合层数少、分支多的图）。
输出的是纯坐标与尺寸，渲染交给 ``report.py`` 的 SVG 生成器。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping

from .graph import WorkflowSpec

Direction = Literal["TB", "LR"]

NODE_W = 190
NODE_H = 76
H_GAP = 34
V_GAP = 46
PAD_X = 28
PAD_Y = 28


@dataclass
class NodeBox:
    node_id: str
    layer: int = 0
    index: int = 0
    x: float = 0.0
    y: float = 0.0
    width: float = NODE_W
    height: float = NODE_H

    @property
    def cx(self) -> float:
        return self.x + self.width / 2

    @property
    def cy(self) -> float:
        return self.y + self.height / 2

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height


@dataclass
class Layout:
    boxes: dict[str, NodeBox]
    back_edges: set[tuple[str, str]] = field(default_factory=set)
    width: float = 0.0
    height: float = 0.0
    layers: int = 0
    direction: Direction = "TB"

    def box(self, node_id: str) -> NodeBox:
        return self.boxes[node_id]

    def edge_geometry(self, source: str, target: str) -> tuple[tuple[float, float], tuple[float, float], bool]:
        """返回 (起点, 终点, 是否回边)。回边绕行到图的右侧。"""
        a, b = self.boxes[source], self.boxes[target]
        is_back = (source, target) in self.back_edges or b.layer <= a.layer
        if is_back:
            return (a.right, a.cy), (b.right, b.cy), True
        if self.direction == "TB":
            return (a.cx, a.bottom), (b.cx, b.y), False
        return (a.right, a.cy), (b.x, b.cy), False


def _bfs_layers(spec: WorkflowSpec) -> dict[str, int]:
    """从入口做 BFS：首次发现即定层（只用来挑出“不前进”的边）。"""
    layer = {spec.start: 0}
    queue = [spec.start]
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in spec.node_ids}
    for edge in spec.edges:
        adjacency[edge.source].append(edge.target)
    while queue:
        node = queue.pop(0)
        for successor in adjacency.get(node, []):
            if successor not in layer:
                layer[successor] = layer[node] + 1
                queue.append(successor)
    for node_id in spec.node_ids:
        layer.setdefault(node_id, 0)
    return layer


def _longest_path_layers(spec: WorkflowSpec, forward: set[tuple[str, str]],
                         fallback: dict[str, int]) -> dict[str, int]:
    """在去掉回边的 DAG 上做最长路径分层（Kahn 拓扑序）。"""
    order = {node_id: index for index, node_id in enumerate(spec.node_ids)}
    indegree = {node_id: 0 for node_id in spec.node_ids}
    successors: dict[str, list[str]] = {node_id: [] for node_id in spec.node_ids}
    for edge in spec.edges:
        if (edge.source, edge.target) not in forward:
            continue
        successors[edge.source].append(edge.target)
        indegree[edge.target] += 1

    layer = dict(fallback)
    remaining = set(spec.node_ids)
    guard = 0
    while remaining:
        guard += 1
        if guard > len(spec.node_ids) + 2:
            break
        ready = sorted((n for n in remaining if indegree[n] <= 0), key=lambda n: order[n])
        if not ready:
            break  # 理论上不会发生（已去回边）
        for node in ready:
            remaining.discard(node)
            for successor in successors[node]:
                if successor in remaining:
                    layer[successor] = max(layer.get(successor, 0), layer[node] + 1)
                    indegree[successor] -= 1
    return layer


def compute_layers(spec: WorkflowSpec) -> dict[str, int]:
    """分层：BFS 初值 + “去回边后最长路径”迭代到不动点。

    难点是**图里有环**（复审回边、修订回边），而“哪些边是回边”又依赖分层，
    形成循环依赖。这里用不动点迭代解决：

    1. 先 BFS 得到初始分层（首次发现优先）；
    2. 把“不前进”的边（``layer[target] <= layer[source]``）当作回边去掉；
    3. 在剩下的 DAG 上做最长路径分层（Kahn）；
    4. 重复 2–3 直到分层稳定（最多 3 轮）。

    这样 ``coder -> {review_style, review_security} -> aggregate`` 会被正确
    放成“一个入口、两个并行、一个汇合”，而不是把某个 reviewer 甩到图底部。
    """
    layer = _bfs_layers(spec)
    for _ in range(3):
        # 只有“严格后退”的边才当回边；同层边参与最长路径，能把终态节点推到下一层
        back = {
            (edge.source, edge.target)
            for edge in spec.edges
            if layer.get(edge.target, 0) < layer.get(edge.source, 0)
        }
        forward = {(edge.source, edge.target) for edge in spec.edges} - back
        new_layer = _longest_path_layers(spec, forward, layer)
        if new_layer == layer:
            break
        layer = new_layer

    # 保证入口在 0 层
    base = layer.get(spec.start, 0)
    if base:
        layer = {node: value - base for node, value in layer.items()}
    return layer


def find_back_edges(spec: WorkflowSpec, layer: dict[str, int]) -> set[tuple[str, str]]:
    """回边 = 指向上层的边，用于可视化时画成虚线并绕行到右侧。"""
    return {
        (edge.source, edge.target)
        for edge in spec.edges
        if layer.get(edge.target, 0) < layer.get(edge.source, 0)
    }


def compute_layout(spec: WorkflowSpec, direction: Direction = "TB") -> Layout:
    layer = compute_layers(spec)
    back_edges = find_back_edges(spec, layer)

    by_layer: dict[int, list[str]] = {}
    for node in spec.node_ids:
        by_layer.setdefault(layer[node], []).append(node)

    boxes: dict[str, NodeBox] = {}
    max_cols = max((len(v) for v in by_layer.values()), default=1)
    layers = max(by_layer) + 1 if by_layer else 1
    span = max_cols * NODE_W + (max_cols - 1) * H_GAP
    width = 2 * PAD_X + span + (110 if back_edges else 0)
    height = 2 * PAD_Y + layers * NODE_H + (layers - 1) * V_GAP

    for layer_index, node_ids in sorted(by_layer.items()):
        count = len(node_ids)
        block_w = count * NODE_W + (count - 1) * H_GAP
        left = PAD_X + (span - block_w) / 2
        for index, node_id in enumerate(node_ids):
            if direction == "TB":
                x = left + index * (NODE_W + H_GAP)
                y = PAD_Y + layer_index * (NODE_H + V_GAP)
            else:
                x = PAD_X + layer_index * (NODE_W + V_GAP)
                y = PAD_Y + index * (NODE_H + H_GAP)
            boxes[node_id] = NodeBox(node_id=node_id, layer=layer_index, index=index, x=x, y=y)
    if direction == "LR":
        width = 2 * PAD_X + layers * NODE_W + (layers - 1) * V_GAP + (110 if back_edges else 0)
        height = 2 * PAD_Y + max_cols * NODE_H + (max_cols - 1) * H_GAP
    return Layout(boxes=boxes, back_edges=back_edges, width=width, height=height,
                  layers=layers, direction=direction)


def node_status(spec: WorkflowSpec, state) -> dict[str, dict]:
    """把运行状态映射成可视化状态：ok / running / failed / skipped / pending。"""
    statuses: dict[str, dict] = {nid: {"status": "pending", "visits": 0} for nid in spec.node_ids}
    for run in state.node_runs:
        statuses[run.node_id] = {
            "status": "ok" if run.status == "ok" else run.status,
            "visits": statuses.get(run.node_id, {}).get("visits", 0) + 1,
            "duration_ms": run.duration_ms,
            "agent": run.agent,
            "error": run.error,
            "attempt": run.attempt,
        }
    return statuses
