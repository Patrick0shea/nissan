"""Lower an Opentrons Python protocol (API v2) to a `Program` by static evaluation over `ast`.

M1 scope: constant values only. Loops over `range(...)` or concrete lists are unrolled. Runtime
parameters, `get_values` and the complex liquid-handling commands (`transfer` etc.) are not
modelled yet. The protocol is never executed (D-002).

The first construct we cannot model stops lowering. The steps before it are still analysed, and
the stop is reported as `Program.unsupported`, so a partial analysis is never silent.
"""

import ast
import operator
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from otverify import definitions
from otverify.model import (
    Aspirate,
    Context,
    Dispense,
    DropTip,
    LoadedLabware,
    LoadedPipette,
    LoadLiquid,
    PickUpTip,
    Program,
    Unsupported,
    WellRef,
)

MAX_STEPS = 200_000

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
_CMPOPS: dict[type, Callable[[Any, Any], bool]] = {
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


class _Stop(Exception):
    def __init__(self, node: ast.AST, reason: str) -> None:
        super().__init__(reason)
        self.line = getattr(node, "lineno", 0)
        self.reason = reason


class _Ctx:
    """The ProtocolContext argument of run()."""


@dataclass(frozen=True)
class _Labware:
    index: int


@dataclass(frozen=True)
class _Pipette:
    index: int


class _Liquid:
    """Result of define_liquid(); opaque."""


def _is_num(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


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
                self.body(body if self.expr(test) else orelse)
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

    def expr(self, e: ast.expr) -> Any:
        match e:
            case ast.Constant(value=v):
                return v
            case ast.Name(id=name):
                return self.lookup(e, name)
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
                return not self.expr(x)
            case ast.BoolOp(op=ast.And(), values=values):
                return all(self.expr(v) for v in values)
            case ast.BoolOp(op=ast.Or(), values=values):
                return any(self.expr(v) for v in values)
            case ast.Compare(left=left, ops=ops, comparators=rights):
                lhs = self.expr(left)
                for op, r in zip(ops, rights, strict=True):
                    rhs = self.expr(r)
                    if type(op) not in _CMPOPS:
                        raise _Stop(e, f"comparison `{type(op).__name__}` not modelled")
                    if not _CMPOPS[type(op)](lhs, rhs):
                        return False
                    lhs = rhs
                return True
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

    def binop(self, node: ast.AST, op: ast.operator, a: Any, b: Any) -> Any:
        ok = (_is_num(a) and _is_num(b)) or (
            isinstance(op, ast.Add) and type(a) is type(b) and isinstance(a, list | str)
        )
        if not ok or type(op) not in _BINOPS:
            raise _Stop(node, f"operator `{type(op).__name__}` on these operands not modelled")
        return _BINOPS[type(op)](a, b)

    def subscript(self, node: ast.AST, base: Any, index: ast.expr) -> Any:
        if isinstance(index, ast.Slice):
            bounds = [
                None if x is None else self.expr(x) for x in (index.lower, index.upper, index.step)
            ]
            if not isinstance(base, list) or not all(
                b is None or isinstance(b, int) for b in bounds
            ):
                raise _Stop(node, "slice not modelled")
            return base[slice(*bounds)]
        key = self.expr(index)
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
                if all(self.expr(c) for c in gen.ifs):
                    out.append(self.expr(elt))
        finally:
            self.env = saved
        return out

    # ---- calls ------------------------------------------------------------------------------

    def args(self, call: ast.Call, names: list[str]) -> dict[str, ast.expr]:
        """Map positional and keyword arguments to parameter names (unevaluated)."""
        if any(isinstance(a, ast.Starred) for a in call.args) or any(
            k.arg is None for k in call.keywords
        ):
            raise _Stop(call, "*args / **kwargs not modelled")
        if len(call.args) > len(names):
            raise _Stop(call, "too many positional arguments")
        bound = dict(zip(names, call.args, strict=False))
        for k in call.keywords:
            bound[k.arg] = k.value  # type: ignore[index]
        return bound

    def call(self, call: ast.Call) -> Any:
        func = call.func
        if isinstance(func, ast.Name):
            if func.id in _BUILTINS and func.id not in self.env:
                if call.keywords:
                    raise _Stop(call, f"keyword arguments to {func.id}() not modelled")
                try:
                    return _BUILTINS[func.id](*(self.expr(a) for a in call.args))
                except (TypeError, ValueError) as exc:
                    raise _Stop(call, f"{func.id}() on these arguments: {exc}") from None
            raise _Stop(call, f"call to `{func.id}` not modelled")
        if not isinstance(func, ast.Attribute):
            raise _Stop(call, "call target not modelled")
        recv = self.expr(func.value)
        method = func.attr
        if isinstance(recv, _Ctx):
            return self.ctx_call(call, method)
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
            self.emit(LoadLiquid(call.lineno, self.ctx(), well, float(volume)))
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
            vol = None if volume is None else float(volume)
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


def _render(v: object) -> object:
    if isinstance(v, WellRef):
        return v.well
    if isinstance(v, int | float | str | bool) or v is None:
        return v
    return type(v).__name__


def lower(source: str) -> Program:
    """Lower protocol source to a Program. Raises SyntaxError if the source does not parse."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)  # e.g. invalid escapes in corpus strings
        tree = ast.parse(source)
    prog = Program()
    lowerer = _Lowerer(prog)
    run: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            run = node
        elif isinstance(node, ast.Assign):
            # Module-level constants and the metadata/requirements dicts. Anything we cannot
            # evaluate is left unbound; a later use of it stops lowering with a clear reason.
            try:
                lowerer.stmt(node)
            except _Stop:
                pass
    for meta in ("requirements", "metadata"):
        d = lowerer.env.get(meta)
        if isinstance(d, dict) and "apiLevel" in d:
            prog.api_level = _parse_api_level(d["apiLevel"])
            break
    if prog.api_level is None:
        prog.unsupported = Unsupported(1, "no valid apiLevel in metadata or requirements")
        return prog
    if run is None or len(run.args.args) != 1:
        prog.unsupported = Unsupported(1, "no `run(protocol)` function")
        return prog
    lowerer.env[run.args.args[0].arg] = _Ctx()
    try:
        lowerer.body(run.body)
    except _Stop as stop:
        prog.unsupported = Unsupported(stop.line, stop.reason)
    except RecursionError:
        prog.unsupported = Unsupported(run.lineno, "expression nesting too deep")
    return prog
