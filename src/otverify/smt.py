"""Helpers for values that are either concrete Python numbers or Z3 terms over parameters."""

from fractions import Fraction

import z3

Num = float | int | z3.ArithRef
Cond = bool | z3.BoolRef


def is_sym(v: object) -> bool:
    return isinstance(v, z3.ExprRef)


def ite(cond: Cond, a: Num, b: Num) -> Num:
    if isinstance(cond, bool):
        return a if cond else b
    return z3.If(cond, a, b)


def smin(a: Num, b: Num) -> Num:
    if is_sym(a) or is_sym(b):
        return z3.If(a <= b, a, b)  # type: ignore[operator]
    return min(a, b)


def smax(a: Num, b: Num) -> Num:
    if is_sym(a) or is_sym(b):
        return z3.If(a >= b, a, b)  # type: ignore[operator]
    return max(a, b)


def to_real(v: Num) -> Num:
    """Coerce to a real so that `/` is true division (z3 `/` on two Ints is integer division)."""
    if isinstance(v, z3.ArithRef) and v.is_int():
        return z3.ToReal(v)
    return v


def value_of(v: z3.ExprRef) -> int | float:
    """A Python number from a Z3 numeral (as produced by model evaluation)."""
    if z3.is_int_value(v):
        return v.as_long()
    if z3.is_rational_value(v):
        frac = Fraction(v.numerator_as_long(), v.denominator_as_long())
        return int(frac) if frac.denominator == 1 else float(frac)
    if z3.is_algebraic_value(v):
        return float(Fraction(v.approx(12).as_fraction()))
    raise ValueError(f"not a numeral: {v}")


def free_symbols(e: z3.ExprRef) -> set[str]:
    """Names of the uninterpreted constants (parameters) occurring in `e`."""
    seen: set[str] = set()
    todo = [e]
    visited: set[int] = set()
    while todo:
        x = todo.pop()
        if x.get_id() in visited:
            continue
        visited.add(x.get_id())
        if z3.is_const(x) and x.decl().kind() == z3.Z3_OP_UNINTERPRETED:
            seen.add(x.decl().name())
        todo.extend(x.children())
    return seen


def bound(v: z3.ExprRef) -> tuple[Fraction, bool] | None:
    """Decode an Optimize bound: (value, is_open), or None if unbounded or not decodable.

    z3 reports a strict optimum as `c + epsilon` / `c + -1*epsilon`.
    """
    if z3.is_rational_value(v) or z3.is_int_value(v):
        return Fraction(str(v.as_fraction() if z3.is_rational_value(v) else v.as_long())), False
    text = str(v)
    if "oo" in text:
        return None
    if z3.is_add(v):
        base = [c for c in v.children() if z3.is_rational_value(c) or z3.is_int_value(c)]
        if len(base) == 1 and "epsilon" in text:
            return bound(base[0])[0], True  # type: ignore[index]
    if text in ("epsilon", "-1*epsilon"):
        return Fraction(0), True
    return None
