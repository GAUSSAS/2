# 使用手册

> 覆盖：安装 → 命令 → 产物 → 工作流 JSON 字段 → 自定义 Agent/规则/流程 → 接入 LLM → 报告与 Web 控制台 → 排错。

---

## 1. 环境要求

| 项 | 要求 |
| --- | --- |
| Python | 3.10+（开发验证用 3.13.9），**只用标准库** |
| 第三方包 | 无 |
| 操作系统 | Windows / macOS / Linux（本作业在 Windows 上验证） |
| 网络 / API Key | 不需要（默认离线规则驱动） |

```powershell
cd "D:\ai homwork2"
python --version          # 确认 3.10+
python demo.py            # 冒烟：跑通两个流程
```

---

## 2. 命令参考

### 2.1 全局参数

```powershell
python -m agentflow [--run-root runs] [--no-color] <命令> ...
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--run-root` | `runs` | 运行产物目录 |
| `--no-color` | 关 | 关闭终端颜色（重定向到文件时建议打开） |

> 全局参数要写在子命令**之前**：`python -m agentflow --no-color run --workflow code_review`。

### 2.2 `list` 列出工作流 / Agent / 规则

```powershell
python -m agentflow list
```

输出两个工作流、11 个已注册 Agent（含输入/输出契约）、14 条审查规则（标注严重度与可修复性）。

### 2.3 `validate` 静态校验工作流

```powershell
python -m agentflow validate --workflow code_review
```

检查：节点 id 唯一、边引用存在、`start` 合法、Agent 已注册、`when` 表达式语法、
`needs` 引用、输入引用命名空间、节点可达性。出错时一次性列出**全部**问题。

### 2.4 `run` 执行工作流

```powershell
# 最简
python -m agentflow run --workflow code_review

# 传参（VALUE 会尝试按 JSON 解析，失败则当字符串）
python -m agentflow run --workflow code_review --input task="实现订单导出服务" --input max_rounds=2

# 审查你自己的代码（导入外部文件，跳过模板生成）
python -m agentflow run --workflow code_review --target src/billing.py
python -m agentflow run --workflow code_review --input path=src/billing.py --input mode=file

# 审查一段内联代码
python -m agentflow run --workflow code_review --input "source=import hashlib`ndef h(t):`n    return hashlib.md5(t.encode())"

# 故障注入：让 coder 第一次执行失败，观察自动重试
python -m agentflow run --workflow code_review --fault coder:1

# 机器可读 + 指定运行 id + 跑完自动打开报告
python -m agentflow run --workflow code_review --json --run-id my-demo --open

# 安静模式（只保留最终结论）
python -m agentflow run --workflow code_review --quiet
```

| 参数 | 说明 |
| --- | --- |
| `--workflow` | 工作流 id（`code_review` / `content_creation`）或 JSON 路径 |
| `--input KEY=VALUE` | 可重复；`VALUE` 先按 JSON 解析（`2`→int，`true`→bool），失败则原样字符串 |
| `--target PATH` | 直接审查指定源码文件（等价于 `--input path=PATH --input mode=file`） |
| `--parallelism N` | 覆盖工作流 `settings.parallelism` |
| `--fault NODE[:N]` | 故障注入，可重复；表示该节点前 N 次执行抛可重试异常 |
| `--json` | 输出结果 JSON（含 summary/variables/artifacts） |
| `--quiet` | 不打印执行过程（未知入参的告警仍会写 stderr） |
| `--no-report` | 不保留 HTML 报告 |
| `--open` | 结束后用浏览器打开报告 |
| `--run-id` | 指定运行 id（默认 `<workflow>-<时间戳>-<随机4位>`） |

**退出码**：`0` = 完成（`passed` 或 `partial`），`1` = 失败。用 `--json` 时以 JSON 里的
`status` 为准（`passed` / `partial` / `failed`）。

**审查外部代码的入参**（`code_review` 工作流）：

| 入参 | 说明 |
| --- | --- |
| `path` | 待审文件路径（相对当前目录）；给值后自动进入导入模式 |
| `source` | 内联源码字符串（与 `path` 二选一） |
| `mode` | `template`（默认）/ `file` / `llm`；给了 `path`/`source` 时自动按导入处理 |
| `filename` | 交付文件名；留空时按 `path` 的文件名推导 |
| `task` | 需求/审查目的；可省略，导入模式下自动生成「审查并加固已有模块 X」 |

导入时会做两件事：读文件（UTF-8，失败回退 GBK）、`compile()` 语法校验；空文件或语法错误
会让运行**快速失败并给出明确原因**，不会带着坏代码继续跑。产物写在
`runs/<run_id>/artifacts/`，原文件不会被修改。传了工作流不认识的入参时，会在事件流里留下
`warning` 并（即使 `--quiet`）打印到 stderr，避免“以为传了参数其实被忽略”。

### 2.5 `resume` 从断点续跑

```powershell
python -m agentflow run --workflow code_review --fault coder:2      # 造一个失败（重试次数不够）
#   → 失败，记下 run_id
python -m agentflow resume --run-id code_review-20261025-121642-3l2d
```

续跑会从最后一个成功波次的断点继续，失败的节点会被重试；事件流续写在同一份
`events.jsonl` 里，报告会重新生成。

### 2.6 `visualize` 导出流程图

```powershell
python -m agentflow visualize --workflow code_review --out docs/assets/code_review.svg
python -m agentflow visualize --workflow code_review --html --out docs/code_review.html
```

### 2.7 `serve` 启动 Web 控制台

```powershell
python -m agentflow serve                 # http://127.0.0.1:7860
python -m agentflow serve --port 8080 --no-browser
```

页面能力：选择工作流 → 填参 → 运行；运行中每 600ms 轮询并**实时重绘流程图**
（节点变色、标出重试）；右侧展示历史运行并可打开完整 HTML 报告。

---

## 3. 运行产物

每次运行在 `runs/<run_id>/` 下生成：

| 文件 | 内容 | 用途 |
| --- | --- | --- |
| `report.html` | 单文件可视化报告（内嵌 SVG、时间线、消息表、diff） | 演示/交作业，双击即看 |
| `summary.json` | 状态、耗时、节点执行明细、统计、作品清单 | 机器消费 / 看板 |
| `state.json` | 完整状态快照（含作品全部版本） | 复盘、断点 |
| `checkpoint.json` | 调度器簿记（`gen/seen/visits/frontier/wave/step`） | `resume` 用 |
| `events.jsonl` | 结构化事件流（每行一个事件） | 日志分析 |
| `workflow.json` | 本次使用的工作流定义 | 可追溯“当时用的哪个版本流程” |
| `artifacts/` | 终版产物（`profile_service.py`、`review_report.md`…） | 直接取用 |
| `artifacts/versions/` | 每个历史版本（`profile_service.v1.py`…） | diff / 回滚 |

---

## 4. 工作流 JSON 字段参考

顶层：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | ✅ | 唯一标识，也是 `--workflow` 的取值 |
| `name` / `description` / `version` | | 展示用 |
| `start` | ✅ | 入口节点 id |
| `inputs` | | 入参声明：`{名称: {type, required, default, description}}`；`default` 会作为默认值 |
| `defaults` | | 节点默认值：`max_visits` / `timeout_s` / `on_error` / `retry` |
| `settings` | | `parallelism`（并行度）、`max_steps`（调度步数上限） |
| `nodes` | ✅ | 节点数组 |
| `edges` | ✅ | 边数组 |

节点 `NodeSpec`：

| 字段 | 说明 |
| --- | --- |
| `id` | 唯一，报告中显示 |
| `agent` | 已注册的 Agent 名 |
| `name` / `description` / `kind` | 展示与着色（`author/review/gate/fix/deliver/risk`） |
| `inputs` | 引用映射（运行时解析，见 5.1） |
| `params` | 字面量配置，与解析后的 `inputs` 合并后传给 Agent（`inputs` 优先） |
| `outputs` | 输出契约：缺字段视为失败（可触发重试） |
| `needs` | 前置节点（join）：必须在本节点执行前各完成一次 |
| `max_visits` | 最多执行次数（死循环保护） |
| `timeout_s` | 单次执行超时（超时视为可重试错误） |
| `retry` | `{max_attempts, backoff_ms, multiplier, max_backoff_ms, retry_on}` |
| `on_error` | `fail`（默认，终止运行）或 `skip`（标记跳过，走 `on="skip"` 边） |

边 `EdgeSpec`：

| 字段 | 说明 |
| --- | --- |
| `from` / `to` | 源/目标节点 id |
| `when` | 条件表达式，空表示无条件 |
| `label` | 图上显示的标签 |
| `on` | `success`（默认）/ `skip`：该边在何种节点状态下才考虑 |
| `exclusive` | 命中后不再评估同一源节点的后续边（实现“优先级最高分支”） |

---

## 5. 表达式与引用

### 5.1 输入引用（节点 `inputs` 的值）

```jsonc
{
  "task": "inputs.task",                                  // 运行入参
  "verdict": "vars.verdict",                              // 变量黑板
  "code": "artifacts.code",                               // 作品内容
  "findings": "nodes.aggregate.findings",                 // 节点输出
  "literal_mode": "literal:error",                        // 字面量
  "notes": {"ref": "nodes.edit.notes", "default": []}     // 可选 + 默认值
}
```

裸名（如 `"verdict"`）会按 `inputs → vars → counters → 作品 → 节点输出` 顺序查找，
适合边条件里图省事；节点输入建议用显式路径，避免歧义。

### 5.2 条件表达式（边的 `when`）

支持：字面量、点号路径、`== != < <= > >= in not in is is not`、`and or not`、
`+ - * / // % **`、三元表达式 `a if cond else b`、白名单函数
`len/min/max/sum/abs/any/all/int/str/float/bool/round` 以及
`contains(text, sub)`、`startswith`、`endswith`。

```jsonc
"when": "verdict == 'fail'"
"when": "verdict == 'needs_revision' and revision < inputs.max_revisions"
"when": "fixed_count > 0 and remaining_errors == 0"
"when": "len(findings) > 0"
"when": "iteration >= inputs.max_rounds"
```

**明确禁止**（会抛 `ExpressionError`）：`__import__(...)`、属性/下标赋值、lambda、推导式、
关键字参数调用、`eval/exec/open` 等任何未列入白名单的调用。
实现见 `agentflow/core/expr.py`，测试 `tests/test_core.py::test_rejects_arbitrary_code`。

### 5.3 引擎提供的上下文变量

| 变量 | 含义 |
| --- | --- |
| `inputs.*` | 运行入参 |
| `vars.*` | 变量黑板（Agent 用 `ctx.set()` 写入） |
| `counters.*` | 计数器（`ctx.incr()`），另有 `iteration` / `step` 便捷量 |
| `nodes.<id>.<key>` | 节点最近一次输出 |
| `nodes.<id>.status` | 节点最近一次状态（`ok`/`skipped`/`failed`） |
| `arts.<name>.version` | 作品版本号 |
| `run_id` / `workflow` | 运行标识 |

---

## 6. 自定义 Agent

```python
from agentflow.core.agent import AgentContext, AgentResult, BaseAgent, register_agent

@register_agent
class DesignAgent(BaseAgent):
    """设计 Agent：把需求转成模块设计。"""

    name = "designer"
    description = "设计 Agent：输出模块划分与接口草案"
    inputs = ("task",)             # 仅作契约声明与文档
    outputs = ("design", "modules")  # 缺字段会触发失败/重试

    def run(self, ctx: AgentContext, inputs) -> AgentResult:
        task = inputs["task"]
        design = {"modules": ["api", "service", "storage"], "task": task}

        # 1) 写作品（自动版本化）
        ctx.put_artifact("design", design, note="首版设计")
        # 2) 写变量（供 when 条件使用）
        ctx.set("design_ready", True)
        # 3) 发消息（协作轨迹）
        ctx.send("coder", "handoff", {"summary": "设计完成，请编码"})
        # 4) 日志（进报告）
        ctx.log(f"拆出 {len(design['modules'])} 个模块")

        return AgentResult(outputs={"design": design, "modules": design["modules"]},
                           artifacts=["design"])
```

注册后在工作流里引用：

```jsonc
{ "id": "design", "agent": "designer", "inputs": { "task": "inputs.task" },
  "outputs": ["design", "modules"] }
```

**外部插件**（不改动本项目代码）：

```powershell
$env:AGENTFLOW_AGENTS = "my_team.agents"     # 逗号分隔的模块路径
python -m agentflow run --workflow my_flow
```

模块里用 `@register_agent` 即可；`load_plugin_agents()` 会在 CLI 启动时自动导入。

---

## 7. 自定义审查规则与自动修复

```python
# 规则：发现残留 TODO
from agentflow.agents.linting import Finding, rule

@rule("todo-comment", category="style", severity="warning",
      message="源码中残留 TODO", hint="上线前清理 TODO")
def check_todo(tree, source):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and "TODO" in node.value:
            yield Finding(rule_id="todo-comment", severity="warning",
                          message="注释里还有 TODO", category="style",
                          line=getattr(node, "lineno", 0), hint="上线前清理",
                          symbol="", node=node)
```

规则注册后，`reviewer` 节点、`rule_catalog`、报告、`python -m agentflow list` 全部自动更新。
若还想要自动修复：

```python
from agentflow.agents.codemods import fixer

@fixer("todo-comment")
def fix_todo(tree, finding, parents, ctx):
    ...            # 就地修改 AST，返回 True 表示已修复
    return True
```

`apply_fixes` 会在修复后 `compile()` 校验；失败则回退原文并记录原因，绝不写入坏代码。

---

## 8. 接入真实 LLM（可选）

```powershell
$env:AGENTFLOW_LLM_BASE_URL = "https://api.deepseek.com/v1"   # 或 OpenAI/vLLM/Ollama
$env:AGENTFLOW_LLM_API_KEY  = "sk-..."
$env:AGENTFLOW_LLM_MODEL    = "deepseek-chat"
python -m agentflow run --workflow code_review --input mode=llm
```

其他可调环境变量：`AGENTFLOW_LLM_TIMEOUT`（秒）、`AGENTFLOW_LLM_TEMPERATURE`、
`AGENTFLOW_LLM_MAX_TOKENS`。

行为说明：
* 未配置时 `mode=llm` 会**自动回退**到离线模板，并在日志里说明原因（不会让流程失败）；
* LLM 输出会剥离 ``` 代码围栏并做语法校验，不合法同样回退；
* 生成器名字会写进作品与报告（`generator=llm:deepseek-chat` / `offline-template(fallback)`）。

你可以在任意自定义 Agent 里直接调用：

```python
from agentflow.agents.llm import get_provider

provider = get_provider()          # 未配置返回 None
if provider:
    text = provider.complete("...", system="你只输出可运行的 Python 源码。")
```

---

## 9. 报告与控制台

### 9.1 HTML 报告 6 个区块

1. **工作流执行图**：状态着色 + 边上条件 + 图例；
2. **运行概览**：入参、轮次、终态变量、作品版本表、完整执行时间线；
3. **Agent 协作**：全部消息（`#`、方向、类型、内容）；
4. **审查发现与修复**：按严重度分组的发现、**评审轮次演进**表、自动修复明细；
5. **作品与代码演进**：首版→终版的 unified diff、终版代码、版本历史；
6. **原始数据**：`summary.json`、工作流定义、状态快照、完整事件流（折叠查看）。

### 9.2 Web 控制台

* 左侧：流程图（服务端按当前事件重渲染 SVG，前端只换 `<img>`）；
* 右侧：工作流选择、入参表单（按 `inputs` 声明自动生成）、故障注入、历史运行；
* 运行中实时显示状态胶囊、节点变色、日志流；完成后给出报告链接。

---

## 10. 排错

| 现象 | 原因 / 处理 |
| --- | --- |
| `找不到工作流 'xxx'` | `--workflow` 用 id 或 JSON 路径；`python -m agentflow list` 看可用 id |
| `未注册的 Agent: 'xxx'` | 该 Agent 未 import/注册；自定义 Agent 需用 `@register_agent` 或 `AGENTFLOW_AGENTS` |
| `校验失败` 且提示表达式非法 | `when` 用了不支持的语法（如函数调用、lambda），改写或用节点输出预先计算 |
| `输入 ... 无法解析` | 引用了还没产出的作品/节点输出；改用 `{"ref": ..., "default": ...}` 或调整流程顺序 |
| 运行 `failed` 且提示 `max_visits` | 循环没有收敛；检查回边条件与 `max_rounds`，或用 `resume` 续跑 |
| 想看某个节点到底为什么失败 | `runs/<id>/events.jsonl` 里搜 `node_end`（含 `traceback`），或报告的原始数据区 |
| 中文在终端乱码 | `chcp 65001` 或 `$env:PYTHONIOENCODING="utf-8"` |
| 端口被占用 | `python -m agentflow serve --port 8080` |

---

## 11. 规模与性能（实测）

| 指标 | 数值 |
| --- | --- |
| 代码审查流程 | 8 节点 / 14 边 / 10 波次 / 13 次节点执行 → 约 **50 ms** |
| 内容创作流程 | 4 节点 / 5 边 / 6 波次 / 6 次节点执行 → 约 **20 ms** |
| 50 个测试 | 约 **1.6 s** |
| 报告文件 | 约 **200–350 KB**（单文件、自包含） |

引擎本身的开销（调度、状态、事件）在毫秒级；真实场景的耗时取决于 Agent 内部
（换成 LLM 后每个节点可能是秒级），此时 `parallelism` 与 `timeout_s` 才有实际意义。
