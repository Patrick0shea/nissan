"""Lower an Opentrons Python protocol (API v2) to `Program`s by static evaluation over `ast`.

Runtime parameters (`add_parameters`, API >= 2.18) are handled as follows (D-013):
- Finite parameters (bool, or any `choices`) are enumerated. One Program is lowered per
  combination, with the default combination first.
- Interval parameters (int/float with minimum/maximum) become Z3 constants. Arithmetic on them
  builds Z3 terms that flow into step volumes.

A parameter-dependent value is fine as a volume. Anywhere a concrete value is needed (a branch
condition, a loop bound, an index, a labware name) it stops lowering. Symbolic control flow is M3.

Loops over `range(...)` or concrete lists are unrolled. `get_values` and the complex
liquid-handling commands (`transfer` etc.) are not modelled yet. The protocol is never executed
(D-002).

The first construct we cannot model stops lowering. The steps before it are still analysed, and
the stop is reported as `Program.unsupported`, so a partial analysis is never silent (D-011).
"""

import ast
import copy
import itertools
import operator
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import z3

from otverify import definitions
from otverify.model import (
    Aspirate,
    Context,
    Dispense,
    DropTip,
    LoadedLabware,
    LoadedPipette,
    LoadLiquid,
    ParamSpec,
    PickUpTip,
    Program,
    Unsupported,
    WellRef,
)
from otverify.smt import is_sym, smax, smin, to_real

MAX_STEPS = 200_000
MAX_ASSIGNMENTS = 256  # combinations of finite-parameter values lowered separately

# ProtocolContext / InstrumentContext methods that cannot change any liquid volume.
_CTX_NOOPS = {"comment", "delay", "pause", "home", "set_rail_lights"}
_PIPETTE_NOOPS = {"touch_tip", "home"}

_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_CMPOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}
_BUILTINS: dict[str, Callable[..., Any]] = {
    "range": lambda *a: list(range(*a)),
    "len": len,
    "min": min,
    "max": max,
    "int": int,
    "float": float,
    "round": round,
    "abs": abs,
    "sum": sum,
    "list": list,
    "zip": lambda *a: list(zip(*a, strict=False)),
    "enumerate": lambda *a: list(enumerate(*a)),
}

# add_* method -> positional parameter names, as in opentrons 9.0.0 ParameterContext.
_PARAM_METHODS = {
    "add_int": ["display_name", "variable_name", "default", "minimum", "maximum", "choices"],
    "add_float": ["display_name", "variable_name", "default", "minimum", "maximum", "choices"],
    "add_bool": ["display_name", "variable_name", "default"],
    "add_str": ["display_name", "variable_name", "default", "choices"],
}
_PARAM_TYPES: dict[str, tuple[type, ...]] = {
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
    "str": (str,),
}


class _Stop(Exception):
    def __init__(self, node: ast.AST, reason: str) -> None:
        super().__init__(reason)
        self.line = getattr(node, "lineno", 0)
        self.reason = reason


class _Ctx:
    """The ProtocolContext argument of run()."""


class _ParamCtx:
    """The ParameterContext argument of add_parameters()."""


class _Params:
    """`protocol.params`."""


@dataclass(frozen=True)
class _Labware:
    index: int


@dataclass(frozen=True)
class _Pipette:
    index: int


class _Liquid:
    """Result of define_liquid(); opaque."""


def _is_num(v: object) -> bool:
    return (isinstance(v, int | float) and not isinstance(v, bool)) or isinstance(v, z3.ArithRef)


def _has_sym(v: object) -> bool:
    if isinstance(v, list | tuple):
        return any(_has_sym(x) for x in v)
    return is_sym(v)


def _parse_api_level(v: object) -> tuple[int, int] | None:
    if not isinstance(v, str):
        return None
    major, _, minor = v.partition(".")
    if major.isdigit() and minor.isdigit():
        return int(major), int(minor)
    return None


class _Lowerer:
    def __init__(self, prog: Program) -> None:
        self.prog = prog
        self.env: dict[str, Any] = {}
        self.params: dict[str, Any] | None = None  # name -> concrete value or Z3 constant
        self.param_specs: list[ParamSpec] = []
        self.context: list[tuple[str, object]] = []
        self.defs: list[definitions.LabwareDef] = []
        self.pip_defs: list[definitions.PipetteDef] = []
        self.last_location: dict[int, WellRef] = {}

    # ---- statements -------------------------------------------------------------------------

    def body(self, stmts: list[ast.stmt]) -> None:
        for s in stmts:
            self.stmt(s)

    def stmt(self, s: ast.stmt) -> None:
        match s:
            case ast.Expr(value=ast.Constant()) | ast.Pass() | ast.Import() | ast.ImportFrom():
                pass
            case ast.Expr(value=ast.Call() as call):
                self.call(call)
            case ast.Assign(targets=[target], value=value):
                self.bind(target, self.expr(value))
            case ast.AugAssign(target=ast.Name(id=name) as target, op=op, value=value):
                self.bind(target, self.binop(s, op, self.lookup(target, name), self.expr(value)))
            case ast.For(target=target, iter=it, body=body, orelse=[]):
                items = self.expr(it)
                if not isinstance(items, list | tuple):
                    raise _Stop(it, "for-loop over a value that is not a concrete list or range")
                for item in items:
                    self.bind(target, item)
                    self.context.append((ast.unparse(target), _render(item)))
                    try:
                        self.body(body)
                    finally:
                        self.context.pop()
            case ast.If(test=test, body=body, orelse=orelse):
                self.body(body if self.truth(test, self.expr(test)) else orelse)
            case _:
                raise _Stop(s, f"statement `{type(s).__name__}` not modelled")

    def bind(self, target: ast.expr, value: Any) -> None:
        match target:
            case ast.Name(id=name):
                self.env[name] = value
            case ast.Tuple(elts=elts) | ast.List(elts=elts):
                if not isinstance(value, list | tuple) or len(value) != len(elts):
                    raise _Stop(target, "unpacking a value of unknown shape")
                for t, v in zip(elts, value, strict=True):
                    self.bind(t, v)
            case _:
                raise _Stop(target, f"assignment to `{ast.unparse(target)}` not modelled")

    # ---- expressions ------------------------------------------------------------------------

    def lookup(self, node: ast.AST, name: str) -> Any:
        if name in self.env:
            return self.env[name]
        raise _Stop(node, f"`{name}` has no statically known value")

    def truth(self, node: ast.expr, v: Any) -> bool:
        if is_sym(v):
            raise _Stop(
                node,
                f"branch on parameter-dependent condition `{ast.unparse(node)}` "
                "(symbolic control flow: M3)",
            )
        return bool(v)

    def expr(self, e: ast.expr) -> Any:
        match e:
            case ast.Constant(value=v):
                return v
            case ast.Name(id=name):
                return self.lookup(e, name)
            case ast.Attribute(value=base, attr=attr):
                return self.attribute(e, self.expr(base), attr)
            case ast.BinOp(left=left, op=op, right=right):
                return self.binop(e, op, self.expr(left), self.expr(right))
            case ast.UnaryOp(op=ast.USub(), operand=x):
                v = self.expr(x)
                if not _is_num(v):
                    raise _Stop(e, "negation of a non-number")
                return -v
            case ast.UnaryOp(op=ast.UAdd(), operand=x):
                return self.expr(x)
            case ast.UnaryOp(op=ast.Not(), operand=x):
                return not self.truth(x, self.expr(x))
            case ast.BoolOp(op=op, values=values):
                v: Any = None
                for node in values:
                    v = self.expr(node)
                    if self.truth(node, v) == isinstance(op, ast.Or):
                        return v
                return v
            case ast.Compare(left=left, ops=ops, comparators=rights):
                return self.compare(e, left, ops, rights)
            case ast.List(elts=elts) | ast.Tuple(elts=elts):
                return [self.expr(x) for x in elts]
            case ast.Dict(keys=keys, values=values) if None not in keys:
                return {self.expr(k): self.expr(v) for k, v in zip(keys, values, strict=True)}  # type: ignore[arg-type]
            case ast.Subscript(value=base, slice=index):
                return self.subscript(e, self.expr(base), index)
            case ast.Call():
                return self.call(e)
            case ast.ListComp(elt=elt, generators=[gen]) if not gen.is_async:
                return self.list_comp(elt, gen)
            case _:
                raise _Stop(e, f"expression `{ast.unparse(e)}` not modelled")

    def attribute(self, node: ast.AST, recv: Any, attr: str) -> Any:
        if isinstance(recv, _Ctx) and attr == "params":
            if self.params is None:
                raise _Stop(node, "`protocol.params` used, but there is no add_parameters()")
            return _Params()
        if isinstance(recv, _Params):
            if self.params is None or attr not in self.params:
                raise _Stop(node, f"no runtime parameter named `{attr}`")
            return self.params[attr]
        raise _Stop(node, f"attribute `{ast.unparse(node)}` not modelled")  # type: ignore[arg-type]

    def compare(
        self, node: ast.AST, left: ast.expr, ops: list[ast.cmpop], rights: list[ast.expr]
    ) -> Any:
        lhs = self.expr(left)
        symbolic: list[z3.BoolRef] = []
        for op, r in zip(ops, rights, strict=True):
            rhs = self.expr(r)
            if type(op) not in _CMPOPS:
                raise _Stop(node, f"comparison `{type(op).__name__}` not modelled")
            if is_sym(lhs) or is_sym(rhs):
                if not (_is_num(lhs) and _is_num(rhs)):
                    raise _Stop(node, "comparison of a parameter with a non-number not modelled")
                symbolic.append(_CMPOPS[type(op)](lhs, rhs))
            elif not _CMPOPS[type(op)](lhs, rhs):
                return False
            lhs = rhs
        return z3.And(*symbolic) if symbolic else True

    def binop(self, node: ast.AST, op: ast.operator, a: Any, b: Any) -> Any:
        if is_sym(a) or is_sym(b):
            if not (_is_num(a) and _is_num(b)):
                raise _Stop(node, "arithmetic on a parameter and a non-number")
            if isinstance(op, ast.Add | ast.Sub | ast.Mult):
                return _BINOPS[type(op)](a, b)
            if isinstance(op, ast.Div):
                if is_sym(b):
                    raise _Stop(node, "division by a parameter-dependent value not modelled")
                if b == 0:
                    raise _Stop(node, "division by zero")
                return to_real(a) / b
            raise _Stop(node, f"`{type(op).__name__}` on a parameter-dependent value not modelled")
        ok = (_is_num(a) and _is_num(b)) or (
            isinstance(op, ast.Add) and type(a) is type(b) and isinstance(a, list | str)
        )
        if not ok or type(op) not in _BINOPS:
            raise _Stop(node, f"operator `{type(op).__name__}` on these operands not modelled")
        try:
            return _BINOPS[type(op)](a, b)
        except ArithmeticError as exc:
            raise _Stop(node, f"arithmetic error: {exc}") from None

    def subscript(self, node: ast.AST, base: Any, index: ast.expr) -> Any:
        if isinstance(index, ast.Slice):
            parts = (index.lower, index.upper, index.step)
            bounds = [None if x is None else self.expr(x) for x in parts]
            if _has_sym(bounds):
                raise _Stop(node, "slice bound depends on a parameter (M3)")
            if not isinstance(base, list) or not all(
                b is None or isinstance(b, int) for b in bounds
            ):
                raise _Stop(node, "slice not modelled")
            return base[slice(*bounds)]
        key = self.expr(index)
        if is_sym(key):
            raise _Stop(node, "index depends on a parameter (M3)")
        if isinstance(base, _Labware):
            return self.well(node, base, key)
        if isinstance(base, list | tuple) and isinstance(key, int) and not isinstance(key, bool):
            if not -len(base) <= key < len(base):
                raise _Stop(node, f"index {key} out of range for a list of length {len(base)}")
            return base[key]
        if isinstance(base, dict) and key in base:
            return base[key]
        raise _Stop(node, "subscript not modelled")

    def well(self, node: ast.AST, lw: _Labware, name: object) -> WellRef:
        if not isinstance(name, str) or name not in self.defs[lw.index].capacities:
            load_name = self.prog.labware[lw.index].load_name
            raise _Stop(node, f"well {name!r} does not exist in {load_name}")
        return WellRef(lw.index, name)

    def list_comp(self, elt: ast.expr, gen: ast.comprehension) -> list[Any]:
        items = self.expr(gen.iter)
        if not isinstance(items, list | tuple):
            raise _Stop(gen.iter, "comprehension over a value that is not a concrete list")
        saved = dict(self.env)
        out = []
        try:
            for item in items:
                self.bind(gen.target, item)
                if all(self.truth(c, self.expr(c)) for c in gen.ifs):
                    out.append(self.expr(elt))
        finally:
            self.env = saved
        return out

    # ---- calls ------------------------------------------------------------------------------

    def args(self, call: ast.Call, names: list[str]) -> dict[str, ast.expr]:
        """Map positional and keyword arguments to parameter names (unevaluated)."""
        starred = any(isinstance(a, ast.Starred) for a in call.args)
        if starred or any(k.arg is None for k in call.keywords):
            raise _Stop(call, "*args / **kwargs not modelled")
        if len(call.args) > len(names):
            raise _Stop(call, "too many positional arguments")
        bound = dict(zip(names, call.args, strict=False))
        for k in call.keywords:
            bound[k.arg] = k.value  # type: ignore[index]
        return bound

    def builtin(self, call: ast.Call, name: str) -> Any:
        if call.keywords:
            raise _Stop(call, f"keyword arguments to {name}() not modelled")
        args = [self.expr(a) for a in call.args]
        if _has_sym(args):
            if name in ("min", "max") and len(args) >= 2 and all(_is_num(a) for a in args):
                fold = smin if name == "min" else smax
                result = args[0]
                for a in args[1:]:
                    result = fold(result, a)
                return result
            if name == "abs" and len(args) == 1:
                return z3.If(args[0] >= 0, args[0], -args[0])
            if name == "float" and len(args) == 1:
                return to_real(args[0])
            raise _Stop(call, f"`{name}()` of a parameter-dependent value (M3)")
        try:
            return _BUILTINS[name](*args)
        except (TypeError, ValueError) as exc:
            raise _Stop(call, f"{name}() on these arguments: {exc}") from None

    def call(self, call: ast.Call) -> Any:
        func = call.func
        if isinstance(func, ast.Name):
            if func.id in _BUILTINS and func.id not in self.env:
                return self.builtin(call, func.id)
            raise _Stop(call, f"call to `{func.id}` not modelled")
        if not isinstance(func, ast.Attribute):
            raise _Stop(call, "call target not modelled")
        recv = self.expr(func.value)
        method = func.attr
        if isinstance(recv, _Ctx):
            return self.ctx_call(call, method)
        if isinstance(recv, _ParamCtx):
            return self.param_call(call, method)
        if isinstance(recv, _Pipette):
            return self.pipette_call(call, recv, method)
        if isinstance(recv, _Labware):
            return self.labware_call(call, recv, method)
        if isinstance(recv, WellRef):
            return self.well_call(call, recv, method)
        if isinstance(recv, list) and method in ("append", "extend") and len(call.args) == 1:
            getattr(recv, method)(self.expr(call.args[0]))
            return None
        raise _Stop(call, f"method `.{method}()` not modelled")

    def param_call(self, call: ast.Call, method: str) -> None:
        if method == "add_csv_file":
            raise _Stop(call, "CSV runtime parameters not modelled")
        if method not in _PARAM_METHODS:
            raise _Stop(call, f"`parameters.{method}()` not modelled")
        a = {k: self.expr(v) for k, v in self.args(call, _PARAM_METHODS[method]).items()}
        kind = method.removeprefix("add_")
        name, default = a.get("variable_name"), a.get("default")
        if not isinstance(name, str) or any(p.name == name for p in self.param_specs):
            raise _Stop(call, f"missing or duplicate variable_name {name!r}")
        types = _PARAM_TYPES[kind]
        if not isinstance(default, types) or (kind != "bool" and isinstance(default, bool)):
            raise _Stop(call, f"default of `{name}` has the wrong type (ParameterValueError)")
        minimum, maximum, raw_choices = a.get("minimum"), a.get("maximum"), a.get("choices")
        choices: tuple[object, ...] | None = None
        if kind == "bool":
            choices = (False, True)
        elif raw_choices is not None:
            if minimum is not None or maximum is not None:
                raise _Stop(
                    call, f"`{name}` has both choices and min/max (ParameterDefinitionError)"
                )
            if not isinstance(raw_choices, list) or not all(
                isinstance(c, dict) and isinstance(c.get("value"), types) for c in raw_choices
            ):
                raise _Stop(call, f"malformed choices for `{name}` (ParameterDefinitionError)")
            choices = tuple(c["value"] for c in raw_choices)
            if default not in choices:
                raise _Stop(call, f"default of `{name}` is not one of its choices")
        elif kind in ("int", "float") and isinstance(minimum, types) and isinstance(maximum, types):
            if not minimum <= default <= maximum:
                raise _Stop(call, f"default of `{name}` is outside [{minimum}, {maximum}]")
        else:
            raise _Stop(
                call,
                f"`{name}` needs choices, or both minimum and maximum (ParameterDefinitionError)",
            )
        self.param_specs.append(
            ParamSpec(name, kind, default, minimum, maximum, choices, call.lineno)
        )

    def ctx_call(self, call: ast.Call, method: str) -> Any:
        if method in _CTX_NOOPS:
            return None
        if method == "define_liquid":
            return _Liquid()
        if method == "load_labware":
            a = self.args(call, ["load_name", "location", "label", "namespace", "version"])
            if "load_name" not in a or "location" not in a:
                raise _Stop(call, "load_labware without load_name/location")
            load_name, location = self.expr(a["load_name"]), self.expr(a["location"])
            if is_sym(location):
                raise _Stop(call, "deck slot depends on a parameter")
            lw_def = definitions.labware(load_name) if isinstance(load_name, str) else None
            if lw_def is None:
                raise _Stop(call, f"labware {load_name!r} is not in the definitions snapshot")
            self.prog.labware.append(LoadedLabware(load_name, str(location), call.lineno))
            self.defs.append(lw_def)
            return _Labware(len(self.prog.labware) - 1)
        if method == "load_instrument":
            return self.load_instrument(call)
        raise _Stop(call, f"`protocol.{method}()` not modelled")

    def load_instrument(self, call: ast.Call) -> _Pipette:
        a = self.args(call, ["instrument_name", "mount", "tip_racks", "replace"])
        name = self.expr(a["instrument_name"]) if "instrument_name" in a else None
        pip_def = definitions.pipette(name) if isinstance(name, str) else None
        if pip_def is None:
            raise _Stop(call, f"pipette {name!r} is not in the definitions snapshot")
        mount = self.expr(a["mount"]) if "mount" in a else None
        racks = self.expr(a["tip_racks"]) if "tip_racks" in a else []
        if not isinstance(racks, list) or not all(isinstance(r, _Labware) for r in racks):
            raise _Stop(call, "tip_racks is not a list of loaded labware")
        tip_caps = {self.defs[r.index].capacities[self.defs[r.index].wells()[0]] for r in racks}
        if len(tip_caps) > 1:
            raise _Stop(call, "tip racks with different tip capacities not modelled")
        tip_capacity = min([pip_def.max_volume, *tip_caps])
        self.prog.pipettes.append(
            LoadedPipette(
                name,
                str(mount),
                tuple(r.index for r in racks),
                pip_def.channels,
                tip_capacity,
                call.lineno,
            )
        )
        self.pip_defs.append(pip_def)
        return _Pipette(len(self.prog.pipettes) - 1)

    def labware_call(self, call: ast.Call, lw: _Labware, method: str) -> Any:
        lw_def = self.defs[lw.index]
        if call.args or call.keywords:
            raise _Stop(call, f"`labware.{method}()` with arguments not modelled")
        if method == "wells":
            return [WellRef(lw.index, w) for w in lw_def.wells()]
        if method == "columns":
            return [[WellRef(lw.index, w) for w in col] for col in lw_def.columns()]
        if method == "rows":
            return [[WellRef(lw.index, w) for w in row] for row in lw_def.rows()]
        if method == "wells_by_name":
            return {w: WellRef(lw.index, w) for w in lw_def.wells()}
        raise _Stop(call, f"`labware.{method}()` not modelled")

    def well_call(self, call: ast.Call, well: WellRef, method: str) -> Any:
        if method in ("top", "bottom", "center"):
            return well  # a position inside the well; the well is what matters for volumes
        if method == "load_liquid":
            a = self.args(call, ["liquid", "volume"])
            volume = self.expr(a["volume"]) if "volume" in a else None
            if not _is_num(volume):
                raise _Stop(call, "load_liquid volume is not a number")
            self.emit(LoadLiquid(call.lineno, self.ctx(), well, _num(volume)))
            return None
        raise _Stop(call, f"`well.{method}()` not modelled")

    def pipette_call(self, call: ast.Call, pip: _Pipette, method: str) -> _Pipette:
        if method in _PIPETTE_NOOPS:
            return pip
        if method == "pick_up_tip":
            self.emit(PickUpTip(call.lineno, self.ctx(), pip.index))
        elif method in ("drop_tip", "return_tip"):
            self.emit(DropTip(call.lineno, self.ctx(), pip.index))
        elif method == "move_to":
            a = self.args(call, ["location"])
            self.last_location[pip.index] = self.location(call, pip, a.get("location"))
        elif method in ("aspirate", "dispense"):
            a = self.args(call, ["volume", "location"])
            volume = self.expr(a["volume"]) if "volume" in a else None
            if volume is not None and not _is_num(volume):
                raise _Stop(call, f"{method} volume is not a number")
            well = self.location(call, pip, a.get("location"))
            self.last_location[pip.index] = well
            wells = self.channel_wells(call, pip, well)
            vol = None if volume is None else _num(volume)
            cls = Aspirate if method == "aspirate" else Dispense
            self.emit(cls(call.lineno, self.ctx(), pip.index, wells, vol))
        else:
            raise _Stop(call, f"`pipette.{method}()` not modelled yet")
        return pip

    def location(self, call: ast.Call, pip: _Pipette, node: ast.expr | None) -> WellRef:
        if node is None:
            if pip.index not in self.last_location:
                raise _Stop(call, "no location given and no previous location for this pipette")
            return self.last_location[pip.index]
        loc = self.expr(node)
        if not isinstance(loc, WellRef):
            raise _Stop(node, "location is not a well (trash and Location objects not modelled)")
        return loc

    def channel_wells(self, call: ast.Call, pip: _Pipette, well: WellRef) -> tuple[WellRef, ...]:
        channels = self.pip_defs[pip.index].channels
        if channels == 1:
            return (well,)
        column = next(c for c in self.defs[well.labware].ordering if well.well in c)
        if len(column) == 1:  # e.g. a reservoir trough: every channel lands in the same well
            return (well,) * channels
        if len(column) == channels and column[0] == well.well:
            return tuple(WellRef(well.labware, w) for w in column)
        raise _Stop(call, f"{channels}-channel access to {well.well} of this labware not modelled")

    # ---- helpers ----------------------------------------------------------------------------

    def ctx(self) -> Context:
        return tuple(self.context)

    def emit(self, step: Any) -> None:
        if len(self.prog.steps) >= MAX_STEPS:
            raise _Stop(ast.Pass(lineno=step.line), f"more than {MAX_STEPS} steps")
        self.prog.steps.append(step)


def _num(v: Any) -> Any:
    return v if is_sym(v) else float(v)


def _render(v: object) -> object:
    if isinstance(v, WellRef):
        return v.well
    if isinstance(v, int | float | str | bool) or v is None:
        return v
    if is_sym(v):
        return str(v)
    return type(v).__name__


def _assignments(specs: list[ParamSpec]) -> list[dict[str, object]]:
    """All combinations of finite-parameter values, the all-defaults combination first."""
    finite = [p for p in specs if p.finite]
    options = [[p.default, *(c for c in p.choices or () if c != p.default)] for p in finite]
    return [
        {p.name: v for p, v in zip(finite, combo, strict=True)}
        for combo in itertools.product(*options)
    ]


def _symbol(p: ParamSpec) -> z3.ArithRef:
    return z3.Int(p.name) if p.kind == "int" else z3.Real(p.name)


def lower(source: str) -> list[Program]:
    """Lower protocol source to one Program per finite-parameter assignment.

    Raises SyntaxError if the source does not parse.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)  # e.g. invalid escapes in corpus strings
        tree = ast.parse(source)
    base = _Lowerer(Program())
    run: ast.FunctionDef | None = None
    add_parameters: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            run = node
        elif isinstance(node, ast.FunctionDef) and node.name == "add_parameters":
            add_parameters = node
        elif isinstance(node, ast.Assign):
            # Module-level constants and the metadata/requirements dicts. Anything we cannot
            # evaluate is left unbound; a later use of it stops lowering with a clear reason.
            try:
                base.stmt(node)
            except _Stop:
                pass
    api_level = None
    for meta in ("requirements", "metadata"):
        d = base.env.get(meta)
        if isinstance(d, dict) and "apiLevel" in d:
            api_level = _parse_api_level(d["apiLevel"])
            break

    def failed(line: int, reason: str) -> list[Program]:
        return [Program(api_level=api_level, unsupported=Unsupported(line, reason))]

    if api_level is None:
        return failed(1, "no valid apiLevel in metadata or requirements")
    if run is None or len(run.args.args) != 1:
        return failed(1, "no `run(protocol)` function")

    specs: list[ParamSpec] = []
    if add_parameters is not None:
        if api_level < (2, 18):
            return failed(add_parameters.lineno, "add_parameters() requires apiLevel >= 2.18")
        if len(add_parameters.args.args) != 1:
            return failed(add_parameters.lineno, "add_parameters() must take one argument")
        reader = _Lowerer(Program())
        reader.env = copy.deepcopy(base.env)
        reader.env[add_parameters.args.args[0].arg] = _ParamCtx()
        try:
            reader.body(add_parameters.body)
        except _Stop as stop:
            return failed(stop.line, stop.reason)
        specs = reader.param_specs

    assignments = _assignments(specs)
    if len(assignments) > MAX_ASSIGNMENTS:
        return failed(
            add_parameters.lineno if add_parameters else 1,
            f"{len(assignments)} combinations of finite parameters (limit {MAX_ASSIGNMENTS})",
        )
    symbols = {p.name: _symbol(p) for p in specs if not p.finite}

    programs = []
    for assignment in assignments:
        prog = Program(
            api_level=api_level, params=tuple(specs), assignment=assignment, symbols=symbols
        )
        lowerer = _Lowerer(prog)
        lowerer.env = copy.deepcopy(base.env)
        lowerer.env[run.args.args[0].arg] = _Ctx()
        if add_parameters is not None:
            lowerer.params = {**assignment, **symbols}
        try:
            lowerer.body(run.body)
        except _Stop as stop:
            prog.unsupported = Unsupported(stop.line, stop.reason)
        except RecursionError:
            prog.unsupported = Unsupported(run.lineno, "expression nesting too deep")
        programs.append(prog)
    return programs
