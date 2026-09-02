"""산술식 계산기. LLM 이 숫자를 직접 계산하다 틀리는 걸 막는다.

【ast.literal_eval 을 쓰지 않는 이유】
literal_eval 은 **리터럴만** 평가한다. '1+1' 같은 산술식은 ValueError 로 죽는다
(파이썬 3.12 기준 BinOp 를 통과시키지 않는다). 그래서 못 쓴다.

대신 ast.parse(mode="eval") 로 트리를 만들고 **노드 화이트리스트**로 걷는다.
허용한 노드 밖의 것(이름·호출·속성·첨자·문자열…)을 만나면 그 자리에서 거절한다.
`__import__('os').system('ls')` 는 Call 노드라 여기서 막힌다.

【폭탄 방어】
거듭제곱은 계산 전에 지수·밑을 본다. 2**999999999 는 값을 만들기 시작하면
프로세스가 몇 분간 멈추고 메모리를 다 먹는다. 평가 중 프로세스가 멈추면
그 문항만 0점이 아니라 **평가 전체가 죽는다.**
"""
from __future__ import annotations

import ast
import math

MAX_EXPR = 500          # 입력 길이 상한
MAX_POW_EXP = 8         # 지수 상한
MAX_POW_BASE = 1e6      # 밑 상한
MAX_ABS = 1e30          # 결과 절댓값 상한

_BIN = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
}
_UNARY = {ast.USub: lambda a: -a, ast.UAdd: lambda a: +a}


class CalcError(ValueError):
    """계산을 거절한 이유. 사용자에게 그대로 보여줄 문장."""


def _num(node):
    """숫자 리터럴만 값으로 인정한다. True/False, 문자열, 복소수는 숫자가 아니다."""
    if not isinstance(node, ast.Constant):
        raise CalcError(f"숫자가 아닌 값이 있다: {type(node).__name__}")
    v = node.value
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise CalcError(f"숫자 리터럴만 쓸 수 있다: {v!r}")
    return v


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        return _num(node)
    if isinstance(node, ast.UnaryOp):
        op = _UNARY.get(type(node.op))
        if op is None:
            raise CalcError(f"허용하지 않는 단항 연산: {type(node.op).__name__}")
        return op(_eval(node.operand))
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left), _eval(node.right)
        t = type(node.op)
        if t is ast.Pow:
            # 계산 **하기 전에** 막는다. 만들고 나서 재면 이미 늦다
            if abs(right) > MAX_POW_EXP:
                raise CalcError(f"지수가 너무 크다({right}). {MAX_POW_EXP} 이하만 계산한다")
            if abs(left) > MAX_POW_BASE:
                raise CalcError(f"밑이 너무 크다({left}). {MAX_POW_BASE:g} 이하만 계산한다")
            return left ** right
        if t in (ast.Div, ast.FloorDiv, ast.Mod) and right == 0:
            raise CalcError("0 으로 나눌 수 없다")
        op = _BIN.get(t)
        if op is None:
            raise CalcError(f"허용하지 않는 연산: {t.__name__}")
        return op(left, right)
    raise CalcError(f"허용하지 않는 문법: {type(node).__name__}")


def calculate(expression=None, **_ignored) -> dict:
    """산술식 하나를 계산한다. 어떤 입력에도 예외를 던지지 않는다.

    콤마는 지운다 — 공시 숫자는 '3,477,640' 처럼 들어오는 게 정상이다.
    """
    expr = "" if expression is None else str(expression)
    out = {"status": "error", "value": None, "expression": expr,
           "results": [], "sources": [], "note": ""}
    if not expr.strip():
        out["note"] = "expression 이 비어 있다"
        return out
    if len(expr) > MAX_EXPR:
        out["note"] = f"식이 너무 길다({len(expr)}자). {MAX_EXPR}자 이하만 계산한다"
        return out

    clean = expr.replace(",", "").replace(" ", " ").strip()
    out["expression"] = clean
    try:
        tree = ast.parse(clean, mode="eval")
    except SyntaxError as e:
        out["note"] = f"식을 읽을 수 없다: {e.msg}"
        return out

    try:
        value = _eval(tree)
    except CalcError as e:
        out["note"] = str(e)
        return out
    except (OverflowError, ZeroDivisionError, ValueError) as e:
        out["note"] = f"계산 실패: {type(e).__name__}: {e}"
        return out

    if isinstance(value, complex) or not math.isfinite(value):
        out["note"] = "결과가 유한한 수가 아니다"
        return out
    if abs(value) > MAX_ABS:
        out["note"] = f"결과가 너무 크다(|{value:.3g}| > {MAX_ABS:g})"
        return out

    out["status"] = "ok"
    out["value"] = value
    out["note"] = f"{clean} = {value:,.6g}" if isinstance(value, float) else f"{clean} = {value:,}"
    return out
