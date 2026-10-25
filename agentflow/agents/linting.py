"""代码审查规则引擎（基于 AST 的静态检查）。

为什么自己实现规则引擎？
------------------------
课程要求是“Agent 协作 + 工作流”，重点不在审查工具本身。用 AST 写一套
可插拔的规则，好处是：**离线、确定性、可复现** —— 不需要 LLM Key，
评审时能稳定复现“发现问题 → 修复 → 复审通过”的闭环。

规则注册表是数据驱动的：新增一条规则只需 ``@rule(...)`` 装饰一个函数；
新增一个审查维度（如“性能”）只需给规则打上新的 ``category``，
再在工作流里加一个 reviewer 节点即可，引擎与其它 Agent 都不用改。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
SQL_KEYWORDS = ("select ", "insert ", "update ", "delete ", "drop ", "create ")
SECRET_NAME_RE = re.compile(r"(API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_?KEY|ACCESS_?KEY)", re.I)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    """一条审查发现。"""

    rule_id: str
    severity: str
    message: str
    category: str = "style"
    line: int = 0
    hint: str = ""
    symbol: str = ""
    fixable: bool = True
    #: AST 节点引用（仅在进程内用于自动修复，不参与序列化）
    node: ast.AST | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "message": self.message,
            "category": self.category,
            "line": self.line,
            "hint": self.hint,
            "symbol": self.symbol,
            "fixable": self.fixable,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Finding":
        return cls(
            rule_id=data.get("rule_id", ""),
            severity=data.get("severity", "warning"),
            message=data.get("message", ""),
            category=data.get("category", "style"),
            line=int(data.get("line", 0)),
            hint=data.get("hint", ""),
            symbol=data.get("symbol", ""),
            fixable=bool(data.get("fixable", True)),
        )

    @property
    def key(self) -> tuple[str, int]:
        return (self.rule_id, self.line)

    def label(self) -> str:
        return f"L{self.line} {self.rule_id} {self.message}"


@dataclass
class Rule:
    rule_id: str
    category: str
    severity: str
    message: str
    hint: str
    fixable: bool
    check: Callable[[ast.Module, str], Iterable[Finding]]


RULES: dict[str, Rule] = {}


def rule(rule_id: str, *, category: str, severity: str, message: str,
         hint: str = "", fixable: bool = True):
    """注册一条检查规则。"""

    def decorator(func: Callable[[ast.Module, str], Iterable[Finding]]):
        RULES[rule_id] = Rule(
            rule_id=rule_id,
            category=category,
            severity=severity,
            message=message,
            hint=hint,
            fixable=fixable,
            check=func,
        )
        return func

    return decorator


# ---------------------------------------------------------------------------
# AST 辅助
# ---------------------------------------------------------------------------
def parse_module(source: str) -> ast.Module:
    return ast.parse(source)


def iter_functions(tree: ast.Module) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def has_docstring(node: ast.AST) -> bool:
    body = getattr(node, "body", None) or []
    if not body:
        return False
    first = body[0]
    return isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)


def is_sql(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(kw in node.value.lower() for kw in SQL_KEYWORDS)
    )


def used_names(tree: ast.Module) -> set[str]:
    """被读取（Load）的标识符名称集合，用于未使用导入检测。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            names.add(node.value.id)
    return names


def imported_bindings(tree: ast.Module) -> list[tuple[str, ast.stmt, ast.alias]]:
    """返回 (绑定名, import 语句, alias) 列表，只扫描任意层级的 import。"""
    rows: list[tuple[str, ast.stmt, ast.alias]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                rows.append((alias.asname or alias.name.split(".")[0], node, alias))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                rows.append((alias.asname or alias.name, node, alias))
    return rows


def module_level_mutables(tree: ast.Module) -> set[str]:
    """模块级可变全局量（用于检测“可变默认参数”的间接写法）。"""
    mutable: set[str] = set()
    for node in tree.body:
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        if value is None:
            continue
        if isinstance(value, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
            for target in targets:
                if isinstance(target, ast.Name):
                    mutable.add(target.id)
    return mutable


def call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = func.value.id if isinstance(func.value, ast.Name) else ""
        return f"{base}.{func.attr}" if base else func.attr
    return ""


# ---------------------------------------------------------------------------
# 代码风格规则（style pack）
# ---------------------------------------------------------------------------
@rule("module-docstring", category="style", severity="warning",
      message="模块缺少文档字符串",
      hint="在文件开头添加三引号说明模块职责")
def check_module_docstring(tree: ast.Module, source: str):
    if not has_docstring(tree):
        yield Finding(rule_id="module-docstring", severity="warning",
                      message="模块缺少文档字符串", category="style",
                      line=1, hint="在文件开头添加三引号说明模块职责",
                      symbol="<module>", node=tree)


@rule("function-docstring", category="style", severity="warning",
      message="函数缺少文档字符串",
      hint="说明参数含义、返回值与副作用")
def check_function_docstring(tree: ast.Module, source: str):
    for func in iter_functions(tree):
        if not has_docstring(func):
            yield Finding(rule_id="function-docstring", severity="warning",
                          message=f"函数 {func.name} 缺少文档字符串", category="style",
                          line=func.lineno, hint="说明参数含义、返回值与副作用",
                          symbol=func.name, node=func)


@rule("missing-type-hints", category="style", severity="warning",
      message="函数缺少类型标注",
      hint="为参数与返回值补充类型标注，便于静态检查")
def check_type_hints(tree: ast.Module, source: str):
    for func in iter_functions(tree):
        args = list(func.args.args) + list(func.args.kwonlyargs) + list(func.args.posonlyargs)
        missing = [a.arg for a in args if a.annotation is None and a.arg not in ("self", "cls")]
        if missing or (func.returns is None and func.name != "__init__"):
            detail = f"参数 {', '.join(missing)}" if missing else "返回值"
            yield Finding(rule_id="missing-type-hints", severity="warning",
                          message=f"函数 {func.name} 缺少类型标注（{detail}）",
                          category="style", line=func.lineno,
                          hint="为参数与返回值补充类型标注", symbol=func.name, node=func)


@rule("mutable-default-argument", category="style", severity="warning",
      message="使用了可变对象作为默认参数",
      hint="改为 None 并在函数体内初始化，避免跨调用共享状态")
def check_mutable_defaults(tree: ast.Module, source: str):
    suspicious = module_level_mutables(tree)
    for func in iter_functions(tree):
        args = func.args
        positional = list(args.args)[len(args.args) - len(args.defaults):] if args.defaults else []
        pairs: list[tuple[ast.arg, ast.AST]] = list(zip(positional, args.defaults))
        pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
        for arg, default in pairs:
            is_mutable = isinstance(default, (ast.List, ast.Dict, ast.Set,
                                              ast.ListComp, ast.DictComp, ast.SetComp))
            if isinstance(default, ast.Name) and default.id in suspicious:
                is_mutable = True
            if is_mutable:
                yield Finding(rule_id="mutable-default-argument", severity="warning",
                              message=f"函数 {func.name} 的参数 {arg.arg} 使用可变默认值",
                              category="style", line=func.lineno,
                              hint="改为 None 并在函数体内初始化", symbol=func.name, node=func)


@rule("bare-except", category="style", severity="warning",
      message="使用裸 except 捕获所有异常",
      hint="显式写出异常类型，避免掩盖 KeyboardInterrupt 等问题")
def check_bare_except(tree: ast.Module, source: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            yield Finding(rule_id="bare-except", severity="warning",
                          message="使用裸 except 捕获所有异常", category="style",
                          line=node.lineno, hint="改为 except Exception as exc 并记录日志",
                          symbol="", node=node)


@rule("unused-import", category="style", severity="warning",
      message="存在未使用的导入",
      hint="删除未使用的 import，减少依赖与启动开销")
def check_unused_imports(tree: ast.Module, source: str):
    used = used_names(tree)
    seen: set[str] = set()
    for name, statement, alias in imported_bindings(tree):
        if name in used or name in seen:
            continue
        seen.add(name)
        yield Finding(rule_id="unused-import", severity="warning",
                      message=f"导入 {name} 未被使用", category="style",
                      line=statement.lineno, hint="删除未使用的 import",
                      symbol=name, node=statement)


@rule("open-without-encoding", category="style", severity="warning",
      message="open() 未指定 encoding",
      hint="显式指定 encoding='utf-8'，避免依赖平台默认编码")
def check_open_encoding(tree: ast.Module, source: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and call_name(node) == "open":
            if not any(kw.arg == "encoding" for kw in node.keywords):
                yield Finding(rule_id="open-without-encoding", severity="warning",
                              message="open() 未指定 encoding", category="style",
                              line=node.lineno, hint="显式指定 encoding='utf-8'",
                              symbol="open", node=node)


@rule("print-statement", category="style", severity="info", fixable=False,
      message="生产代码中使用 print 输出",
      hint="改用 logging，便于分级与采集")
def check_print(tree: ast.Module, source: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and call_name(node) == "print":
            yield Finding(rule_id="print-statement", severity="info",
                          message="生产代码中使用 print 输出", category="style",
                          line=node.lineno, hint="改用 logging，便于分级与采集",
                          symbol="print", fixable=False, node=node)


@rule("long-function", category="style", severity="info", fixable=False,
      message="函数过长，建议拆分",
      hint="单函数建议控制在 40 行以内")
def check_long_function(tree: ast.Module, source: str):
    for func in iter_functions(tree):
        length = (getattr(func, "end_lineno", func.lineno) or func.lineno) - func.lineno
        if length > 40:
            yield Finding(rule_id="long-function", severity="info",
                          message=f"函数 {func.name} 有 {length} 行，建议拆分", category="style",
                          line=func.lineno, hint="单函数建议控制在 40 行以内",
                          symbol=func.name, fixable=False, node=func)


# ---------------------------------------------------------------------------
# 安全规则（security pack）
# ---------------------------------------------------------------------------
@rule("sql-injection", category="security", severity="error",
      message="SQL 语句由字符串拼接/格式化构造，存在注入风险",
      hint="改用参数化查询：execute(sql, params)")
def check_sql_injection(tree: ast.Module, source: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and is_sql(node.left):
            yield Finding(rule_id="sql-injection", severity="error",
                          message="SQL 语句由 % 格式化构造，存在注入风险", category="security",
                          line=node.lineno, hint="改用参数化查询：execute(sql, params)",
                          symbol="execute", node=node)
        elif isinstance(node, ast.JoinedStr) and node.values and is_sql(node.values[0]):
            # f-string 拼接无法安全地自动改写成参数化查询 → 明确标记为不可自动修复
            yield Finding(rule_id="sql-injection", severity="error",
                          message="SQL 语句由 f-string 构造，存在注入风险", category="security",
                          line=node.lineno, hint="改用参数化查询：execute(sql, params)",
                          symbol="execute", fixable=False, node=node)


@rule("hardcoded-secret", category="security", severity="error",
      message="源码中硬编码了凭据",
      hint="从环境变量或密钥管理服务读取")
def check_hardcoded_secret(tree: ast.Module, source: str):
    for node in tree.body:
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str) or not value.value:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and SECRET_NAME_RE.search(target.id):
                yield Finding(rule_id="hardcoded-secret", severity="error",
                              message=f"变量 {target.id} 硬编码了凭据", category="security",
                              line=node.lineno, hint="从环境变量或密钥管理服务读取",
                              symbol=target.id, node=node)


@rule("weak-hash", category="security", severity="error",
      message="使用了不安全的哈希算法",
      hint="口令/完整性场景请改用 sha256 或更安全的 KDF")
def check_weak_hash(tree: ast.Module, source: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = call_name(node)
            if name in ("hashlib.md5", "hashlib.sha1", "md5", "sha1"):
                yield Finding(rule_id="weak-hash", severity="error",
                              message=f"使用了不安全的哈希算法 {name}", category="security",
                              line=node.lineno, hint="改用 sha256 或更安全的 KDF",
                              symbol=name, node=node)


@rule("broad-except", category="security", severity="info", fixable=False,
      message="捕获 Exception 后未记录/未处理",
      hint="记录异常并区分可恢复与不可恢复错误")
def check_broad_except(tree: ast.Module, source: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name) and node.type.id == "Exception":
            is_swallowed = len(node.body) == 1 and isinstance(node.body[0], (ast.Pass, ast.Return))
            if is_swallowed:
                yield Finding(rule_id="broad-except", severity="info",
                              message="捕获 Exception 后静默吞掉", category="security",
                              line=node.lineno, hint="记录异常并区分可恢复与不可恢复错误",
                              symbol="", fixable=False, node=node)


@rule("shell-true", category="security", severity="info", fixable=False,
      message="subprocess 使用 shell=True",
      hint="传入列表参数并避免 shell=True")
def check_shell_true(tree: ast.Module, source: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and any(
            kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        ):
            yield Finding(rule_id="shell-true", severity="info",
                          message="subprocess 使用 shell=True", category="security",
                          line=node.lineno, hint="传入列表参数并避免 shell=True",
                          symbol="subprocess", fixable=False, node=node)


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def rule_catalog() -> list[dict[str, Any]]:
    """列出所有规则（文档 / Web UI 使用）。"""
    return [
        {
            "rule_id": r.rule_id,
            "category": r.category,
            "severity": r.severity,
            "message": r.message,
            "hint": r.hint,
            "fixable": r.fixable,
        }
        for r in sorted(RULES.values(), key=lambda r: (r.category, SEVERITY_ORDER.get(r.severity, 9), r.rule_id))
    ]


def analyze(source: str, categories: Iterable[str] | None = None, *,
            rule_ids: Iterable[str] | None = None,
            tree: ast.Module | None = None) -> list[Finding]:
    """对源码运行规则，返回按严重度/行号排序的发现列表。

    Args:
        categories: 只运行这些类别的规则（``["style"]`` / ``["security"]``）。
        rule_ids: 只运行指定的规则（优先于 categories）。
        tree: 可传入已解析的 AST（修复流程必须复用同一棵树，否则 finding 里的
            节点引用会指向另一棵树，修复将作用不到目标上）。
    """
    tree = tree if tree is not None else parse_module(source)
    wanted_categories = set(categories) if categories else None
    wanted_rules = set(rule_ids) if rule_ids else None
    findings: list[Finding] = []
    for rule_obj in RULES.values():
        if wanted_rules is not None:
            if rule_obj.rule_id not in wanted_rules:
                continue
        elif wanted_categories is not None and rule_obj.category not in wanted_categories:
            continue
        try:
            result = rule_obj.check(tree, source)
            findings.extend(list(result))
        except SyntaxError:  # pragma: no cover - parse 已经在上面校验
            raise
        except Exception as exc:  # noqa: BLE001 - 单条规则失败不应中断整轮审查
            findings.append(
                Finding(rule_id=rule_obj.rule_id, severity="info",
                        message=f"规则执行异常: {type(exc).__name__}: {exc}",
                        category=rule_obj.category, hint="请联系规则维护者", fixable=False)
            )
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.line, f.rule_id))
    return findings


def summarize(findings: Iterable[Finding | Mapping[str, Any]]) -> dict[str, Any]:
    """统计发现：按严重度、类别、规则聚合。"""
    by_severity = {"error": 0, "warning": 0, "info": 0}
    by_rule: dict[str, int] = {}
    by_category: dict[str, int] = {}
    fixable = 0
    for finding in findings:
        data = finding.to_dict() if isinstance(finding, Finding) else dict(finding)
        severity = data.get("severity", "warning")
        by_severity[severity] = by_severity.get(severity, 0) + 1
        by_rule[data["rule_id"]] = by_rule.get(data["rule_id"], 0) + 1
        category = data.get("category", "style")
        by_category[category] = by_category.get(category, 0) + 1
        if data.get("fixable", True):
            fixable += 1
    return {
        "total": sum(by_severity.values()),
        "error": by_severity["error"],
        "warning": by_severity["warning"],
        "info": by_severity["info"],
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "by_category": by_category,
        "fixable": fixable,
    }


def score(findings: Iterable[Finding | Mapping[str, Any]]) -> int:
    """把发现折算成 0-100 的可读分数（error 扣 12，warning 扣 3）。"""
    stats = summarize(findings)
    value = 100 - stats["error"] * 12 - stats["warning"] * 3
    return max(0, min(100, value))


def verdict_of(findings: Iterable[Finding | Mapping[str, Any]], *, allow_warnings: bool = True) -> str:
    """根据发现决定审查结论：pass / pass_with_warnings / fail。"""
    stats = summarize(findings)
    if stats["error"] > 0:
        return "fail"
    if stats["warning"] > 0 and not allow_warnings:
        return "fail"
    if stats["warning"] > 0:
        return "pass_with_warnings"
    return "pass"
