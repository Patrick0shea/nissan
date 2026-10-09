"""P1 volume safety, concrete version (M1): interpret a lowered Program over well and tip volumes.

A well's starting volume is known only if the protocol declares it with `load_liquid`. Otherwise
the starting volume is unknown, and we track a *lower bound* that starts at 0 (D-009):
- An overflow is reported only when even the lower bound exceeds capacity. That is a certain
  bug, whatever the starting volume was.
- An aspirate from a well with unknown contents is not checked. It is counted in
  `Result.unchecked_aspirations` instead, so the gap is visible.

API-level-dependent semantics confirmed against opentrons 9.0.0 (docstrings and simulator probes):
- `aspirate(None)` fills the tip. `aspirate(0)` does the same below API 2.16, and nothing from 2.16.
- `dispense(None)` empties the tip. `dispense(0)` does the same up to API 2.16, and nothing
  from 2.17.
- A dispense of more than the tip holds is an error from API 2.17. Up to 2.16 it empties the tip.
- The tip's capacity is min(pipette max volume, tip capacity).
"""

from collections import Counter
from dataclasses import dataclass, field

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

EPS = 1e-6

OVERFLOW = "P1a"  # a well receives more than its capacity
OVERDRAW = "P1b"  # aspirating more than a well contains
TIP_VOLUME = "P1c"  # tip capacity exceeded, or dispensing more than the tip holds
NO_TIP = "TIP"  # liquid handling without a tip attached


@dataclass(frozen=True)
class Finding:
    property: str
    severity: str  # "violation" | "warning"
    line: int
    context: Context
    message: str


@dataclass
class Result:
    findings: list[Finding] = field(default_factory=list)
    unchecked_aspirations: Counter[str] = field(default_factory=Counter)
    unsupported: Unsupported | None = None

    @property
    def violations(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "violation"]


@dataclass
class _Well:
    known: bool  # True: `volume` is exact. False: `volume` is a lower bound.
    volume: float = 0.0


@dataclass
class _Tip:
    attached: bool = False
    volume: float = 0.0  # per channel


def _fmt(v: float) -> str:
    return f"{v:g} µL"


class _Checker:
    def __init__(self, prog: Program) -> None:
        self.prog = prog
        self.api = prog.api_level or (2, 0)
        self.defs = [definitions.labware(lw.load_name) for lw in prog.labware]
        self.wells: dict[WellRef, _Well] = {}
        self.tips = [_Tip() for _ in prog.pipettes]
        self.result = Result(unsupported=prog.unsupported)

    def report(self, step: Step, prop: str, message: str, severity: str = "violation") -> None:
        self.result.findings.append(Finding(prop, severity, step.line, step.context, message))

    def label(self, w: WellRef) -> str:
        lw = self.prog.labware[w.labware]
        return f"{w.well} of {lw.load_name} (slot {lw.location})"

    def capacity(self, w: WellRef) -> float:
        lw_def = self.defs[w.labware]
        assert lw_def is not None  # the front end only emits steps for known labware
        return lw_def.capacities[w.well]

    def state(self, w: WellRef) -> _Well:
        return self.wells.setdefault(w, _Well(known=False))

    def run(self) -> Result:
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
        return self.result

    def load_liquid(self, step: LoadLiquid) -> None:
        cap = self.capacity(step.well)
        if step.volume > cap + EPS:
            self.report(
                step,
                OVERFLOW,
                f"load_liquid puts {_fmt(step.volume)} in {self.label(step.well)}, "
                f"capacity {_fmt(cap)}",
            )
        self.wells[step.well] = _Well(known=True, volume=min(step.volume, cap))

    def aspirate(self, step: Aspirate) -> None:
        tip = self.tips[step.pipette]
        if not tip.attached:
            self.report(step, NO_TIP, "aspirate without a tip attached")
            return
        room = self.prog.pipettes[step.pipette].tip_capacity - tip.volume
        volume = step.volume
        if volume == 0 and self.api < (2, 16):
            volume = None
        if volume is None:
            volume = room
        if volume > room + EPS:
            self.report(
                step,
                TIP_VOLUME,
                f"aspirating {_fmt(volume)} into a tip holding {_fmt(tip.volume)} "
                f"of {_fmt(self.prog.pipettes[step.pipette].tip_capacity)}",
            )
            volume = room
        for w in step.wells:
            st = self.state(w)
            if not st.known:
                self.result.unchecked_aspirations[self.label(w)] += 1
            elif volume > st.volume + EPS:
                self.report(
                    step,
                    OVERDRAW,
                    f"aspirating {_fmt(volume)} from {self.label(w)}, "
                    f"which holds {_fmt(st.volume)}",
                )
            st.volume = max(0.0, st.volume - volume)
        tip.volume += volume

    def dispense(self, step: Dispense) -> None:
        tip = self.tips[step.pipette]
        if not tip.attached:
            self.report(step, NO_TIP, "dispense without a tip attached")
            return
        volume = step.volume
        if volume == 0 and self.api <= (2, 16):
            volume = None
        if volume is None:
            volume = tip.volume
        if volume > tip.volume + EPS:
            message = f"dispensing {_fmt(volume)} from a tip holding {_fmt(tip.volume)}"
            if self.api >= (2, 17):
                self.report(step, TIP_VOLUME, message)
            else:
                self.report(
                    step,
                    TIP_VOLUME,
                    message + " (API < 2.17 dispenses the tip contents)",
                    "warning",
                )
            volume = tip.volume
        for w in step.wells:
            st = self.state(w)
            cap = self.capacity(w)
            if st.volume + volume > cap + EPS:
                amount = _fmt(st.volume + volume)
                if not st.known:
                    amount = f"at least {amount} (even if it started empty)"
                self.report(
                    step,
                    OVERFLOW,
                    f"{self.label(w)} would hold {amount}, capacity {_fmt(cap)}",
                )
            st.volume = min(cap, st.volume + volume)
        tip.volume -= volume


def check_volumes(prog: Program) -> Result:
    return _Checker(prog).run()
