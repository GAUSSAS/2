"""安全的表达式求值器（用于边的 when 条件）。

为什么不用 ``eval``？
--------------------
工作流定义是**外部输入**（JSON 文件，将来可能来自可视化编辑器）。
直接 ``eval`` 等于把任意代码执行权交给工作流作者。
这里基于 ``ast`` 做白名单求值：只允许字面量、布尔运算、比较、算术、
少量安全内置函数和 *点号路径*（``review_style.error_count > 0``）。

支持的语法::

    字面量      "pass" / 3 / 3.5 / True / None / [] / {}
    路径        state.verdict, inputs.topic, code.version, review.error_count
    比较        == != < <= > >= in not in
    布尔        and or not
    算术        + - * / // % **   （仅数值）
    三元        a if cond else b
    函数        len() min() max() sum() abs() any() all() int() str() float() bool()
             以及 contains(text, sub) / startswith(text, prefix) / endswith(text, suffix)

示例::

    >>> safe_eval("verdict == 'fail' and attempts < 3", {"verdict": "fail", "attempts": 1})
    True
    >>> safe_eval("len(findings) > 0", {"findings": [{}, {}]})
    True
"""

from __future__ import annotations

import ast
import operator
from typing import Any, Callable, Mapping

from .errors import ExpressionError

_BIN_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_CMP_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}

_UNARY_OPS: dict[type, Callable[[Any], Any]] = {
    ast.Not: operator.not_,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _contains(text: Any, sub: Any) -> bool:
    return str(sub).lower() in str(text).lower()


def _startswith(text: Any, prefix: Any) -> bool:
    return str(text).lower().startswith(str(prefix).lower())


def _endswith(text: Any, suffix: Any) -> bool:
    return str(text).lower().endswith(str(suffix).lower())


_SAFE_FUNCS: dict[str, Callable[..., Any]] = {
    "len": len,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "any": any,
    "all": all,
    "int": int,
    "str": str,
    "float": float,
    "bool": bool,
    "round": round,
    "contains": _contains,
    "startswith": _startswith,
    "endswith": _endswith,
}


class _Evaluator:
    def __init__(self, context: Mapping[str, Any]) -> None:
        self.context = dict(context)

    # ---- 路径解析 -------------------------------------------------
    def resolve(self, node: ast.AST) -> Any:
        """把 ``a.b.c`` 解析为上下文里以 ``"a.b.c"`` 为键的值（或嵌套 dict 查找）。"""
        if isinstance(node, ast.Name):
            if node.id not in self.context:
                raise ExpressionError(f"未知变量: {node.id!r}")
            return self.context[node.id]
        if isinstance(node, ast.Attribute):
            path = _dotted(node)
            if path in self.context:
                return self.context[path]
            # 退化：先取根对象的属性链
            base = self.resolve(node.value)
            if isinstance(base, Mapping) and node.attr in base:
                return base[node.attr]
            raise ExpressionError(f"未知变量: {path!r}")
        if isinstance(node, ast.Subscript):
            base = self.resolve(node.value)
            key = self.visit(node.slice)
            try:
                return base[key]
            except Exception as exc:  # noqa: BLE001 - 统一转换为 ExpressionError
                raise ExpressionError(f"下标访问失败: {exc}") from exc
        return self.visit(node)

    # ---- 通用求值 -------------------------------------------------
    def visit(self, node: ast.AST) -> Any:
        method = getattr(self, f"visit_{type(node).__name__}", None)
        if method is None:
            raise ExpressionError(f"表达式中不允许的语法: {type(node).__name__}")
        return method(node)

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id == "True":
            return True
        if node.id == "False":
            return False
        if node.id == "None":
            return None
        return self.resolve(node)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        return self.resolve(node)

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        return self.resolve(node)

    def visit_List(self, node: ast.List) -> list:
        return [self.visit(e) for e in node.elts]

    def visit_Tuple(self, node: ast.Tuple) -> tuple:
        return tuple(self.visit(e) for e in node.elts)

    def visit_Set(self, node: ast.Set) -> set:
        return {self.visit(e) for e in node.elts}

    def visit_Dict(self, node: ast.Dict) -> dict:
        return {self.visit(k): self.visit(v) for k, v in zip(node.keys, node.values)}

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ExpressionError(f"不支持的一元运算符: {type(node.op).__name__}")
        return op(self.visit(node.operand))

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            value: Any = True
            for operand in node.values:
                value = self.visit(operand)
                if not value:
                    return value
            return value
        if isinstance(node.op, ast.Or):
            value = False
            for operand in node.values:
                value = self.visit(operand)
                if value:
                    return value
            return value
        raise ExpressionError(f"不支持的布尔运算符: {type(node.op).__name__}")

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ExpressionError(f"不支持的二元运算符: {type(node.op).__name__}")
        return op(self.visit(node.left), self.visit(node.right))

    def visit_Compare(self, node: ast.Compare) -> bool:
        left = self.visit(node.left)
        for op_node, comparator in zip(node.ops, node.comparators):
            op = _CMP_OPS.get(type(op_node))
            if op is None:
                raise ExpressionError(f"不支持的比较运算符: {type(op_node).__name__}")
            right = self.visit(comparator)
            try:
                if not op(left, right):
                    return False
            except TypeError as exc:
                raise ExpressionError(f"比较类型不匹配: {left!r} {type(op_node).__name__} {right!r}") from exc
            left = right
        return True

    def visit_IfExp(self, node: ast.IfExp) -> Any:
        return self.visit(node.body) if self.visit(node.test) else self.visit(node.orelse)

    def visit_Call(self, node: ast.Call) -> Any:
        if not isinstance(node.func, ast.Name):
            raise ExpressionError("只允许调用白名单内置函数")
        func = _SAFE_FUNCS.get(node.func.id)
        if func is None:
            raise ExpressionError(f"不允许调用的函数: {node.func.id!r}")
        if node.keywords:
            raise ExpressionError("条件表达式中的函数调用不支持关键字参数")
        return func(*[self.visit(a) for a in node.args])


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    else:
        raise ExpressionError("路径表达式只能由标识符和点组成")
    return ".".join(reversed(parts))


def compile_expression(expression: str) -> ast.Expression:
    """把表达式编译成 AST（仅做语法校验与安全检查）。"""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"表达式语法错误: {expression!r} ({exc.msg})") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp,
                             ast.GeneratorExp, ast.NamedExpr, ast.Await, ast.Yield)):
            raise ExpressionError(f"表达式中不允许的语法: {type(node).__name__}")
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AugAssign, ast.AnnAssign)):
            raise ExpressionError(f"表达式中不允许的语法: {type(node).__name__}")
        if isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            raise ExpressionError("表达式中不允许属性赋值")
    return tree


def safe_eval(expression: str, context: Mapping[str, Any], *, default: Any = None) -> Any:
    """在受限上下文中求值 ``expression``。

    Args:
        expression: 形如 ``"verdict == 'fail' and attempts < 3"`` 的条件。
        context: 扁平化的变量表，键可以用点号路径（``"review.error_count"``）。
        default: 供调用方自行处理缺省值；本函数不吞异常。

    Raises:
        ExpressionError: 语法非法、变量缺失或类型不匹配。
    """
    if expression is None or str(expression).strip() == "":
        return True
    tree = compile_expression(str(expression))
    return bool(_Evaluator(context).visit(tree))


def referenced_names(expression: str) -> list[str]:
    """返回表达式中引用的点号路径，用于工作流静态校验/依赖分析。"""
    tree = compile_expression(expression)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                names.append(_dotted(node))
        elif isinstance(node, ast.Name) and node.id not in _SAFE_FUNCS and node.id not in {"True", "False", "None"}:
            names.append(node.id)
    # 去重且保持顺序
    seen: set[str] = set()
    result = [n for n in names if not (n in seen or seen.add(n))]
    return result
