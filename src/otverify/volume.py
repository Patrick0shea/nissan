"""P1 volume safety: interpret lowered Programs over well and tip volumes, for all parameter values.

Volumes are concrete numbers or Z3 terms over interval parameters (D-013). Every safety
condition ("guard") is checked as follows:
- A concrete guard is decided directly.
- A symbolic guard is decided by asking Z3 whether `domain ∧ assumptions ∧ ¬guard` is
  satisfiable. SAT gives a violation with a witness: the smallest violating value of the first
  parameter involved. We also report the range of each parameter over the violating set, and
  whether the default values violate the guard (the H2 "parameter-only" distinction). UNSAT means
  proved. `unknown` is reported as such.

What happens after a violation, so that checking continues and independent bugs are still found:
- Concrete: the state is clamped to what is physically possible (e.g. a full well), as in M1.
- Symbolic (D-019): the violated guard becomes an *assumption* attached to the affected well or
  tip state. Later checks that read that state only consider parameter values for which the
  earlier guard held. This is clamping without nested `If` terms (which made Z3 slow), minus
  duplicate reports of the same overflow. Assumptions flow with the liquid: a well filled from a
  tip inherits the tip's assumptions.

A well's starting volume is known only if the protocol declares it with `load_liquid`. Otherwise
the starting volume is unknown, and we track a *lower bound* that starts at 0 (D-009):
- An overflow is reported only when even the lower bound exceeds capacity. That is a certain
  bug, whatever the starting volume was.
- An aspirate from a well with unknown contents is not checked. It is counted in
  `Result.unchecked_aspirations` instead, so the gap is visible.

Semantics confirmed against opentrons 9.0.0 (D-012, D-018):
- `aspirate(None)` fills the tip. `aspirate(0)` does the same below API 2.16, and nothing from 2.16.
- `dispense(None)` empties the tip. `dispense(0)` does the same up to API 2.16, and nothing
  from 2.17.
- A dispense of more than the tip holds is an error from API 2.17. Up to 2.16 it empties the tip.
- The tip's capacity is min(pipette max volume, tip capacity). Air gaps count against it.
- `blow_out` expels the tip contents into its location (a well, or the trash).
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from fractions import Fraction

import z3

from otverify.model import (
    AirGap,
    Aspirate,
    BlowOut,
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

# A set of assumptions, kept canonical (sorted by Z3 AST id, no duplicates) for cache keys.
Assume = tuple[z3.BoolRef, ...]


def _merge(*sets: Assume) -> Assume:
    nonempty = [s for s in sets if s]
    if len(nonempty) <= 1:
        return nonempty[0] if nonempty else ()
    if all(s is nonempty[0] for s in nonempty):
        return nonempty[0]
    by_id = {a.get_id(): a for s in nonempty for a in s}
    return tuple(by_id[k] for k in sorted(by_id))


@dataclass(frozen=True)
class Interval:
    """The values of one parameter over a violating set; None bounds are unknown."""

    lo: Fraction | None
    lo_open: bool
    hi: Fraction | None
    hi_open: bool
    integer: bool

    def __str__(self) -> str:
        left = (
            f"{'(' if self.lo_open else '['}{_fmt_param(self.lo)}" if self.lo is not None else "(?"
        )
        right = (
            f"{_fmt_param(self.hi)}{')' if self.hi_open else ']'}" if self.hi is not None else "?)"
        )
        return f"{left}, {right}"


def union(intervals: list[Interval]) -> list[Interval]:
    """Merge overlapping or touching intervals (for integers, [a, b] and [b + 1, c] touch)."""
    known = sorted((i for i in intervals if i.lo is not None and i.hi is not None), key=_lo_key)
    merged: list[Interval] = []
    for i in known:
        if merged and _touches(merged[-1], i):
            last = merged[-1]
            if (i.hi, not i.hi_open) > (last.hi, not last.hi_open):  # type: ignore[operator]
                merged[-1] = Interval(last.lo, last.lo_open, i.hi, i.hi_open, last.integer)
        else:
            merged.append(i)
    return merged + [i for i in intervals if i.lo is None or i.hi is None]


def _lo_key(i: Interval) -> tuple[Fraction, bool]:
    return (i.lo, i.lo_open)  # type: ignore[return-value]


def _touches(a: Interval, b: Interval) -> bool:
    assert a.hi is not None and b.lo is not None
    if a.integer and b.integer:
        return b.lo <= a.hi + 1
    return b.lo < a.hi or (b.lo == a.hi and not (a.hi_open and b.lo_open))


@dataclass(frozen=True)
class Finding:
    property: str
    severity: str  # "violation" | "warning" | "unknown" (solver could not decide)
    line: int
    context: Context
    message: str  # amounts are evaluated at the witness
    witness: tuple[tuple[str, object], ...] = ()  # parameter values that trigger it
    # Per parameter: its values over the violating set. With assumptions (D-019) this is the set
    # where this check is the first failure of its state; `union` over a line's findings gives
    # the line's full violating set.
    ranges: tuple[tuple[str, Interval], ...] = ()
    at_default: bool = True  # also happens with every parameter at its default


@dataclass
class Result:
    findings: list[Finding] = field(default_factory=list)
    unchecked_aspirations: Counter[str] = field(default_factory=Counter)
    unsupported: list[Unsupported] = field(default_factory=list)
    # Solver outcomes by (assumptions, guard, amounts), shared across the Programs of one
    # protocol. The key's Z3 terms are kept alive in the value, so their ids cannot be reused.
    _cache: dict[object, tuple[object, ...]] = field(default_factory=dict, repr=False)

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
    assume: Assume = ()
    # Unknown wells only: an upper bound on the contents. With the capacity it bounds what an
    # aspirate can take (D-023). Kept unclamped (no If terms); the guard also uses the capacity.
    upper: Num = 0.0


@dataclass
class _Tip:
    attached: bool = False
    liquid: Num = 0.0  # per channel
    air: Num = 0.0  # per channel; air gaps count against tip capacity (D-018)
    assume: Assume = ()


def _fmt(v: object) -> str:
    if isinstance(v, int | float | Fraction) and not isinstance(v, bool):
        return f"{float(v):g} µL"
    return f"{v} µL"


def _fmt_param(v: object) -> str:
    if isinstance(v, float | Fraction):
        return f"{float(v):g}"
    return repr(v) if isinstance(v, str) else str(v)


def _is_zero_const(v: Num) -> bool:
    return not is_sym(v) and v == 0


class _Checker:
    def __init__(self, prog: Program, result: Result) -> None:
        self.prog = prog
        self.result = result
        self.api = prog.api_level or (2, 0)
        self.defs = [lw.definition for lw in prog.labware]
        self.wells: dict[WellRef, _Well] = {}
        self.tips = [_Tip() for _ in prog.pipettes]
        specs = {p.name: p for p in prog.params}
        self.domain = [
            z3.And(s >= specs[n].minimum, s <= specs[n].maximum) for n, s in prog.symbols.items()
        ] + list(prog.path)
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
        assume: Assume = (),
        severity: str = "violation",
    ) -> Cond | None:
        """Report unless `guard` holds for every parameter value satisfying the domain and
        `assume`. Returns None if it holds; else the guard (False when concrete), which the
        caller turns into a clamp (concrete) or an assumption (symbolic)."""
        if isinstance(guard, bool):
            if guard:
                return None
            self.add(step, prop, severity, template.format(*(_fmt(a) for a in amounts)))
            return False
        # Z3 hash-conses terms, so the same guard built in another Program has the same id: look
        # it up before simplifying or solving.
        amounts = tuple(amounts)
        key = (
            tuple(a.get_id() for a in assume),
            guard.get_id(),
            tuple(a.get_id() if is_sym(a) else a for a in amounts),
        )
        if key not in self.result._cache:
            g = z3.simplify(guard)
            outcome = ("unsat",) if z3.is_true(g) else self.solve(g, amounts, assume)
            self.result._cache[key] = (assume, guard, amounts, g, *outcome)
        _, _, _, g, verdict, *outcome = self.result._cache[key]
        if verdict == "unsat":
            return None
        if verdict == "unknown":
            text, reason = outcome
            self.add(step, prop, "unknown", f"{template.format(*text)} (solver: {reason})")
            return g
        witness, rendered, ranges = outcome
        at_default = False
        if self.default_assignment:
            first_failure = z3.And(*self.prog.path, *assume, z3.Not(g))
            if self.defaults:
                first_failure = z3.substitute(first_failure, *self.defaults)
            at_default = z3.is_true(z3.simplify(first_failure))
        self.add(step, prop, severity, template.format(*rendered), witness, ranges, at_default)
        return g

    def solve(self, g: z3.BoolRef, amounts: tuple[Num, ...], assume: Assume) -> tuple[object, ...]:
        """("unsat",), ("unknown", amounts as text, reason) or ("sat", witness, amounts, ranges)."""
        solver = z3.Solver()
        solver.set("timeout", SOLVER_TIMEOUT_MS)
        solver.add(*self.domain, *assume, z3.Not(g))
        verdict = solver.check()
        if verdict == z3.unsat:
            return ("unsat",)
        if verdict == z3.unknown:
            text = [str(z3.simplify(a)) if is_sym(a) else _fmt(a) for a in amounts]
            return ("unknown", text, solver.reason_unknown())
        names = sorted(free_symbols(g))
        model = self.smallest_witness(g, assume, names[0]) if names else None
        model = model or solver.model()
        witness = tuple(
            (n, value_of(model.eval(self.prog.symbols[n], model_completion=True))) for n in names
        )
        rendered = [
            _fmt(value_of(model.eval(a, model_completion=True))) if is_sym(a) else _fmt(a)
            for a in amounts
        ]
        ranges = tuple((n, self.violating_range(g, assume, n)) for n in names)
        return ("sat", witness, rendered, ranges)

    def add(
        self,
        step: Step,
        prop: str,
        severity: str,
        message: str,
        witness: tuple[tuple[str, object], ...] = (),
        ranges: tuple[tuple[str, Interval], ...] = (),
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

    def optimize(
        self, g: z3.BoolRef, assume: Assume, name: str, minimize: bool
    ) -> z3.Optimize | None:
        opt = z3.Optimize()
        opt.set("timeout", SOLVER_TIMEOUT_MS)
        opt.add(*self.domain, *assume, z3.Not(g))
        sym = self.prog.symbols[name]
        handle = opt.minimize(sym) if minimize else opt.maximize(sym)
        if opt.check() != z3.sat:
            return None
        opt.handle = handle  # type: ignore[attr-defined]
        return opt

    def smallest_witness(self, g: z3.BoolRef, assume: Assume, name: str) -> z3.ModelRef | None:
        opt = self.optimize(g, assume, name, minimize=True)
        return opt.model() if opt else None

    def violating_range(self, g: z3.BoolRef, assume: Assume, name: str) -> Interval:
        lo_opt = self.optimize(g, assume, name, minimize=True)
        hi_opt = self.optimize(g, assume, name, minimize=False)
        lo = bound(lo_opt.lower(lo_opt.handle)) if lo_opt else None  # type: ignore[attr-defined]
        hi = bound(hi_opt.upper(hi_opt.handle)) if hi_opt else None  # type: ignore[attr-defined]
        return Interval(
            lo[0] if lo else None,
            bool(lo and lo[1]),
            hi[0] if hi else None,
            bool(hi and hi[1]),
            self.prog.symbols[name].is_int(),
        )

    def may_be_zero(self, v: Num, assume: Assume) -> bool:
        """Whether volume `v` can be exactly 0 (matters for the API < 2.16/2.17 zero quirks)."""
        if not is_sym(v):
            return v == 0
        key = ("zero", tuple(a.get_id() for a in assume), v.get_id())
        if key not in self.result._cache:
            solver = z3.Solver()
            solver.set("timeout", SOLVER_TIMEOUT_MS)
            solver.add(*self.domain, *assume, v == 0)
            self.result._cache[key] = (assume, v, solver.check() != z3.unsat)
        return bool(self.result._cache[key][-1])

    # ---- state ------------------------------------------------------------------------------

    def label(self, w: WellRef) -> str:
        lw = self.prog.labware[w.labware]
        return f"{w.well} of {lw.load_name} (slot {lw.location})"

    def capacity(self, w: WellRef) -> float:
        return self.defs[w.labware].capacities[w.well]

    def state(self, w: WellRef) -> _Well:
        if w not in self.wells:
            self.wells[w] = _Well(known=False, upper=self.capacity(w))
        return self.wells[w]

    def run(self) -> None:
        for line, ctx, prop, severity, message in self.prog.lint:
            self.add(Step(line, ctx), prop, severity, message)
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
                case AirGap():
                    self.air_gap(step)
                case BlowOut():
                    self.blow_out(step)

    def load_liquid(self, step: LoadLiquid) -> None:
        cap = self.capacity(step.well)
        label = self.label(step.well)
        bad = self.require(
            step,
            OVERFLOW,
            step.volume <= cap + EPS,
            f"load_liquid puts {{0}} in {label}, capacity {_fmt(cap)}",
            [step.volume],
        )
        volume = cap if bad is False else step.volume
        self.wells[step.well] = _Well(True, volume, () if bad is None or bad is False else (bad,))

    def tip(self, step: Step, pipette: int, action: str) -> _Tip | None:
        tip = self.tips[pipette]
        if not tip.attached:
            self.add(step, NO_TIP, "violation", f"{action} without a tip attached")
            return None
        return tip

    def take_in(self, step: Step, tip: _Tip, pipette: int, volume: Num | None, what: str) -> Num:
        """Check and return how much liquid or air an aspirate/air gap of `volume` draws in.
        A symbolic violation is added to the tip's assumptions."""
        tip_cap = self.prog.pipettes[pipette].tip_capacity
        held = tip.liquid + tip.air
        room = tip_cap - held
        if volume is None:
            volume = room
        elif self.api < (2, 16) and self.may_be_zero(volume, tip.assume):
            volume = ite(volume == 0, room, volume)
        bad = self.require(
            step,
            TIP_VOLUME,
            volume <= room + EPS,
            f"{what} {{0}} into a tip holding {{1}} of {_fmt(tip_cap)}",
            [volume, held],
            tip.assume,
        )
        if bad is False:
            return room
        if bad is not None:
            tip.assume = _merge(tip.assume, (bad,))
        return volume

    def aspirate(self, step: Aspirate) -> None:
        if (tip := self.tip(step, step.pipette, "aspirate")) is None:
            return
        volume = self.take_in(step, tip, step.pipette, step.volume, "aspirating")
        taken = volume
        for w in step.wells:
            st = self.state(w)
            label = self.label(w)
            if not st.known:
                # The contents are unknown, but at most min(capacity, upper bound): asking for
                # more is a certain overdraw. Anything less is assumed available (D-009).
                self.result.unchecked_aspirations[label] += 1
                cap = self.capacity(w)
                assume = _merge(st.assume, tip.assume)
                guard = _conj(volume <= cap + EPS, volume <= st.upper + EPS)
                bad = self.require(
                    step,
                    OVERDRAW,
                    guard,
                    f"aspirating {{0}} from {label}, which can hold at most {{1}}",
                    [volume, smin(cap, st.upper)],  # an If only for rendering the message
                    assume,
                )
                if bad is False:
                    taken = _min_const(cap, st.upper, volume)
                elif bad is not None:
                    st.assume = _merge(assume, (bad,))
                    tip.assume = _merge(tip.assume, (bad,))
                st.upper = st.upper - taken if not is_sym(taken) else st.upper - volume
                # A lower bound never goes below 0; a well at 0 stays at 0 without an If.
                if not _is_zero_const(st.volume):
                    st.volume = smax(0.0, st.volume - volume)
                continue
            assume = _merge(st.assume, tip.assume)
            bad = self.require(
                step,
                OVERDRAW,
                volume <= st.volume + EPS,
                f"aspirating {{0}} from {label}, which holds {{1}}",
                [volume, st.volume],
                assume,
            )
            if bad is False:
                st.volume = smax(0.0, st.volume - volume)
            else:
                st.volume = st.volume - volume
                st.assume = assume if bad is None else _merge(assume, (bad,))
        tip.liquid = tip.liquid + (taken if not is_sym(taken) else volume)

    def air_gap(self, step: AirGap) -> None:
        if (tip := self.tip(step, step.pipette, "air gap")) is None:
            return
        # air_gap(0) is never "fill the tip": only aspirate() has that legacy quirk.
        if step.volume is not None and _is_zero_const(step.volume):
            return
        tip.air = tip.air + self.take_in(step, tip, step.pipette, step.volume, "air gap of")

    def dispense(self, step: Dispense) -> None:
        if (tip := self.tip(step, step.pipette, "dispense")) is None:
            return
        held = tip.liquid + tip.air
        if step.volume is None:
            volume: Num = held
        elif self.api <= (2, 16) and self.may_be_zero(step.volume, tip.assume):
            volume = ite(step.volume == 0, held, step.volume)
        else:
            volume = step.volume
        if self.api >= (2, 17):
            note, severity = "", "violation"
        else:
            note, severity = " (API < 2.17 dispenses the tip contents)", "warning"
        bad = self.require(
            step,
            TIP_VOLUME,
            volume <= held + EPS,
            f"dispensing {{0}} from a tip holding {{1}}{note}",
            [volume, held],
            tip.assume,
            severity,
        )
        if bad is False:
            volume = held
        elif bad is not None:
            tip.assume = _merge(tip.assume, (bad,))
        # The air gap was drawn in last, so it sits at the tip opening and leaves first.
        if _is_zero_const(tip.air):
            air_out: Num = 0.0
        else:
            air_out = ite(tip.air <= volume, tip.air, volume)
        liquid_out = volume - air_out
        tip.air = tip.air - air_out
        tip.liquid = tip.liquid - liquid_out
        self.deliver(step, step.wells, liquid_out, tip.assume)

    def blow_out(self, step: BlowOut) -> None:
        if (tip := self.tip(step, step.pipette, "blow out")) is None:
            return
        self.deliver(step, step.wells, tip.liquid, tip.assume)
        tip.liquid, tip.air = 0.0, 0.0

    def deliver(self, step: Step, wells: tuple[WellRef, ...], volume: Num, assume: Assume) -> None:
        """Add `volume` (per channel) to each well; no wells means the trash."""
        for w in wells:
            st = self.state(w)
            cap = self.capacity(w)
            label = self.label(w)
            qualifier = "" if st.known else "at least "
            suffix = "" if st.known else " (even if it started empty)"
            after = st.volume + volume
            merged = _merge(st.assume, assume)
            bad = self.require(
                step,
                OVERFLOW,
                after <= cap + EPS,
                f"{label} would hold {qualifier}{{0}}{suffix}, capacity {_fmt(cap)}",
                [after],
                merged,
            )
            if bad is False:
                st.volume = cap
            else:
                st.volume = after
                st.assume = merged if bad is None else _merge(merged, (bad,))
            if not st.known:
                st.upper = st.upper + volume


def _conj(a: Cond, b: Cond) -> Cond:
    if isinstance(a, bool) and isinstance(b, bool):
        return a and b
    return z3.And(a, b)


def _min_const(*values: Num) -> Num:
    """min() of the concrete values (symbolic ones are left to the guard)."""
    concrete = [v for v in values if not is_sym(v)]
    return min(concrete) if len(concrete) == len(values) else values[0]


def _const(sym: z3.ArithRef, value: object) -> z3.ArithRef:
    return z3.IntVal(value) if sym.is_int() else z3.RealVal(value)  # type: ignore[arg-type]


def _fingerprint(prog: Program) -> tuple[object, ...]:
    """Everything the checker reads, except the parameter assignment. Programs that differ only
    in parameters with no effect on volumes (e.g. the pipette mount) have equal fingerprints."""

    def value(v: object) -> object:
        return ("z3", v.get_id()) if is_sym(v) else v  # type: ignore[attr-defined]

    steps = tuple(
        (type(s).__name__, s.line, s.context)
        + tuple(
            value(getattr(s, f)) for f in ("pipette", "well", "wells", "volume") if hasattr(s, f)
        )
        for s in prog.steps
    )
    labware = tuple(lw.load_name for lw in prog.labware)
    pipettes = tuple((p.channels, p.tip_capacity) for p in prog.pipettes)
    path = tuple(c.get_id() for c in prog.path)
    return (prog.api_level, labware, pipettes, steps, path, prog.unsupported)


def check_volumes(programs: Program | list[Program]) -> Result:
    """Check P1 on every Program (one per finite-parameter assignment) and merge the findings.

    Programs whose steps are identical to an earlier one are skipped: they produce the same
    findings, and the earlier one is closer to the defaults (programs come defaults-first)."""
    if isinstance(programs, Program):
        programs = [programs]
    result = Result()
    seen: set[tuple[object, ...]] = set()
    for prog in programs:
        fingerprint = _fingerprint(prog)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        _Checker(prog, result).run()
        for gap in [prog.unsupported, *prog.notes]:
            if gap and gap not in result.unsupported:
                result.unsupported.append(gap)
    return result
