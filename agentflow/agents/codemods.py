"""自动修复（code mod）。

修复不是“重新生成一份好代码”，而是对现有 AST 做**定点变换**：
``规则 id -> 变换函数``。这样修复行为与审查规则一一对应、可解释、可测试，
新增一条规则的修复只需要再注册一个 fixer。

设计要点
--------
* 只应用 ``severity in severities`` 且**有 fixer** 的发现 —— 因而“只修阻塞问题”
  （severity=error）和“清理风格问题”（severity=warning）可以是同一条流程里的
  两个节点，由工作流决定先修哪一类。
* 修完统一用 ``ast.unparse`` 重写源码，并 ``compile()`` 验证语法；验证失败则
  回退原文，把问题如实记录进报告，绝不把坏代码写进作品仓库。
* 因为使用 AST 重写，**原有注释不会保留**（已知限制，见文档）。
"""

from __future__ import annotations

import ast
import copy
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .linting import (
    Finding,
    analyze,
    is_sql,
    module_level_mutables,
    summarize,
    used_names,
)


@dataclass
class FixOutcome:
    source: str
    applied: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    compiled: bool = True
    error: str | None = None
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    remaining: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "skipped": self.skipped,
            "compiled": self.compiled,
            "error": self.error,
            "before": self.before,
            "after": self.after,
            "remaining": self.remaining,
            "applied_count": len(self.applied),
            "skipped_count": len(self.skipped),
        }


#: rule_id -> 修复函数
FIXERS: dict[str, Callable[[ast.Module, Finding, dict[str, ast.AST], dict[str, Any]], bool]] = {}


def fixer(rule_id: str):
    def decorator(func):
        FIXERS[rule_id] = func
        return func

    return decorator


# ---------------------------------------------------------------------------
# AST 辅助
# ---------------------------------------------------------------------------
def build_parent_map(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _annotation(expr: str) -> ast.expr:
    return ast.parse(expr, mode="eval").body


def _ensure_import(tree: ast.Module, module: str, name: str | None = None) -> bool:
    """确保 ``import module``（或 ``from module import name``）存在。"""
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module:
                    return True
        elif isinstance(node, ast.ImportFrom) and node.module == module and name:
            for alias in node.names:
                if alias.name == name:
                    return True
    stmt: ast.stmt
    if name:
        stmt = ast.ImportFrom(module=module, names=[ast.alias(name=name)], level=0)
    else:
        stmt = ast.Import(names=[ast.alias(name=module)])
    # 插在文档字符串与 __future__ 导入之后
    index = 0
    while index < len(tree.body):
        node = tree.body[index]
        is_doc = (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        is_future = isinstance(node, ast.ImportFrom) and node.module == "__future__"
        if not (is_doc or is_future):
            break
        index += 1
    tree.body.insert(index, stmt)
    return True


def _docstring_of(node: Any) -> str:
    body = getattr(node, "body", [])
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[0].value.value
    return ""


def _insert_after_docstring(body: list[ast.stmt], stmt: ast.stmt) -> None:
    index = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        index = 1
    body.insert(index, stmt)


def _arg_pairs(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[ast.arg, ast.AST]]:
    args = func.args
    pairs: list[tuple[ast.arg, ast.AST]] = []
    if args.defaults:
        positional = list(args.args)[len(args.args) - len(args.defaults):]
        pairs.extend(zip(positional, args.defaults))
    pairs.extend((a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None)
    return pairs


def _is_mutable_default(node: ast.AST, suspicious: set[str]) -> bool:
    if isinstance(node, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    return isinstance(node, ast.Name) and node.id in suspicious


def _all_args(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    args = func.args
    return list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)


HINT_BY_NAME: dict[str, str] = {
    "name": "str", "text": "str", "raw": "str", "query": "str", "sql": "str",
    "path": "str", "message": "str", "prefix": "str", "suffix": "str",
    "email": "str", "title": "str", "key": "str", "mode": "str",
    "user": "dict", "data": "dict", "record": "dict", "payload": "dict",
    "config": "dict", "cfg": "dict", "meta": "dict", "row": "dict",
    "tags": "list", "items": "list", "rows": "list", "values": "list", "names": "list",
    "count": "int", "limit": "int", "offset": "int", "size": "int",
    "flag": "bool", "enabled": "bool",
}


def _infer_annotation(arg_name: str) -> str:
    return HINT_BY_NAME.get(arg_name.lower(), "Any")


def _infer_return(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    returns = [n for n in ast.walk(func) if isinstance(n, ast.Return) and n.value is not None]
    if not returns:
        return "None"
    values = [r.value for r in returns]
    if all(isinstance(v, ast.Constant) and isinstance(v.value, bool) for v in values):
        return "bool"
    if all(isinstance(v, ast.Constant) and isinstance(v.value, str) for v in values):
        return "str"
    if all(isinstance(v, ast.Constant) and isinstance(v.value, int) for v in values):
        return "int"
    if all(isinstance(v, (ast.List, ast.ListComp, ast.Tuple)) for v in values):
        return "list"
    if all(isinstance(v, ast.Dict) for v in values):
        return "dict"
    return "Any"


# ---------------------------------------------------------------------------
# 各规则的定点修复
# ---------------------------------------------------------------------------
@fixer("module-docstring")
def fix_module_docstring(tree: ast.Module, finding: Finding, parents, ctx) -> bool:
    doc = ctx.get("module_docstring") or (
        "模块说明（由修复 Agent 自动补充）。\n\n"
        "职责：提供用户档案的读写与查询辅助函数。\n"
        "TODO: 请人工补充完整的模块职责、使用方式与注意事项。"
    )
    if _docstring_of(tree):
        return False
    tree.body.insert(0, ast.Expr(value=ast.Constant(value=doc)))
    return True


@fixer("function-docstring")
def fix_function_docstring(tree: ast.Module, finding: Finding, parents, ctx) -> bool:
    func = finding.node
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) or _docstring_of(func):
        return False
    args = [a.arg for a in _all_args(func)]
    arg_text = ", ".join(args) if args else "无"
    doc = (
        f"{func.name}: 自动补全的文档字符串。\n\n"
        f"    参数: {arg_text}\n"
        f"    返回: {_infer_return(func)}\n"
        "    TODO: 由修复 Agent 依据函数签名生成，请人工确认语义描述。"
    )
    _insert_after_docstring(func.body, ast.Expr(value=ast.Constant(value=doc)))
    return True


@fixer("missing-type-hints")
def fix_type_hints(tree: ast.Module, finding: Finding, parents, ctx) -> bool:
    func = finding.node
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    # 只有类里的第一个参数才是 self；模块级函数的第一个参数是普通入参
    is_method = isinstance(parents.get(func), ast.ClassDef)
    self_name = func.args.args[0].arg if (is_method and func.args.args) else ""
    changed = False
    for arg in _all_args(func):
        if arg.annotation is not None or arg.arg in ("self", "cls"):
            continue
        hint = "Any" if (self_name and arg.arg == self_name) else _infer_annotation(arg.arg)
        arg.annotation = _annotation(hint)
        changed = True
    if func.returns is None and func.name != "__init__":
        func.returns = _annotation(_infer_return(func))
        changed = True
    # 用到了 Any 就补 import
    if re.search(r"\bAny\b", ast.unparse(func)):
        _ensure_import(tree, "typing", "Any")
    return changed


@fixer("mutable-default-argument")
def fix_mutable_default(tree: ast.Module, finding: Finding, parents, ctx) -> bool:
    func = finding.node
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    suspicious = module_level_mutables(tree)
    target = next(((a, d) for a, d in _arg_pairs(func) if _is_mutable_default(d, suspicious)), None)
    if target is None:
        return False
    arg, default = target

    # 解析默认值：若是模块级全局量，取其字面量
    literal: ast.AST | None = None
    if isinstance(default, ast.Name):
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == default.id for t in node.targets
            ):
                literal = copy.deepcopy(node.value)
                break
    else:
        literal = copy.deepcopy(default)
    if literal is None:
        return False

    kind = {ast.List: "list", ast.Dict: "dict", ast.Set: "set"}.get(type(literal), "list")
    if arg.annotation is None:
        arg.annotation = _annotation(f"{kind} | None")
    elif isinstance(arg.annotation, ast.Name) and arg.annotation.id in ("list", "dict", "set", "tuple"):
        # 已有裸容器标注（例如类型标注先被补成 list），需要补上 None
        arg.annotation = ast.BinOp(
            left=arg.annotation, op=ast.BitOr(), right=ast.Constant(value=None)
        )

    # 默认值改为 None
    if default in func.args.defaults:
        func.args.defaults[func.args.defaults.index(default)] = ast.Constant(value=None)
    else:
        for index, kw_default in enumerate(func.args.kw_defaults):
            if kw_default is default:
                func.args.kw_defaults[index] = ast.Constant(value=None)

    # 函数体内初始化
    guard = ast.If(
        test=ast.Compare(
            left=ast.Name(id=arg.arg, ctx=ast.Load()),
            ops=[ast.Is()],
            comparators=[ast.Constant(value=None)],
        ),
        body=[ast.Assign(targets=[ast.Name(id=arg.arg, ctx=ast.Store())], value=literal)],
        orelse=[],
    )
    _insert_after_docstring(func.body, guard)
    return True


@fixer("bare-except")
def fix_bare_except(tree: ast.Module, finding: Finding, parents, ctx) -> bool:
    handler = finding.node
    if not isinstance(handler, ast.ExceptHandler) or handler.type is not None:
        return False
    handler.type = ast.Name(id="Exception", ctx=ast.Load())
    return True


@fixer("unused-import")
def fix_unused_import(tree: ast.Module, finding: Finding, parents, ctx) -> bool:
    statement = finding.node
    name = finding.symbol or ""
    if not isinstance(statement, (ast.Import, ast.ImportFrom)):
        return False
    # 关键：按**当前**源码重新判断是否仍未被使用（前面的修复可能刚刚用到它）
    used = used_names(tree)
    if name in used:
        return False
    remaining = []
    removed = False
    for alias in statement.names:
        bound = alias.asname or (alias.name.split(".")[0] if isinstance(statement, ast.Import) else alias.name)
        if bound == name and bound not in used:
            removed = True
            continue
        remaining.append(alias)
    if not removed:
        return False
    if remaining:
        statement.names = remaining
        return True
    parent = parents.get(statement)
    if isinstance(parent, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.Try)):
        body = parent.body
        if statement in body:
            body.remove(statement)
            return True
    return False


@fixer("open-without-encoding")
def fix_open_encoding(tree: ast.Module, finding: Finding, parents, ctx) -> bool:
    call = finding.node
    if not isinstance(call, ast.Call):
        return False
    if any(kw.arg == "encoding" for kw in call.keywords):
        return False
    call.keywords.append(ast.keyword(arg="encoding", value=ast.Constant(value="utf-8")))
    return True


@fixer("sql-injection")
def fix_sql_injection(tree: ast.Module, finding: Finding, parents, ctx) -> bool:
    binop = finding.node
    if not isinstance(binop, ast.BinOp) or not is_sql(getattr(binop, "left", None)):
        return False
    parent = parents.get(binop)
    if not isinstance(parent, ast.Call):
        return False
    if len(parent.args) != 1 or parent.args[0] is not binop:
        return False
    sql = binop.left.value
    # 同时处理裸占位符与带引号的占位符：'%s' -> ?，避免生成 WHERE name = '?'
    new_sql, count = re.subn(r"(['\"])%[sdfr]\1|%[sdfr]", "?", sql)
    if not count:
        return False
    right = binop.right
    params = right.elts if isinstance(right, ast.Tuple) else [right]
    if len(params) != count:
        return False
    parent.args[0] = ast.Constant(value=new_sql)
    parent.args.append(
        params[0] if len(params) == 1 and isinstance(right, ast.Tuple) else ast.Tuple(elts=list(params), ctx=ast.Load())
    )
    return True


@fixer("hardcoded-secret")
def fix_hardcoded_secret(tree: ast.Module, finding: Finding, parents, ctx) -> bool:
    statement = finding.node
    value = getattr(statement, "value", None)
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return False
    _ensure_import(tree, "os")
    statement.value = ast.Call(
        func=ast.Attribute(
            value=ast.Attribute(value=ast.Name(id="os", ctx=ast.Load()), attr="environ", ctx=ast.Load()),
            attr="get",
            ctx=ast.Load(),
        ),
        args=[ast.Constant(value=finding.symbol or "SECRET"), ast.Constant(value="")],
        keywords=[],
    )
    return True


@fixer("weak-hash")
def fix_weak_hash(tree: ast.Module, finding: Finding, parents, ctx) -> bool:
    call = finding.node
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in ("md5", "sha1"):
        func.attr = "sha256"
        return True
    return False


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def apply_fixes(
    source: str,
    *,
    severities: tuple[str, ...] = ("error",),
    categories: tuple[str, ...] | None = None,
    context: Mapping[str, Any] | None = None,
) -> FixOutcome:
    """对源码应用自动修复。

    Args:
        source: 原始源码。
        severities: 只修复这些严重度（默认只修 error，即“先修阻塞问题”）。
        categories: 限定类别（style/security），None 表示全部。
        context: 修复上下文（如 ``module_docstring``）。
    """
    context = dict(context or {})
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return FixOutcome(source=source, compiled=False, error=f"源码语法错误，无法修复: {exc}")

    # 关键：让规则在**同一棵** AST 上运行，这样 finding.node 才能被就地修改
    findings = analyze(source, list(categories) if categories else None, tree=tree)
    before = summarize(findings)
    parents = build_parent_map(tree)

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for finding in findings:
        if finding.severity not in severities:
            continue
        handler = FIXERS.get(finding.rule_id)
        if handler is None:
            skipped.append({**finding.to_dict(), "reason": "暂无自动修复实现"})
            continue
        try:
            ok = handler(tree, finding, parents, context)
            reason = "已应用" if ok else "该位置无法安全自动修复"
        except Exception as exc:  # noqa: BLE001 - 单条修复失败不影响其他修复
            ok, reason = False, f"修复异常: {type(exc).__name__}: {exc}"
        record = {**finding.to_dict(), "reason": reason}
        (applied if ok else skipped).append(record)

    if not applied:
        return FixOutcome(source=source, applied=[], skipped=skipped, before=before,
                          after=before, remaining=[f.to_dict() for f in findings])

    # 新插入的节点没有位置信息，unparse 需要 lineno，补齐后再重写
    ast.fix_missing_locations(tree)
    new_source = ast.unparse(tree)
    try:
        compile(new_source, "<agentflow-fixed>", "exec")
    except SyntaxError as exc:
        return FixOutcome(
            source=source,
            applied=[],
            skipped=skipped + [{"rule_id": "unparse", "reason": f"重写后语法校验失败: {exc}"}],
            compiled=False,
            error=str(exc),
            before=before,
            after=before,
            remaining=[f.to_dict() for f in findings],
        )

    after_findings = analyze(new_source, list(categories) if categories else None)
    return FixOutcome(
        source=new_source,
        applied=applied,
        skipped=skipped,
        compiled=True,
        before=before,
        after=summarize(after_findings),
        remaining=[f.to_dict() for f in after_findings],
    )
