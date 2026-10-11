"""Lower an Opentrons Python protocol (API v2) to `Program`s by static evaluation over `ast`.

Parameters come from `add_parameters` (API >= 2.18), or from the legacy library mechanism
`get_values(...)` backed by `fields.json` (D-016, see `legacy.py`). Their domains are handled as
follows:
- Finite parameters (bool, choices, dropDown, default-only) are enumerated. One Program is lowered
  per combination, with the default combination first (D-013). Beyond MAX_ASSIGNMENTS
  combinations, each finite parameter is varied one at a time from the defaults, and the gap is
  reported (D-021).
- Interval parameters become Z3 constants. Arithmetic on them builds Z3 terms that flow into step
  volumes (D-013).
- A parameter-dependent branch condition is first decided over the whole domain with Z3 (D-021).
  If it is undecided and one branch only raises an exception (input validation), the run
  continues on the other branch under a path condition (D-021).
- When an int interval parameter reaches a position that needs a concrete value (a loop bound,
  index, undecided branch, divisor...), lowering restarts with that parameter enumerated, provided
  its domain is small (D-017). Floats and large domains stop lowering instead.

The evaluated Python subset: assignments, `for`/`while` loops (unrolled, with `break` and
`continue`), `if`, helper functions defined in the protocol (inlined), comprehensions, f-strings,
and concrete `str`/`list`/`dict` methods. Modules: `math`, `csv`, `json`, `opentrons.types`.
Hardware modules (`load_module`) only hold labware for P1. `mix` is expanded into aspirate and
dispense steps exactly as opentrons 9.0.0 does. `transfer`, `distribute` and `consolidate` are not
modelled yet. The protocol is never executed (D-002).

The first construct we cannot model stops lowering. The steps before it are still analysed, and
the stop is reported as `Program.unsupported`, so a partial analysis is never silent (D-011).
"""

import ast
import copy
import csv
import itertools
import json
import math
import operator
import posixpath
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import z3

from otverify import definitions, transfers
from otverify.legacy import params_from_fields
from otverify.model import (
    AirGap,
    Aspirate,
    BlowOut,
    Context,
    Dispense,
    DropTip,
    LoadedLabware,
    LoadedPipette,
    LoadLiquid,
    ParamSpec,
    Pause,
    PickUpTip,
    Program,
    Unsupported,
    WellRef,
)
from otverify.smt import free_symbols, is_sym, smax, smin, to_real

MAX_STEPS = 200_000
MAX_ASSIGNMENTS = 1024  # combinations of finite-parameter values lowered separately
MAX_ENUMERATED_DOMAIN = 400  # largest int interval enumerated on demand (384-well plates fit)
MAX_WHILE_ITERATIONS = 10_000
MAX_CALL_DEPTH = 50
DECIDE_TIMEOUT_MS = 5_000

# ProtocolContext / InstrumentContext / module methods that cannot change any liquid volume.
_CTX_NOOPS = {"comment", "delay", "home", "set_rail_lights", "resume"}
_PIPETTE_NOOPS = {"touch_tip", "home", "home_plunger", "move_to_well", "reset_tipracks"}
_STR_METHODS = {
    "split", "strip", "rstrip", "lstrip", "splitlines", "upper", "lower", "replace", "title",
    "startswith", "endswith", "join", "format", "count", "index", "find", "isdigit", "isnumeric",
    "zfill", "capitalize",
}  # fmt: skip
_LIST_METHODS = {"index", "count", "pop", "insert", "remove", "reverse", "sort", "copy", "clear"}
_DICT_METHODS = {"get", "keys", "values", "items", "update", "pop", "copy", "setdefault"}

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
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
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
    "tuple": tuple,
    "zip": lambda *a: list(zip(*a, strict=False)),
    "enumerate": lambda *a: list(enumerate(*a)),
    "str": str,
    "bool": bool,
    "sorted": sorted,
    "reversed": lambda *a: list(reversed(*a)),
    "any": any,
    "all": all,
    "dict": dict,
    "set": lambda *a: list(dict.fromkeys(*a)),
    "isinstance": lambda v, t: isinstance(v, t),
    "chr": chr,
    "ord": ord,
}
_BUILTIN_TYPES = {"int": int, "float": float, "str": str, "list": list, "dict": dict, "bool": bool}
# Functions of imported modules, applied to concrete arguments only.
_MODULE_FUNCS: dict[str, Callable[..., Any]] = {
    "math.ceil": math.ceil,
    "math.floor": math.floor,
    "math.sqrt": math.sqrt,
    "math.pow": math.pow,
    "math.log": math.log,
    "math.log10": math.log10,
    "math.isclose": math.isclose,
    "csv.reader": lambda lines, **kw: [list(r) for r in csv.reader(lines, **kw)],
    "csv.DictReader": lambda lines, **kw: [dict(r) for r in csv.DictReader(lines, **kw)],
    "json.loads": json.loads,
    "io.StringIO": lambda text: text.splitlines(keepends=True),
    # Tip-state persistence on the robot: we model a fresh robot with no saved files (D-022).
    "os.path.join": posixpath.join,
    "os.path.dirname": posixpath.dirname,
    "os.path.basename": posixpath.basename,
    "os.path.split": posixpath.split,
    "os.path.splitext": posixpath.splitext,
    "os.path.isfile": lambda *a: False,
    "os.path.exists": lambda *a: False,
    "os.path.isdir": lambda *a: False,
}
_MODULE_CONSTS: dict[str, Any] = {"math.pi": math.pi, "math.inf": math.inf}
_MODULE_NOOPS = {"time.sleep", "sleep", "os.makedirs", "os.mkdir", "json.dump"}
_OPAQUE_CONSTRUCTORS = {"opentrons.types.Point", "opentrons.types.Location", "Point"}

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


class _NeedConcrete(_Stop):
    """A parameter-dependent value reached a position that needs a concrete value."""

    def __init__(self, node: ast.AST, reason: str, names: set[str]) -> None:
        super().__init__(node, reason)
        self.names = names


class _Return(Exception):
    def __init__(self, value: Any) -> None:
        self.value = value


class _Raise(Exception):
    """The protocol raised an exception on this path: the run ends here (by design)."""

    def __init__(self, line: int) -> None:
        self.line = line


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class _Ctx:
    """The ProtocolContext argument of run()."""


class _ParamCtx:
    """The ParameterContext argument of add_parameters()."""


class _Params:
    """`protocol.params`."""


class _Trash:
    """The fixed trash, or any well or position in it."""


class _Opaque:
    """A value we track but never look into (a Point, a Location offset, ...)."""


@dataclass(frozen=True)
class _ModuleRef:
    """An imported Python module or name, e.g. `math` or `opentrons.types.Point`."""

    name: str


@dataclass(eq=False)
class _HwModule:
    """A hardware module from load_module() (magnetic, temperature, thermocycler, ...)."""

    location: str
    kind: str = ""
    # Attributes we track: magnetic `status` ("engaged"/"disengaged", confirmed in 9.0.0
    # MagneticModuleContext.status) and thermocycler `lid_position` ("open"/"closed").
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class _UserFunc:
    node: ast.FunctionDef
    defaults: list[Any] = field(default_factory=list)


@dataclass(eq=False)
class _UserClass:
    node: ast.ClassDef
    methods: dict[str, _UserFunc] = field(default_factory=dict)


@dataclass(eq=False)
class _Instance:
    cls: _UserClass
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(eq=False)
class _Bound:
    """A method bound to an instance."""

    instance: _Instance
    func: _UserFunc


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


def _symbols_in(v: object) -> set[str]:
    if isinstance(v, list | tuple):
        return set().union(*(_symbols_in(x) for x in v)) if v else set()
    if isinstance(v, dict):
        return _symbols_in(list(v.keys()) + list(v.values()))
    return free_symbols(v) if is_sym(v) else set()


def _parse_api_level(v: object) -> tuple[int, int] | None:
    if not isinstance(v, str):
        return None
    major, _, minor = v.partition(".")
    if major.isdigit() and minor.isdigit():
        return int(major), int(minor)
    return None


def _only_raises(body: list[ast.stmt]) -> bool:
    """A branch that does nothing but (comment and) raise: input validation."""
    stmts = [s for s in body if not isinstance(s, ast.Expr | ast.Pass)]
    return len(stmts) == 1 and isinstance(stmts[0], ast.Raise)


class _Lowerer:
    def __init__(
        self,
        prog: Program,
        custom_labware: dict[str, definitions.LabwareDef] | None = None,
        domain: list[z3.BoolRef] | None = None,
    ) -> None:
        self.prog = prog
        self.custom_labware = custom_labware or {}
        self.domain = domain or []  # interval-parameter bounds, for deciding branches
        self.env: dict[str, Any] = {}
        self.params: dict[str, Any] | None = None  # name -> concrete value or Z3 constant
        self.legacy = False  # parameters come from fields.json via get_values()
        self.param_specs: list[ParamSpec] = []
        self.context: list[tuple[str, object]] = []
        self.defs: list[definitions.LabwareDef] = []
        self.pip_defs: list[definitions.PipetteDef] = []
        self.last_location: dict[int, WellRef | _Trash] = {}
        self.depth = 0
        self.attrs: dict[tuple[Any, str], Any] = {}  # custom attributes set on wells, labware...
        self.tip_attached: dict[int, bool] = {}
        # What the pipette reports as current_volume: the plunger volume (liquid + air), with
        # Opentrons' own clamps where they are concrete.
        self.tip_volume: dict[int, Any] = {}

    # ---- statements -------------------------------------------------------------------------

    def body(self, stmts: list[ast.stmt]) -> None:
        for s in stmts:
            self.stmt(s)

    def stmt(self, s: ast.stmt) -> None:
        match s:
            case ast.Expr(value=ast.Constant()) | ast.Pass() | ast.Global() | ast.Nonlocal():
                pass
            case ast.Import(names=names):
                for alias in names:
                    root = alias.name.split(".")[0]
                    self.env[alias.asname or root] = _ModuleRef(
                        alias.name if alias.asname else root
                    )
            case ast.ImportFrom(module=module, names=names):
                for alias in names:
                    self.env[alias.asname or alias.name] = _ModuleRef(f"{module}.{alias.name}")
            case ast.FunctionDef(name=name, args=args):
                defaults = [self.expr(d) for d in args.defaults]
                self.env[name] = _UserFunc(s, defaults)
            case ast.ClassDef(name=name, body=body, bases=bases):
                if bases:
                    self.env[name] = _ModuleRef(f"<class {name}>")
                else:
                    cls = _UserClass(s)
                    for item in body:
                        if isinstance(item, ast.FunctionDef):
                            defaults = [self.expr(d) for d in item.args.defaults]
                            cls.methods[item.name] = _UserFunc(item, defaults)
                    self.env[name] = cls
            case ast.Expr(value=ast.Call() as call):
                self.call(call)
            case ast.Expr(value=value):
                self.expr(value)
            case ast.Assign(targets=targets, value=value):
                v = self.expr(value)
                for target in targets:
                    self.bind(target, v)
            case ast.AnnAssign(target=target, value=value) if value is not None:
                self.bind(target, self.expr(value))
            case ast.AugAssign(target=ast.Name(id=name) as target, op=op, value=value):
                self.bind(target, self.binop(s, op, self.lookup(target, name), self.expr(value)))
            case ast.AugAssign(target=ast.Subscript() | ast.Attribute() as target, op=op, value=v):
                load = copy.copy(target)
                load.ctx = ast.Load()
                self.bind(target, self.binop(s, op, self.expr(load), self.expr(v)))
            case ast.Delete(targets=targets):
                for target in targets:
                    self.delete(target)
            case ast.For(target=target, iter=it, body=body, orelse=orelse):
                self.for_loop(s, target, self.expr(it), body, orelse)
            case ast.While(test=test, body=body, orelse=orelse):
                self.while_loop(s, test, body, orelse)
            case ast.If(test=test, body=body, orelse=orelse):
                self.if_stmt(s, test, body, orelse)
            case ast.Return(value=value):
                raise _Return(None if value is None else self.expr(value))
            case ast.Raise():
                raise _Raise(s.lineno)
            case ast.Break():
                raise _Break()
            case ast.Continue():
                raise _Continue()
            case ast.Assert(test=test):
                if not self.truth(test, self.expr(test)):
                    raise _Raise(s.lineno)
            case ast.Try(body=body, handlers=handlers, orelse=orelse, finalbody=finalbody):
                # We never model exceptions from the API (e.g. OutOfTipsError), so handlers only
                # run for the protocol's own `raise`; we take the first handler (D-021).
                try:
                    self.body(body)
                except _Raise:
                    if not handlers:
                        raise
                    self.body(handlers[0].body)
                else:
                    self.body(orelse)
                self.body(finalbody)
            case ast.With(body=body):
                self.body(body)
            case _:
                raise _Stop(s, f"statement `{type(s).__name__}` not modelled")

    def for_loop(
        self, s: ast.stmt, target: ast.expr, items: Any, body: list[ast.stmt], orelse: list
    ) -> None:
        if isinstance(items, dict):
            items = list(items)
        if isinstance(items, str):
            items = list(items)
        if not isinstance(items, list | tuple):
            self.concrete(s, items, "loop iterable")
            raise _Stop(s, "for-loop over a value that is not a concrete list or range")
        for item in items:
            self.bind(target, item)
            self.context.append((ast.unparse(target), _render(item)))
            try:
                self.body(body)
            except _Continue:
                pass
            except _Break:
                return
            finally:
                self.context.pop()
        self.body(orelse)

    def while_loop(self, s: ast.stmt, test: ast.expr, body: list[ast.stmt], orelse: list) -> None:
        for _ in range(MAX_WHILE_ITERATIONS):
            if not self.truth(test, self.expr(test)):
                self.body(orelse)
                return
            try:
                self.body(body)
            except _Continue:
                pass
            except _Break:
                return
        raise _Stop(s, f"while-loop ran more than {MAX_WHILE_ITERATIONS} iterations")

    def if_stmt(self, s: ast.stmt, test: ast.expr, body: list, orelse: list) -> None:
        cond = self.expr(test)
        if isinstance(cond, z3.ArithRef):
            cond = cond != 0
        if _symbols_in(cond) and is_sym(cond):
            decided = self.decide(cond)
            if decided is None and (_only_raises(body) or _only_raises(orelse)):
                # Input validation: values for which the protocol raises are not real runs.
                keep_then = _only_raises(orelse)
                self.prog.path.append(cond if keep_then else z3.Not(cond))
                self.body(body if keep_then else orelse)
                return
        self.body(body if self.truth(test, cond) else orelse)

    def is_setting(self, target: ast.expr) -> bool:
        """An assignment to an attribute or item of a context, pipette or hardware module, e.g.
        `ctx.max_speeds["X"] = 100` or `p.flow_rate.aspirate = 50`. None affects volumes."""
        root = target
        while isinstance(root, ast.Attribute | ast.Subscript):
            root = root.value
        if not isinstance(root, ast.Name) or root.id not in self.env:
            return False
        return isinstance(self.env[root.id], _Ctx | _Pipette | _HwModule | _Labware | WellRef)

    def delete(self, target: ast.expr) -> None:
        if self.is_setting(target):
            return
        if isinstance(target, ast.Name):
            self.env.pop(target.id, None)
            return
        if isinstance(target, ast.Subscript) and not isinstance(target.slice, ast.Slice):
            container, key = self.expr(target.value), self.expr(target.slice)
            self.concrete(target, key, "index")
            if isinstance(container, dict | list):
                try:
                    del container[key]
                    return
                except (KeyError, IndexError, TypeError):
                    pass
        raise _Stop(target, f"`del {ast.unparse(target)}` not modelled")

    def bind(self, target: ast.expr, value: Any) -> None:
        match target:
            case ast.Name(id=name):
                self.env[name] = value
            case ast.Tuple(elts=elts) | ast.List(elts=elts):
                if isinstance(value, dict):
                    value = list(value)
                if not isinstance(value, list | tuple | str) or len(value) != len(elts):
                    raise _Stop(target, "unpacking a value of unknown shape")
                for t, v in zip(elts, value, strict=True):
                    self.bind(t, v)
            case ast.Attribute(value=base, attr=attr) if self.is_setting(target):
                obj = self.expr(base)
                if isinstance(obj, WellRef | _Labware | _Pipette):
                    self.attrs[(obj, attr)] = value  # a protocol's own bookkeeping
            case _ if self.is_setting(target):
                pass
            case ast.Attribute(value=base, attr=attr):
                obj = self.expr(base)
                if isinstance(obj, _Instance):
                    obj.attrs[attr] = value
                elif isinstance(obj, WellRef | _Labware | _Pipette):
                    self.attrs[(obj, attr)] = value
                elif not isinstance(obj, _Opaque | _HwModule):
                    raise _Stop(target, f"assignment to `{ast.unparse(target)}` not modelled")
            case ast.Subscript(value=base, slice=index) if not isinstance(index, ast.Slice):
                container, key = self.expr(base), self.expr(index)
                self.concrete(target, key, "index")
                if isinstance(container, dict):
                    container[key] = value
                elif isinstance(container, list) and isinstance(key, int):
                    if not -len(container) <= key < len(container):
                        raise _Stop(target, f"index {key} out of range")
                    container[key] = value
                else:
                    raise _Stop(target, f"assignment to `{ast.unparse(target)}` not modelled")
            case _:
                raise _Stop(target, f"assignment to `{ast.unparse(target)}` not modelled")

    # ---- expressions ------------------------------------------------------------------------

    def lookup(self, node: ast.AST, name: str) -> Any:
        if name in self.env:
            return self.env[name]
        if name in ("True", "False", "None"):
            return {"True": True, "False": False, "None": None}[name]
        raise _Stop(node, f"`{name}` has no statically known value")

    def concrete(self, node: ast.AST, value: Any, what: str) -> None:
        """Signal that `value` must be concrete here (lowering may retry with enumeration)."""
        if names := _symbols_in(value):
            raise _NeedConcrete(node, f"{what} depends on parameter(s) {sorted(names)}", names)

    def decide(self, cond: z3.BoolRef) -> bool | None:
        """True/False if `cond` holds for all/none of the parameter values on this path."""
        for want, query in ((True, z3.Not(cond)), (False, cond)):
            solver = z3.Solver()
            solver.set("timeout", DECIDE_TIMEOUT_MS)
            solver.add(*self.domain, *self.prog.path, query)
            if solver.check() == z3.unsat:
                return want
        return None

    def truth(self, node: ast.expr, v: Any) -> bool:
        if isinstance(v, z3.ArithRef):
            v = v != 0  # `if vol:` tests a number
        if isinstance(v, _Opaque):
            raise _Stop(node, f"branch on `{ast.unparse(node)}`, a value we do not model")
        if is_sym(v) and isinstance(v, z3.BoolRef):
            decided = self.decide(v)
            if decided is not None:
                return decided
        self.concrete(node, v, f"branch condition `{ast.unparse(node)}`")
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
                if isinstance(v, _Opaque):
                    return v
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
            case ast.IfExp(test=test, body=body, orelse=orelse):
                return self.expr(body if self.truth(test, self.expr(test)) else orelse)
            case ast.Compare(left=left, ops=ops, comparators=rights):
                return self.compare(e, left, ops, rights)
            case ast.Tuple(elts=elts):
                return tuple(self.expr(x) for x in elts)
            case ast.List(elts=elts) | ast.Set(elts=elts):
                return [self.expr(x) for x in elts]
            case ast.Dict(keys=keys, values=values) if None not in keys:
                d = {}
                for k, v in zip(keys, values, strict=True):
                    key = self.expr(k)  # type: ignore[arg-type]
                    self.concrete(e, key, "dict key")
                    d[key] = self.expr(v)
                return d
            case ast.Subscript(value=base, slice=index):
                return self.subscript(e, self.expr(base), index)
            case ast.Call():
                return self.call(e)
            case (
                ast.ListComp(elt=elt, generators=gens) | ast.GeneratorExp(elt=elt, generators=gens)
            ):
                return self.comprehension(gens, lambda: [self.expr(elt)])
            case ast.SetComp(elt=elt, generators=gens):
                return list(dict.fromkeys(self.comprehension(gens, lambda: [self.expr(elt)])))
            case ast.DictComp(key=k, value=v, generators=gens):
                return dict(self.comprehension(gens, lambda: [(self.expr(k), self.expr(v))]))
            case ast.JoinedStr(values=parts):
                return "".join(self.fstring_part(p) for p in parts)
            case ast.Lambda():
                raise _Stop(e, "lambda not modelled")
            case _:
                raise _Stop(e, f"expression `{ast.unparse(e)}` not modelled")

    def fstring_part(self, part: ast.expr) -> str:
        if isinstance(part, ast.Constant):
            return str(part.value)
        assert isinstance(part, ast.FormattedValue)
        v = self.expr(part.value)
        self.concrete(part, v, "f-string value")
        if part.conversion == ord("r"):
            v = repr(v)
        elif part.conversion == ord("s"):
            v = str(v)
        spec = self.expr(part.format_spec) if part.format_spec else ""
        try:
            return format(v, spec)
        except (TypeError, ValueError) as exc:
            raise _Stop(part, f"f-string formatting: {exc}") from None

    def attribute(self, node: ast.AST, recv: Any, attr: str) -> Any:
        if isinstance(recv, _Ctx) and attr == "params":
            if self.params is None or self.legacy:
                raise _Stop(node, "`protocol.params` used, but there is no add_parameters()")
            return _Params()
        if isinstance(recv, _Ctx) and attr == "fixed_trash":
            return _Trash()
        if isinstance(recv, _Pipette) and attr == "trash_container":
            return _Trash()
        if (recv, attr) in self.attrs if isinstance(recv, WellRef | _Labware | _Pipette) else False:
            return self.attrs[(recv, attr)]
        if isinstance(recv, _Pipette):
            return self.pipette_attribute(node, recv, attr)
        if isinstance(recv, WellRef):
            return self.well_attribute(node, recv, attr)
        if isinstance(recv, _Labware):
            if attr in ("load_name", "name"):
                return self.prog.labware[recv.index].load_name
            if attr in (
                "parent",
                "parameters",
                "uri",
                "highest_z",
                "quirks",
                "magdeck_engage_height",
            ):
                return _Opaque()
        if isinstance(recv, _Ctx) and attr == "loaded_labwares":
            loaded: dict[Any, Any] = {
                _slot_key(lw.location): _Labware(i) for i, lw in enumerate(self.prog.labware)
            }
            if (self.prog.api_level or (2, 0)) < (2, 16):
                loaded[12] = _Trash()  # the OT-2 fixed trash is a labware in slot 12
            return loaded
        if isinstance(recv, _Ctx) and attr == "loaded_instruments":
            return {p.mount: _Pipette(i) for i, p in enumerate(self.prog.pipettes)}
        if isinstance(recv, _Ctx) and (
            attr in ("deck", "max_speeds", "rail_lights_on", "door_closed") or attr.startswith("_")
        ):
            return _Opaque()  # settings and private hardware access (e.g. rail lights)
        if isinstance(recv, _HwModule) and attr in recv.state:
            return recv.state[attr]
        if isinstance(recv, _HwModule | _Opaque):
            return _Opaque()  # e.g. temperature: no effect on volumes
        if isinstance(recv, _Instance):
            if attr in recv.attrs:
                return recv.attrs[attr]
            if attr in recv.cls.methods:
                return _Bound(recv, recv.cls.methods[attr])
            raise _Stop(node, f"instance attribute `{attr}` not set")
        if isinstance(recv, _Params):
            if self.params is None or attr not in self.params:
                raise _Stop(node, f"no runtime parameter named `{attr}`")
            return self.params[attr]
        if isinstance(recv, _ModuleRef):
            name = f"{recv.name}.{attr}"
            return _MODULE_CONSTS.get(name, _ModuleRef(name))
        raise _Stop(node, f"attribute `{ast.unparse(node)}` not modelled")  # type: ignore[arg-type]

    def pipette_attribute(self, node: ast.AST, pip: _Pipette, attr: str) -> Any:
        loaded, pip_def = self.prog.pipettes[pip.index], self.pip_defs[pip.index]
        if attr in ("max_volume", "min_volume", "channels"):
            return getattr(pip_def, attr)
        if attr in ("name", "model"):
            return loaded.name
        if attr == "mount":
            return loaded.mount
        if attr == "tip_racks":
            return [_Labware(i) for i in loaded.tip_racks]
        if attr == "has_tip":
            return self.tip_attached.get(pip.index, False)
        if attr == "type":
            return "single" if pip_def.channels == 1 else "multi"
        if attr == "hw_pipette":
            return {
                "has_tip": self.tip_attached.get(pip.index, False),
                "channels": pip_def.channels,
                "max_volume": pip_def.max_volume,
                "min_volume": pip_def.min_volume,
                "name": loaded.name,
            }
        if attr == "current_volume":
            return self.tip_volume.get(pip.index, 0.0)
        if attr in ("flow_rate", "well_bottom_clearance", "starting_tip", "speed",
                    "default_speed", "api_version", "type"):  # fmt: skip
            return _Opaque()
        raise _Stop(node, f"pipette attribute `{attr}` not modelled")

    def well_attribute(self, node: ast.AST, well: WellRef, attr: str) -> Any:
        if attr == "max_volume":
            return self.defs[well.labware].capacities[well.well]
        if attr in ("well_name", "display_name"):
            return well.well
        if attr == "parent":
            return _Labware(well.labware)
        if attr in ("depth", "diameter", "width", "length"):
            geometry = self.defs[well.labware].geometry.get(well.well)
            if geometry is None:
                return _Opaque()
            return geometry[("depth", "diameter", "width", "length").index(attr)]
        if attr in ("geometry", "has_tip"):
            return _Opaque()
        raise _Stop(node, f"well attribute `{attr}` not modelled")

    def compare(
        self, node: ast.AST, left: ast.expr, ops: list[ast.cmpop], rights: list[ast.expr]
    ) -> Any:
        lhs = self.expr(left)
        symbolic: list[z3.BoolRef] = []
        for op, r in zip(ops, rights, strict=True):
            rhs = self.expr(r)
            if type(op) not in _CMPOPS:
                raise _Stop(node, f"comparison `{type(op).__name__}` not modelled")
            if isinstance(lhs, _Opaque) or isinstance(rhs, _Opaque):
                raise _Stop(
                    node,
                    f"comparison `{ast.unparse(node)}` involves well geometry or another value "
                    "we do not model",
                )
            if is_sym(lhs) or is_sym(rhs):
                if not (_is_num(lhs) and _is_num(rhs)) or isinstance(op, ast.Is | ast.IsNot):
                    self.concrete(node, [lhs, rhs], f"comparison `{ast.unparse(node)}`")
                symbolic.append(_CMPOPS[type(op)](lhs, rhs))
            else:
                if _symbols_in([lhs, rhs]):
                    self.concrete(node, [lhs, rhs], f"comparison `{ast.unparse(node)}`")
                try:
                    holds = _CMPOPS[type(op)](lhs, rhs)
                except TypeError as exc:
                    raise _Stop(node, f"comparison `{ast.unparse(node)}`: {exc}") from None
                if not holds:
                    return False
            lhs = rhs
        return z3.And(*symbolic) if symbolic else True

    def binop(self, node: ast.AST, op: ast.operator, a: Any, b: Any) -> Any:
        if isinstance(a, _Opaque) or isinstance(b, _Opaque):
            return _Opaque()  # e.g. well.diameter / 2 for an offset: a position, not a volume
        if is_sym(a) or is_sym(b):
            if not (_is_num(a) and _is_num(b)):
                self.concrete(node, [a, b], "arithmetic on a parameter and a non-number")
            if isinstance(op, ast.Add | ast.Sub | ast.Mult):
                return _BINOPS[type(op)](a, b)
            if isinstance(op, ast.Div) and not is_sym(b):
                if b == 0:
                    raise _Stop(node, "division by zero")
                return to_real(a) / b
            self.concrete(node, [a, b], f"`{ast.unparse(node)}`")  # type: ignore[arg-type]
        if _symbols_in([a, b]):
            self.concrete(node, [a, b], f"`{ast.unparse(node)}`")  # type: ignore[arg-type]
        same_seq = type(a) is type(b) and isinstance(a, list | tuple | str)
        ok = (
            (_is_num(a) and _is_num(b))
            or (isinstance(op, ast.Add) and same_seq)
            or (
                isinstance(op, ast.Mult)
                and isinstance(a, list | tuple | str)
                and isinstance(b, int)
            )
            or (isinstance(op, ast.Mod) and isinstance(a, str))
        )
        if not ok or type(op) not in _BINOPS:
            raise _Stop(node, f"operator `{type(op).__name__}` on these operands not modelled")
        try:
            return _BINOPS[type(op)](a, b)
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise _Stop(node, f"arithmetic error: {exc}") from None

    def subscript(self, node: ast.AST, base: Any, index: ast.expr) -> Any:
        if isinstance(base, _Trash | _Opaque):
            return base
        if isinstance(index, ast.Slice):
            parts = (index.lower, index.upper, index.step)
            bounds = [None if x is None else self.expr(x) for x in parts]
            self.concrete(node, bounds, "slice bound")
            if not isinstance(base, list | tuple | str) or not all(
                b is None or isinstance(b, int) for b in bounds
            ):
                raise _Stop(node, "slice not modelled")
            return base[slice(*bounds)]
        key = self.expr(index)
        self.concrete(node, key, "index")
        if isinstance(base, _Labware):
            return self.well(node, base, key)
        if (
            isinstance(base, list | tuple | str)
            and isinstance(key, int)
            and not isinstance(key, bool)
        ):
            if not -len(base) <= key < len(base):
                raise _Stop(node, f"index {key} out of range for a list of length {len(base)}")
            return base[key]
        if isinstance(base, dict):
            if key not in base:
                raise _Stop(node, f"key {key!r} not in dict")
            return base[key]
        raise _Stop(node, "subscript not modelled")

    def well(self, node: ast.AST, lw: _Labware, name: object) -> WellRef:
        if not isinstance(name, str) or name not in self.defs[lw.index].capacities:
            load_name = self.prog.labware[lw.index].load_name
            raise _Stop(node, f"well {name!r} does not exist in {load_name}")
        return WellRef(lw.index, name)

    def comprehension(self, gens: list[ast.comprehension], emit: Callable[[], list]) -> list:
        saved = dict(self.env)
        out: list = []

        def loop(i: int) -> None:
            if i == len(gens):
                out.extend(emit())
                return
            gen = gens[i]
            items = self.expr(gen.iter)
            if isinstance(items, dict | str):
                items = list(items)
            if not isinstance(items, list | tuple):
                self.concrete(gen.iter, items, "comprehension iterable")
                raise _Stop(gen.iter, "comprehension over a value that is not a concrete list")
            for item in items:
                self.bind(gen.target, item)
                if all(self.truth(c, self.expr(c)) for c in gen.ifs):
                    loop(i + 1)

        try:
            loop(0)
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

    def plain_args(self, call: ast.Call) -> tuple[list[Any], dict[str, Any]]:
        """Evaluated positional and keyword arguments of a call."""
        if any(isinstance(a, ast.Starred) for a in call.args):
            raise _Stop(call, "*args not modelled")
        if any(k.arg is None for k in call.keywords):
            raise _Stop(call, "**kwargs not modelled")
        args = [self.expr(a) for a in call.args]
        kwargs = {k.arg: self.expr(k.value) for k in call.keywords}  # type: ignore[misc]
        return args, kwargs

    def builtin(self, call: ast.Call, name: str) -> Any:
        args, kwargs = self.plain_args(call)
        if _symbols_in(args):
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
            if name == "int" and len(args) == 1 and isinstance(args[0], z3.ArithRef):
                if args[0].is_int():
                    return args[0]
            self.concrete(call, args, f"`{name}()` argument")
        if name == "isinstance" and len(args) == 2 and isinstance(args[1], _ModuleRef):
            return False  # isinstance(x, SomeApiClass): never true for our plain values
        try:
            return _BUILTINS[name](*args, **kwargs)
        except (TypeError, ValueError, KeyError) as exc:
            raise _Stop(call, f"{name}() on these arguments: {exc}") from None

    def get_values(self, call: ast.Call) -> list[Any]:
        if self.params is None or not self.legacy:
            raise _Stop(call, "get_values() without a fields.json")
        names = [self.expr(a) for a in call.args]
        missing = [n for n in names if n not in self.params]
        if call.keywords or missing:
            raise _Stop(call, f"get_values() of unknown field(s) {missing}")
        return [self.params[n] for n in names]

    def instantiate(self, call: ast.Call, cls: _UserClass) -> _Instance:
        instance = _Instance(cls)
        if "__init__" in cls.methods:
            self.user_call(call, cls.methods["__init__"], instance)
        return instance

    def attr_builtin(self, call: ast.Call, name: str) -> Any:
        args, _ = self.plain_args(call)
        if len(args) < 2 or not isinstance(args[1], str):
            raise _Stop(call, f"{name}() with a non-literal attribute name")
        obj, attr = args[0], args[1]
        store = obj.attrs if isinstance(obj, _Instance) else None
        key = (obj, attr)
        if name == "setattr" and len(args) == 3:
            if store is not None:
                store[attr] = args[2]
            elif isinstance(obj, WellRef | _Labware | _Pipette):
                self.attrs[key] = args[2]
            elif not isinstance(obj, _Opaque | _HwModule):
                raise _Stop(call, "setattr() on this object not modelled")
            return None
        present = attr in store if store is not None else key in self.attrs
        if name == "hasattr":
            return present
        if present:
            return store[attr] if store is not None else self.attrs[key]
        if len(args) == 3:
            return args[2]
        raise _Stop(call, f"getattr(): attribute `{attr}` not set")

    def user_call(self, call: ast.Call, fn: _UserFunc, instance: _Instance | None = None) -> Any:
        node = fn.node
        a = node.args
        if a.vararg or a.kwarg or a.kwonlyargs or a.posonlyargs:
            raise _Stop(
                call, f"call to `{node.name}` with *args/**kwargs/keyword-only not modelled"
            )
        if self.depth >= MAX_CALL_DEPTH:
            raise _Stop(call, f"call depth over {MAX_CALL_DEPTH} (recursion?)")
        args, kwargs = self.plain_args(call)
        if instance is not None:
            args = [instance, *args]
        names = [p.arg for p in a.args]
        if len(args) > len(names) or any(k not in names for k in kwargs):
            raise _Stop(call, f"bad arguments to `{node.name}`")
        bound = dict(zip(names, args, strict=False))
        first_default = len(names) - len(fn.defaults)
        for i, name in enumerate(names):
            if name in kwargs:
                bound[name] = kwargs[name]
            elif name not in bound:
                if i < first_default:
                    raise _Stop(call, f"missing argument `{name}` to `{node.name}`")
                bound[name] = fn.defaults[i - first_default]
        # Late-binding approximation of Python scoping: the body sees the caller's variables;
        # names it binds stay local. Mutations of shared lists and dicts persist, as in Python.
        saved = self.env
        self.env = {**saved, **bound}
        self.depth += 1
        self.context.append(("in", f"{node.name}()"))
        try:
            self.body(node.body)
            return None
        except _Return as ret:
            return ret.value
        finally:
            self.context.pop()
            self.depth -= 1
            self.env = saved

    def call(self, call: ast.Call) -> Any:
        func = call.func
        if isinstance(func, ast.Name):
            if func.id in self.env:
                target = self.env[func.id]
                if isinstance(target, _UserFunc):
                    return self.user_call(call, target)
                if isinstance(target, _UserClass):
                    return self.instantiate(call, target)
                if isinstance(target, _Bound):
                    return self.user_call(call, target.func, target.instance)
                if isinstance(target, _ModuleRef):
                    return self.module_call(call, target)
                raise _Stop(call, f"call to `{func.id}` not modelled")
            if func.id in _BUILTINS:
                return self.builtin(call, func.id)
            if func.id == "get_values":
                return self.get_values(call)
            if func.id == "print":
                return None
            if func.id in ("setattr", "getattr", "hasattr"):
                return self.attr_builtin(call, func.id)
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
        if isinstance(recv, _HwModule):
            return self.hw_module_call(call, recv, method)
        if isinstance(recv, _ModuleRef):
            return self.module_call(call, _ModuleRef(f"{recv.name}.{method}"))
        if isinstance(recv, _Instance):
            if method not in recv.cls.methods:
                raise _Stop(call, f"method `{method}` not defined on the class")
            return self.user_call(call, recv.cls.methods[method], recv)
        if isinstance(recv, _Opaque):
            return _Opaque()  # a thread, a Point, a file handle: no effect on volumes
        if isinstance(recv, _Trash) and method in ("wells", "top", "bottom", "center", "move"):
            return [recv] if method == "wells" else recv
        return self.value_method(call, recv, method)

    def value_method(self, call: ast.Call, recv: Any, method: str) -> Any:
        """Methods of concrete str / list / dict values."""
        allowed = (
            _STR_METHODS
            if isinstance(recv, str)
            else _LIST_METHODS | {"append", "extend"}
            if isinstance(recv, list)
            else _DICT_METHODS
            if isinstance(recv, dict)
            else set()
        )
        if method not in allowed:
            raise _Stop(call, f"method `.{method}()` not modelled")
        args, kwargs = self.plain_args(call)
        if method not in ("append", "extend", "insert", "setdefault", "update"):
            self.concrete(call, [recv, args, list(kwargs.values())], f"`.{method}()`")
        try:
            result = getattr(recv, method)(*args, **kwargs)
        except (TypeError, ValueError, KeyError, IndexError, AttributeError) as exc:
            raise _Stop(call, f"`.{method}()`: {exc}") from None
        if method in ("keys", "values", "items"):
            return list(result)
        return result

    def module_call(self, call: ast.Call, fn: _ModuleRef) -> Any:
        name = fn.name.removeprefix("opentrons.protocol_api.").removeprefix("protocol_api.")
        if name in _MODULE_NOOPS or fn.name.endswith(".sleep"):
            return None
        if name in _OPAQUE_CONSTRUCTORS or fn.name.endswith(("types.Point", "types.Location")):
            return _Opaque()
        if fn.name in ("threading.Thread", "threading.Event", "threading.Lock"):
            return _Opaque()  # e.g. blinking the rail lights during a pause
        if name in _MODULE_FUNCS:
            args, kwargs = self.plain_args(call)
            self.concrete(call, [args, list(kwargs.values())], f"`{name}()` argument")
            try:
                return _MODULE_FUNCS[name](*args, **kwargs)
            except (TypeError, ValueError, KeyError, csv.Error) as exc:
                raise _Stop(call, f"`{name}()`: {exc}") from None
        if fn.name in _BUILTIN_TYPES:
            return self.builtin(call, fn.name)
        raise _Stop(call, f"call to `{fn.name}` not modelled")

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

    def load_labware(self, call: ast.Call, location: Any, a: dict[str, ast.expr]) -> _Labware:
        if "load_name" not in a:
            raise _Stop(call, "load_labware without load_name")
        load_name = self.expr(a["load_name"])
        self.concrete(call, [load_name, location], "labware name or deck slot")
        lw_def = None
        if isinstance(load_name, str):
            lw_def = definitions.labware(load_name, self.custom_labware)
        if lw_def is None:
            raise _Stop(call, f"labware {load_name!r} is not in the definitions snapshot")
        self.prog.labware.append(LoadedLabware(load_name, str(location), call.lineno, lw_def))
        self.defs.append(lw_def)
        return _Labware(len(self.prog.labware) - 1)

    def ctx_call(self, call: ast.Call, method: str) -> Any:
        if method in _CTX_NOOPS:
            return None
        if method == "pause":
            self.emit(Pause(call.lineno, self.ctx()))
            return None
        if method == "is_simulating":
            # Follow the simulation branch, as opentrons_simulate does: real-run-only branches
            # are tip-state files and light blinking (D-022).
            return True
        if method == "define_liquid":
            return _Liquid()
        if method in ("load_labware", "load_labware_by_name"):
            a = self.args(call, ["load_name", "location", "label", "namespace", "version"])
            if "location" not in a:
                raise _Stop(call, "load_labware without a location")
            return self.load_labware(call, self.expr(a["location"]), a)
        if method == "load_instrument":
            return self.load_instrument(call)
        if method == "load_module":
            a = self.args(call, ["module_name", "location", "configuration"])
            location = self.expr(a["location"]) if "location" in a else None
            self.concrete(call, location, "module slot")
            name = self.expr(a["module_name"]) if "module_name" in a else ""
            kind = str(name).lower()
            mod = _HwModule(str(location) if location is not None else "thermocycler", kind)
            if "magnetic" in kind or "magdeck" in kind:
                mod.state["status"] = "disengaged"
            return mod
        raise _Stop(call, f"`protocol.{method}()` not modelled")

    def hw_module_call(self, call: ast.Call, mod: _HwModule, method: str) -> Any:
        if method in ("load_labware", "load_labware_by_name"):
            a = self.args(call, ["load_name", "label", "namespace", "version"])
            return self.load_labware(call, f"module in {mod.location}", a)
        if method in ("engage", "disengage") and "status" in mod.state:
            mod.state["status"] = "engaged" if method == "engage" else "disengaged"
        if method in ("open_lid", "close_lid"):
            mod.state["lid_position"] = "open" if method == "open_lid" else "closed"
        # Temperature, magnet, lid, shaking and latch commands do not move liquid.
        return None

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
        if method in ("wells", "columns", "rows") and call.args:
            # wells("A1", "B1"), columns(0), rows("A"): selection by name or index
            args, _ = self.plain_args(call)
            self.concrete(call, args, f"`labware.{method}()` argument")
            try:
                groups = {
                    "wells": dict(zip(lw_def.wells(), lw_def.wells(), strict=True)),
                    "columns": {str(i + 1): c for i, c in enumerate(lw_def.columns())},
                    "rows": {r[0][0]: r for r in lw_def.rows()},
                }[method]
                ordered = list(groups.values())
                picked = [ordered[a] if isinstance(a, int) else groups[str(a)] for a in args]
            except (KeyError, IndexError) as exc:
                raise _Stop(call, f"`labware.{method}()` selection {exc} not found") from None
            if method == "wells":
                return [WellRef(lw.index, w) for w in picked]
            return [[WellRef(lw.index, w) for w in group] for group in picked]
        if (call.args or call.keywords) and method != "well":
            raise _Stop(call, f"`labware.{method}()` with arguments not modelled")
        if method == "wells":
            return [WellRef(lw.index, w) for w in lw_def.wells()]
        if method == "columns":
            return [[WellRef(lw.index, w) for w in col] for col in lw_def.columns()]
        if method == "rows":
            return [[WellRef(lw.index, w) for w in row] for row in lw_def.rows()]
        if method == "wells_by_name":
            return {w: WellRef(lw.index, w) for w in lw_def.wells()}
        if method == "columns_by_name":
            return {
                str(i + 1): [WellRef(lw.index, w) for w in c]
                for i, c in enumerate(lw_def.columns())
            }
        if method == "rows_by_name":
            return {r[0][0]: [WellRef(lw.index, w) for w in r] for r in lw_def.rows()}
        if method == "set_offset":
            return None
        if method == "well":
            args, _ = self.plain_args(call)
            self.concrete(call, args, "`labware.well()` argument")
            if len(args) == 1 and isinstance(args[0], int) and not isinstance(args[0], bool):
                names = lw_def.wells()
                if not 0 <= args[0] < len(names):
                    raise _Stop(call, f"well index {args[0]} out of range")
                return WellRef(lw.index, names[args[0]])
            if len(args) == 1:
                return self.well(call, lw, args[0])
        raise _Stop(call, f"`labware.{method}()` not modelled")

    def well_call(self, call: ast.Call, well: WellRef, method: str) -> Any:
        if method in ("top", "bottom", "center", "move", "from_center_cartesian"):
            return well  # a position inside the well; the well is what matters for volumes
        if method == "load_liquid":
            a = self.args(call, ["liquid", "volume"])
            volume = self.expr(a["volume"]) if "volume" in a else None
            if not _is_num(volume):
                raise _Stop(call, "load_liquid volume is not a number")
            self.emit(LoadLiquid(call.lineno, self.ctx(), well, _num(volume)))
            return None
        raise _Stop(call, f"`well.{method}()` not modelled")

    def volume_arg(self, call: ast.Call, a: dict[str, ast.expr], method: str) -> Any:
        volume = self.expr(a["volume"]) if "volume" in a else None
        if volume is not None and not _is_num(volume):
            raise _Stop(call, f"{method} volume is not a number")
        return None if volume is None else _num(volume)

    def pipette_call(self, call: ast.Call, pip: _Pipette, method: str) -> _Pipette:
        line, i = call.lineno, pip.index
        if method in _PIPETTE_NOOPS:
            return pip
        if method == "pick_up_tip":
            self.emit(PickUpTip(line, self.ctx(), i))
            self.tip_attached[i] = True
        elif method in ("drop_tip", "return_tip"):
            self.emit(DropTip(line, self.ctx(), i))
            self.tip_attached[i] = False
        elif method == "move_to":
            a = self.args(call, ["location", "force_direct", "minimum_z_height", "speed"])
            self.last_location[i] = self.location(call, pip, a.get("location"))
        elif method in ("aspirate", "dispense"):
            a = self.args(call, ["volume", "location", "rate"])
            vol = self.volume_arg(call, a, method)
            loc = self.location(call, pip, a.get("location"))
            self.last_location[i] = loc
            if method == "aspirate":
                if isinstance(loc, _Trash):
                    raise _Stop(call, "aspirate from the trash")
                self.emit(Aspirate(line, self.ctx(), i, self.channel_wells(call, pip, loc), vol))
            else:
                wells = () if isinstance(loc, _Trash) else self.channel_wells(call, pip, loc)
                self.emit(Dispense(line, self.ctx(), i, wells, vol))
        elif method == "mix":
            self.mix(call, pip)
        elif method == "blow_out":
            a = self.args(call, ["location"])
            loc = self.location(call, pip, a.get("location"))
            self.last_location[i] = loc
            wells = () if isinstance(loc, _Trash) else self.channel_wells(call, pip, loc)
            self.emit(BlowOut(line, self.ctx(), i, wells))
        elif method == "air_gap":
            a = self.args(call, ["volume", "height"])
            self.emit(AirGap(line, self.ctx(), i, self.volume_arg(call, a, method)))
        elif method in ("transfer", "distribute", "consolidate"):
            self.transfer(call, pip, method)
        else:
            raise _Stop(call, f"`pipette.{method}()` not modelled yet")
        return pip

    def mix(self, call: ast.Call, pip: _Pipette) -> None:
        a = self.args(call, ["repetitions", "volume", "location", "rate"])
        reps = self.expr(a["repetitions"]) if "repetitions" in a else 1
        vol = self.volume_arg(call, a, "mix")
        self.emit_mix(call, pip, reps, vol, self.location(call, pip, a.get("location")))

    def emit_mix(self, call: ast.Call, pip: _Pipette, reps: Any, vol: Any, loc: Any) -> None:
        """Expand as opentrons 9.0.0 InstrumentContext.mix: aspirate at the location, then
        (repetitions - 1) x (dispense, aspirate) in place, then a final dispense."""
        self.concrete(call, reps, "mix repetitions")
        if not isinstance(reps, int) or isinstance(reps, bool):
            raise _Stop(call, "mix repetitions is not an int")
        if isinstance(loc, _Trash):
            raise _Stop(call, "mix in the trash")
        self.last_location[pip.index] = loc
        wells = self.channel_wells(call, pip, loc)
        line, ctx, i = call.lineno, self.ctx(), pip.index
        self.emit(Aspirate(line, ctx, i, wells, vol))
        for _ in range(reps - 1):
            self.emit(Dispense(line, ctx, i, wells, vol))
            self.emit(Aspirate(line, ctx, i, wells, vol))
        self.emit(Dispense(line, ctx, i, wells, vol))

    def lint(self, call: ast.AST, prop: str, severity: str, message: str) -> None:
        self.prog.lint.append((getattr(call, "lineno", 0), self.ctx(), prop, severity, message))

    def transfer(self, call: ast.Call, pip: _Pipette, mode: str) -> None:
        """transfer / distribute / consolidate, planned by the port of opentrons' TransferPlan
        (transfers.py, checked differentially against the real one: D-020)."""
        a = self.args(call, ["volume", "source", "dest"])
        if not {"volume", "source", "dest"} <= set(a):
            raise _Stop(call, f"{mode}() without volume, source and dest")
        volume = self.expr(a["volume"])
        source, dest = self.expr(a["source"]), self.expr(a["dest"])
        kwargs = {k: self.expr(v) for k, v in a.items() if k not in ("volume", "source", "dest")}
        for k in kwargs:
            if k not in transfers.KNOWN_KWARGS:
                self.lint(
                    call, "API", "warning", f"`{k}=` is not a {mode}() option: Opentrons ignores it"
                )
        api = self.prog.api_level or (2, 0)
        pip_def, loaded = self.pip_defs[pip.index], self.prog.pipettes[pip.index]

        def decide(cond: Any) -> bool:
            if isinstance(cond, z3.BoolRef):
                decided = self.decide(cond)
                if decided is None:
                    self.concrete(call, cond, f"{mode}() planning (volume splitting or grouping)")
                return bool(decided)
            return bool(cond)

        def valid_row(loc: Any) -> bool:
            if not isinstance(loc, WellRef):
                return False
            rows = self.defs[loc.labware].rows()
            first = rows[:2] if api >= (2, 2) and len(rows) == 16 else rows[:1]
            return any(loc.well in row for row in first)

        if pip_def.channels > 1:
            skipped = [
                w.well
                for x in (source, dest)
                for w in _flatten(x)
                if isinstance(w, WellRef) and not valid_row(w)
            ]
            if skipped:
                self.lint(
                    call,
                    "API",
                    "warning",
                    f"{len(skipped)} well(s) outside the first row ({', '.join(skipped[:4])}...) "
                    f"are silently skipped by a {pip_def.channels}-channel {mode}()",
                )
        try:
            opts = transfers.options_from_kwargs(mode, kwargs, api, pip_def.min_volume, decide)
            commands = transfers.plan(
                _as_volume(volume),
                source,
                dest,
                opts,
                pipette_max=pip_def.max_volume,
                tip_max=loaded.tip_capacity,
                channels=pip_def.channels,
                api=api,
                valid_row=valid_row,
                decide=decide,
                trash=_Trash(),
            )
        except transfers.NotPorted as exc:
            raise _Stop(call, f"{mode}(): {exc}") from None
        except transfers.PlanError as exc:
            self.emit_commands(call, pip, exc.commands)
            self.lint(call, "CRASH", "violation", f"{mode}() makes Opentrons raise: {exc}")
            raise _Raise(call.lineno) from None
        if mode != "transfer" and not any(c[0] == "aspirate" for c in commands):
            self.lint(
                call,
                "API",
                "warning",
                f"{mode}() moves no liquid: each volume plus disposal and air gap exceeds the "
                "tip, and Opentrons silently skips it",
            )
        self.emit_commands(call, pip, commands)

    def emit_commands(self, call: ast.Call, pip: _Pipette, commands: list) -> None:
        line, i = call.lineno, pip.index
        for name, *args in commands:
            if name == "pick_up_tip":
                self.emit(PickUpTip(line, self.ctx(), i))
                self.tip_attached[i] = True
            elif name in ("drop_tip", "return_tip"):
                self.emit(DropTip(line, self.ctx(), i))
                self.tip_attached[i] = False
            elif name in ("aspirate", "dispense"):
                vol, loc = args
                vol = _num(vol)
                self.last_location[i] = loc
                if name == "aspirate":
                    if isinstance(loc, _Trash):
                        raise _Stop(call, "transfer aspirates from the trash")
                    self.emit(
                        Aspirate(line, self.ctx(), i, self.channel_wells(call, pip, loc), vol)
                    )
                else:
                    wells = () if isinstance(loc, _Trash) else self.channel_wells(call, pip, loc)
                    self.emit(Dispense(line, self.ctx(), i, wells, vol))
            elif name == "mix":
                reps, vol, loc = args
                self.emit_mix(call, pip, reps, None if vol is None else _num(vol), loc)
            elif name == "air_gap":
                self.emit(AirGap(line, self.ctx(), i, _num(args[0])))
            elif name == "blow_out":
                loc = args[0] if args[0] is not None else self.last_location.get(i)
                if loc is None:
                    raise _Stop(call, "blow_out with no location")
                self.last_location[i] = loc
                wells = () if isinstance(loc, _Trash) else self.channel_wells(call, pip, loc)
                self.emit(BlowOut(line, self.ctx(), i, wells))

    def location(self, call: ast.Call, pip: _Pipette, node: ast.expr | None) -> WellRef | _Trash:
        if node is None:
            if pip.index not in self.last_location:
                raise _Stop(call, "no location given and no previous location for this pipette")
            return self.last_location[pip.index]
        loc = self.expr(node)
        if not isinstance(loc, WellRef | _Trash):
            raise _Stop(node, "location is not a well or the trash (Location objects not modelled)")
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
        row = column.index(well.well)
        if len(column) == 2 * channels and row in (0, 1):  # 384-well: every other row
            return tuple(WellRef(well.labware, w) for w in column[row::2])
        raise _Stop(call, f"{channels}-channel access to {well.well} of this labware not modelled")

    # ---- helpers ----------------------------------------------------------------------------

    def ctx(self) -> Context:
        return tuple(self.context)

    def emit(self, step: Any) -> None:
        if len(self.prog.steps) >= MAX_STEPS:
            raise _Stop(ast.Pass(lineno=step.line), f"more than {MAX_STEPS} steps")
        self.prog.steps.append(step)
        self.track_tip(step)

    def track_tip(self, step: Any) -> None:
        """current_volume and has_tip, for protocols that branch on them."""
        if not hasattr(step, "pipette"):
            return
        i = step.pipette
        cur = self.tip_volume.get(i, 0.0)
        cap = self.prog.pipettes[i].tip_capacity
        api = self.prog.api_level or (2, 0)
        if isinstance(step, PickUpTip):
            self.tip_attached[i], cur = True, 0.0
        elif isinstance(step, DropTip):
            self.tip_attached[i], cur = False, 0.0
        elif isinstance(step, Aspirate | AirGap):
            vol = step.volume
            if vol is None or (isinstance(step, Aspirate) and api < (2, 16) and _is_zero(vol)):
                cur = cap
            else:
                cur = cur + vol
        elif isinstance(step, Dispense):
            vol = step.volume
            if vol is None or (api <= (2, 16) and _is_zero(vol)):
                cur = 0.0
            elif not is_sym(vol) and not is_sym(cur):
                cur = max(0.0, cur - vol)
            else:
                cur = cur - vol
        elif isinstance(step, BlowOut):
            cur = 0.0
        self.tip_volume[i] = cur


def _slot_key(location: str) -> object:
    return int(location) if location.isdigit() else location


def _is_zero(v: Any) -> bool:
    return not is_sym(v) and v == 0


def _num(v: Any) -> Any:
    return v if is_sym(v) else float(v)


def _as_volume(v: Any) -> Any:
    """A transfer volume: a number, a per-transfer list, or a (min, max) gradient tuple."""
    if isinstance(v, list):
        return [_num(x) for x in v]
    if isinstance(v, tuple):
        return tuple(_num(x) for x in v)
    if not _is_num(v):
        raise ValueError(f"transfer volume {v!r} is not a number")
    return _num(v)


def _flatten(x: Any) -> list[Any]:
    if isinstance(x, list | tuple):
        return [w for item in x for w in _flatten(item)]
    return [x]


def _render(v: object) -> object:
    if isinstance(v, WellRef):
        return v.well
    if isinstance(v, int | float | str | bool) or v is None:
        return v
    if is_sym(v):
        return str(v)
    return type(v).__name__


def _assignments(specs: list[ParamSpec]) -> tuple[list[dict[str, object]], int]:
    """Combinations of finite-parameter values, the all-defaults combination first, and the
    total number of combinations. Beyond MAX_ASSIGNMENTS, each finite parameter is varied one
    at a time from the defaults instead (D-021)."""
    finite = [p for p in specs if p.finite]
    options = [[p.default, *(c for c in p.choices or () if c != p.default)] for p in finite]
    total = math.prod(len(o) for o in options)
    if total <= MAX_ASSIGNMENTS:
        combos = itertools.product(*options)
        return [dict(zip((p.name for p in finite), c, strict=True)) for c in combos], total
    defaults = {p.name: p.default for p in finite}
    out = [defaults]
    for p, opts in zip(finite, options, strict=True):
        out.extend({**defaults, p.name: v} for v in opts[1:])
    return out, total


def _symbol(p: ParamSpec) -> z3.ArithRef:
    return z3.Int(p.name) if p.kind == "int" else z3.Real(p.name)


def _enumerable(p: ParamSpec) -> bool:
    return (
        p.kind == "int"
        and not p.finite
        and p.minimum is not None
        and p.maximum is not None
        and p.maximum - p.minimum + 1 <= MAX_ENUMERATED_DOMAIN
    )


def _enumerate(p: ParamSpec) -> ParamSpec:
    assert p.minimum is not None and p.maximum is not None
    return replace(p, choices=tuple(range(int(p.minimum), int(p.maximum) + 1)))


def lower(
    source: str,
    fields: list[dict[str, Any]] | None = None,
    custom_labware: list[dict[str, Any]] | None = None,
) -> list[Program]:
    """Lower protocol source to one Program per finite-parameter assignment.

    `fields` is the protocol's fields.json (legacy library protocols that call get_values), and
    `custom_labware` its labware definitions (the library's `protocols/<name>/labware/*.json`).
    Raises SyntaxError if the source does not parse.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)  # e.g. invalid escapes in corpus strings
        tree = ast.parse(source)
    custom: dict[str, definitions.LabwareDef] = {}
    for definition in custom_labware or []:
        try:
            lw = definitions.from_definition(definition)
            custom[lw.load_name] = lw
        except (KeyError, TypeError, ValueError):
            pass  # a malformed definition: loading it stops lowering with "not in snapshot"
    base = _Lowerer(Program(), custom)
    base.env.update({"__file__": "protocol.py", "__name__": "protocol"})
    run: ast.FunctionDef | None = None
    add_parameters: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            run = node
        elif isinstance(node, ast.FunctionDef) and node.name == "add_parameters":
            add_parameters = node
        elif isinstance(
            node, ast.Assign | ast.Import | ast.ImportFrom | ast.FunctionDef | ast.ClassDef
        ):
            # Module-level constants, imports, helpers and the metadata/requirements dicts.
            # Anything we cannot evaluate is left unbound; a later use stops lowering clearly.
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
    legacy = False
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
    elif fields is not None:
        try:
            specs = params_from_fields(fields)
        except (ValueError, KeyError, TypeError) as exc:
            return failed(1, f"malformed fields.json: {exc}")
        legacy = True

    # Lower every assignment. If an int interval parameter is needed concretely, enumerate it
    # and start again (D-017).
    while True:
        assignments, total = _assignments(specs)
        symbols = {p.name: _symbol(p) for p in specs if not p.finite}
        domain = [
            z3.And(symbols[p.name] >= p.minimum, symbols[p.name] <= p.maximum)
            for p in specs
            if p.name in symbols
        ]
        enumerated = tuple(p.name for p in specs if p.finite and p.minimum is not None)
        partial = None
        if len(assignments) < total:
            partial = Unsupported(
                add_parameters.lineno if add_parameters else 1,
                f"only {len(assignments)} of {total} combinations of finite parameters analysed "
                "(each varied one at a time from the defaults)",
            )
        programs: list[Program] = []
        promote: set[str] = set()
        for assignment in assignments:
            prog = Program(
                api_level=api_level,
                params=tuple(specs),
                assignment=assignment,
                symbols=symbols,
                enumerated=enumerated,
                notes=[partial] if partial else [],
            )
            lowerer = _Lowerer(prog, custom, domain)
            lowerer.env = copy.deepcopy(base.env)
            lowerer.env[run.args.args[0].arg] = _Ctx()
            if specs or add_parameters is not None or legacy:
                lowerer.params = {**assignment, **symbols}
                lowerer.legacy = legacy
            try:
                lowerer.body(run.body)
            except (_Return, _Raise):
                pass  # the run ends here (a `raise` is the protocol rejecting its inputs)
            except (_Break, _Continue):
                prog.unsupported = Unsupported(run.lineno, "break/continue outside a loop")
            except _NeedConcrete as need:
                by_name = {p.name: p for p in specs}
                if all(n in by_name and _enumerable(by_name[n]) for n in need.names):
                    promote = need.names
                    break
                prog.unsupported = Unsupported(need.line, need.reason)
            except _Stop as stop:
                prog.unsupported = Unsupported(stop.line, stop.reason)
            except RecursionError:
                prog.unsupported = Unsupported(run.lineno, "expression nesting too deep")
            except Exception as exc:  # noqa: BLE001 - a gap in our model, never a crash
                prog.unsupported = Unsupported(
                    run.lineno, f"internal error (otverify bug): {type(exc).__name__}: {exc}"
                )
            programs.append(prog)
        if not promote:
            return programs
        specs = [_enumerate(p) if p.name in promote else p for p in specs]
