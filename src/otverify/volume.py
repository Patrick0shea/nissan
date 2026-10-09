"""P1 volume safety: interpret lowered Programs over well and tip volumes, for all parameter values.

Volumes are concrete numbers or Z3 terms over interval parameters (D-013). Every safety
condition ("guard") is checked as follows:
- A concrete guard is decided directly.
- A symbolic guard is decided by asking Z3 whether `domain ∧ ¬guard` is satisfiable. SAT gives a
  violation with a witness: the smallest violating value of the first parameter involved. We also
  report the range of each parameter over the violating set, and whether the default values
  violate the guard (the H2 "parameter-only" distinction). UNSAT means proved. `unknown` is
  reported as such.

After a violation the state is clamped to what is physically possible (e.g. a full well) and
checking continues, so independent bugs are all reported.

A well's starting volume is known only if the protocol declares it with `load_liquid`. Otherwise
the starting volume is unknown, and we track a *lower bound* that starts at 0 (D-009):
- An overflow is reported only when even the lower bound exceeds capacity. That is a certain
  bug, whatever the starting volume was.
- An aspirate from a well with unknown contents is not checked. It is counted in
  `Result.unchecked_aspirations` instead, so the gap is visible.

API-level-dependent semantics confirmed against opentrons 9.0.0 (D-012):
- `aspirate(None)` fills the tip. `aspirate(0)` does the same below API 2.16, and nothing from 2.16.
- `dispense(None)` empties the tip. `dispense(0)` does the same up to API 2.16, and nothing
  from 2.17.
- A dispense of more than the tip holds is an error from API 2.17. Up to 2.16 it empties the tip.
- The tip's capacity is min(pipette max volume, tip capacity).
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from fractions import Fraction

import z3

from otverify import definitions
from otverify.model import (
    Aspirate,
    Context,
    Dispense,
    DropTip,
    LoadLiquid,
    PickUpTip,
    Program,
    Step,
    Unsupported,
    WellRef,
)
from otverify.smt import Cond, Num, bound, free_symbols, is_sym, ite, smax, smin, value_of

EPS = 1e-6
SOLVER_TIMEOUT_MS = 10_000

OVERFLOW = "P1a"  # a well receives more than its capacity
OVERDRAW = "P1b"  # aspirating more than a well contains
TIP_VOLUME = "P1c"  # tip capacity exceeded, or dispensing more than the tip holds
NO_TIP = "TIP"  # liquid handling without a tip attached


@dataclass(frozen=True)
class Finding:
    property: str
    severity: str  # "violation" | "warning" | "unknown" (solver could not decide)
    line: int
    context: Context
    message: str  # amounts are evaluated at the witness
    witness: tuple[tuple[str, object], ...] = ()  # parameter values that trigger it
    ranges: tuple[tuple[str, str], ...] = ()  # per parameter: its values over the violating set
    at_default: bool = True  # also happens with every parameter at its default


@dataclass
class Result:
    findings: list[Finding] = field(default_factory=list)
    unchecked_aspirations: Counter[str] = field(default_factory=Counter)
    unsupported: list[Unsupported] = field(default_factory=list)

    @property
    def violations(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "violation"]

    @property
    def undecided(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "unknown"]


@dataclass
class _Well:
    known: bool  # True: `volume` is exact. False: `volume` is a lower bound.
    volume: Num = 0.0


@dataclass
class _Tip:
    attached: bool = False
    volume: Num = 0.0  # per channel


def _fmt(v: object) -> str:
    if isinstance(v, int | float | Fraction) and not isinstance(v, bool):
        return f"{float(v):g} µL"
    return f"{v} µL"


def _fmt_param(v: object) -> str:
    if isinstance(v, float | Fraction):
        return f"{float(v):g}"
    return repr(v) if isinstance(v, str) else str(v)


def _is_zero(v: Num) -> Cond:
    return v == 0  # a z3.BoolRef when v is symbolic


class _Checker:
    def __init__(self, prog: Program, result: Result) -> None:
        self.prog = prog
        self.result = result
        self.api = prog.api_level or (2, 0)
        self.defs = [definitions.labware(lw.load_name) for lw in prog.labware]
        self.wells: dict[WellRef, _Well] = {}
        self.tips = [_Tip() for _ in prog.pipettes]
        specs = {p.name: p for p in prog.params}
        self.domain = [
            z3.And(s >= specs[n].minimum, s <= specs[n].maximum) for n, s in prog.symbols.items()
        ]
        self.defaults = [(s, _const(s, specs[n].default)) for n, s in prog.symbols.items()]
        self.default_assignment = all(
            prog.assignment[p.name] == p.default for p in prog.params if p.finite
        )
        # Finite parameters away from their defaults are part of every witness in this Program.
        self.finite_witness = tuple(
            (p.name, prog.assignment[p.name])
            for p in prog.params
            if p.finite and prog.assignment[p.name] != p.default
        )

    # ---- checking a guard -------------------------------------------------------------------

    def require(
        self,
        step: Step,
        prop: str,
        guard: Cond,
        template: str,  # str.format template; {0}, {1}, ... are the formatted `amounts`
        amounts: Sequence[Num] = (),
        severity: str = "violation",
    ) -> None:
        """Report unless `guard` holds for every parameter value in the domain."""
        if isinstance(guard, bool):
            if not guard:
                self.add(step, prop, severity, template.format(*(_fmt(a) for a in amounts)))
            return
        g = z3.simplify(guard)
        if z3.is_true(g):
            return
        solver = z3.Solver()
        solver.set("timeout", SOLVER_TIMEOUT_MS)
        solver.add(*self.domain, z3.Not(g))
        verdict = solver.check()
        if verdict == z3.unsat:
            return
        if verdict == z3.unknown:
            text = template.format(
                *(str(z3.simplify(a)) if is_sym(a) else _fmt(a) for a in amounts)
            )
            self.add(step, prop, "unknown", f"{text} (solver: {solver.reason_unknown()})")
            return
        names = sorted(free_symbols(g))
        model = self.smallest_witness(g, names[0]) if names else None
        model = model or solver.model()
        witness = tuple(
            (n, value_of(model.eval(self.prog.symbols[n], model_completion=True))) for n in names
        )
        rendered = [
            _fmt(value_of(model.eval(a, model_completion=True))) if is_sym(a) else _fmt(a)
            for a in amounts
        ]
        at_default = self.default_assignment and z3.is_true(
            z3.simplify(z3.substitute(z3.Not(g), *self.defaults))
            if self.defaults
            else z3.simplify(z3.Not(g))
        )
        ranges = tuple((n, self.violating_range(g, n)) for n in names)
        self.add(step, prop, severity, template.format(*rendered), witness, ranges, at_default)

    def add(
        self,
        step: Step,
        prop: str,
        severity: str,
        message: str,
        witness: tuple[tuple[str, object], ...] = (),
        ranges: tuple[tuple[str, str], ...] = (),
        at_default: bool | None = None,
    ) -> None:
        if at_default is None:
            at_default = self.default_assignment
        self.result.findings.append(
            Finding(
                prop,
                severity,
                step.line,
                step.context,
                message,
                self.finite_witness + witness,
                ranges,
                at_default,
            )
        )

    def optimize(self, g: z3.BoolRef, name: str, minimize: bool) -> z3.Optimize | None:
        opt = z3.Optimize()
        opt.set("timeout", SOLVER_TIMEOUT_MS)
        opt.add(*self.domain, z3.Not(g))
        sym = self.prog.symbols[name]
        handle = opt.minimize(sym) if minimize else opt.maximize(sym)
        if opt.check() != z3.sat:
            return None
        opt.handle = handle  # type: ignore[attr-defined]
        return opt

    def smallest_witness(self, g: z3.BoolRef, name: str) -> z3.ModelRef | None:
        opt = self.optimize(g, name, minimize=True)
        return opt.model() if opt else None

    def violating_range(self, g: z3.BoolRef, name: str) -> str:
        lo_opt = self.optimize(g, name, minimize=True)
        hi_opt = self.optimize(g, name, minimize=False)
        lo = bound(lo_opt.lower(lo_opt.handle)) if lo_opt else None  # type: ignore[attr-defined]
        hi = bound(hi_opt.upper(hi_opt.handle)) if hi_opt else None  # type: ignore[attr-defined]
        left = f"{'(' if lo[1] else '['}{_fmt_param(lo[0])}" if lo else "(?"
        right = f"{_fmt_param(hi[0])}{')' if hi[1] else ']'}" if hi else "?)"
        return f"{left}, {right}"

    # ---- state ------------------------------------------------------------------------------

    def label(self, w: WellRef) -> str:
        lw = self.prog.labware[w.labware]
        return f"{w.well} of {lw.load_name} (slot {lw.location})"

    def capacity(self, w: WellRef) -> float:
        lw_def = self.defs[w.labware]
        assert lw_def is not None  # the front end only emits steps for known labware
        return lw_def.capacities[w.well]

    def state(self, w: WellRef) -> _Well:
        return self.wells.setdefault(w, _Well(known=False))

    def run(self) -> None:
        for step in self.prog.steps:
            match step:
                case LoadLiquid():
                    self.load_liquid(step)
                case PickUpTip(pipette=p):
                    self.tips[p] = _Tip(attached=True)
                case DropTip(pipette=p):
                    self.tips[p] = _Tip()
                case Aspirate():
                    self.aspirate(step)
                case Dispense():
                    self.dispense(step)

    def load_liquid(self, step: LoadLiquid) -> None:
        cap = self.capacity(step.well)
        label = self.label(step.well)
        self.require(
            step,
            OVERFLOW,
            step.volume <= cap + EPS,
            f"load_liquid puts {{0}} in {label}, capacity {_fmt(cap)}",
            [step.volume],
        )
        self.wells[step.well] = _Well(known=True, volume=smin(step.volume, cap))

    def aspirate(self, step: Aspirate) -> None:
        tip = self.tips[step.pipette]
        if not tip.attached:
            self.add(step, NO_TIP, "violation", "aspirate without a tip attached")
            return
        tip_cap = self.prog.pipettes[step.pipette].tip_capacity
        room = tip_cap - tip.volume
        if step.volume is None:
            volume: Num = room
        elif self.api < (2, 16):
            volume = ite(_is_zero(step.volume), room, step.volume)
        else:
            volume = step.volume
        held = tip.volume
        self.require(
            step,
            TIP_VOLUME,
            volume <= room + EPS,
            f"aspirating {{0}} into a tip holding {{1}} of {_fmt(tip_cap)}",
            [volume, held],
        )
        volume = smin(volume, room)
        for w in step.wells:
            st = self.state(w)
            label = self.label(w)
            if not st.known:
                self.result.unchecked_aspirations[label] += 1
            else:
                self.require(
                    step,
                    OVERDRAW,
                    volume <= st.volume + EPS,
                    f"aspirating {{0}} from {label}, which holds {{1}}",
                    [volume, st.volume],
                )
            st.volume = smax(0.0, st.volume - volume)
        tip.volume = tip.volume + volume

    def dispense(self, step: Dispense) -> None:
        tip = self.tips[step.pipette]
        if not tip.attached:
            self.add(step, NO_TIP, "violation", "dispense without a tip attached")
            return
        if step.volume is None:
            volume: Num = tip.volume
        elif self.api <= (2, 16):
            volume = ite(_is_zero(step.volume), tip.volume, step.volume)
        else:
            volume = step.volume
        held = tip.volume
        if self.api >= (2, 17):
            note, severity = "", "violation"
        else:
            note, severity = " (API < 2.17 dispenses the tip contents)", "warning"
        self.require(
            step,
            TIP_VOLUME,
            volume <= held + EPS,
            f"dispensing {{0}} from a tip holding {{1}}{note}",
            [volume, held],
            severity,
        )
        volume = smin(volume, held)
        for w in step.wells:
            st = self.state(w)
            cap = self.capacity(w)
            label = self.label(w)
            qualifier = "" if st.known else "at least "
            suffix = "" if st.known else " (even if it started empty)"
            after = st.volume + volume
            self.require(
                step,
                OVERFLOW,
                after <= cap + EPS,
                f"{label} would hold {qualifier}{{0}}{suffix}, capacity {_fmt(cap)}",
                [after],
            )
            st.volume = smin(cap, after)
        tip.volume = tip.volume - volume


def _const(sym: z3.ArithRef, value: object) -> z3.ArithRef:
    return z3.IntVal(value) if sym.is_int() else z3.RealVal(value)  # type: ignore[arg-type]


def check_volumes(programs: Program | list[Program]) -> Result:
    """Check P1 on every Program (one per finite-parameter assignment) and merge the findings."""
    if isinstance(programs, Program):
        programs = [programs]
    result = Result()
    for prog in programs:
        _Checker(prog, result).run()
        if prog.unsupported and prog.unsupported not in result.unsupported:
            result.unsupported.append(prog.unsupported)
    return result
