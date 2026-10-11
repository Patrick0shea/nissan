"""The lowered protocol: loaded resources plus a flat list of liquid-handling steps."""

from dataclasses import dataclass, field

import z3

from otverify.definitions import LabwareDef

# Loop variable bindings active when a step was emitted, e.g. (("i", 2),).
Context = tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class ParamSpec:
    """A runtime parameter from add_parameters(). Its domain is finite or a closed interval."""

    name: str
    kind: str  # "int" | "float" | "bool" | "str"
    default: object
    minimum: float | None  # interval parameters only (inclusive)
    maximum: float | None
    choices: tuple[object, ...] | None  # finite parameters only; bool is (False, True)
    line: int
    # Where the domain came from: "declared" (add_parameters), and for legacy fields.json
    # (D-016) "options" (dropDown), "label" (range parsed from the label) or "default"
    # (no range known: analysed at the default value only).
    source: str = "declared"

    @property
    def finite(self) -> bool:
        return self.choices is not None


@dataclass(frozen=True)
class WellRef:
    labware: int  # index into Program.labware
    well: str

    def __str__(self) -> str:
        # Like Opentrons' str(well), "A1 of <labware> on <slot>", whose first word protocols
        # sometimes parse. The labware's display name is not modelled.
        return f"{self.well} of labware {self.labware}"


@dataclass(frozen=True)
class LoadedLabware:
    load_name: str
    location: str
    line: int
    definition: "LabwareDef"  # from the snapshot, or the protocol's custom labware JSON


@dataclass(frozen=True)
class LoadedPipette:
    name: str
    mount: str
    tip_racks: tuple[int, ...]  # indices into Program.labware
    channels: int
    tip_capacity: float  # min(pipette max volume, tip capacity) in µL
    line: int


@dataclass(frozen=True)
class Step:
    line: int
    context: Context


@dataclass(frozen=True)
class LoadLiquid(Step):
    well: WellRef
    volume: "float | z3.ArithRef"


@dataclass(frozen=True)
class PickUpTip(Step):
    pipette: int


@dataclass(frozen=True)
class DropTip(Step):
    pipette: int


@dataclass(frozen=True)
class Aspirate(Step):
    pipette: int
    wells: tuple[WellRef, ...]  # one per channel; a multichannel in a reservoir repeats the well
    volume: "float | z3.ArithRef | None"  # None: "as much as the tip holds"


@dataclass(frozen=True)
class Dispense(Step):
    pipette: int
    wells: tuple[WellRef, ...]  # one per channel, as for Aspirate; () means the trash
    volume: "float | z3.ArithRef | None"  # None: "everything in the tip"


@dataclass(frozen=True)
class AirGap(Step):
    pipette: int
    volume: "float | z3.ArithRef | None"  # None: fill the rest of the tip with air


@dataclass(frozen=True)
class BlowOut(Step):
    pipette: int
    wells: tuple[WellRef, ...]  # where the tip contents go; () means the trash


@dataclass(frozen=True)
class Pause(Step):
    """protocol.pause(): a person may refill, empty or replace any well before resuming."""


@dataclass(frozen=True)
class Unsupported:
    line: int
    reason: str


@dataclass
class Program:
    api_level: tuple[int, int] | None = None
    params: tuple[ParamSpec, ...] = ()
    # One Program is lowered per combination of finite-parameter values (`assignment`).
    # Interval parameters stay symbolic as Z3 constants (`symbols`).
    assignment: dict[str, object] = field(default_factory=dict)
    symbols: dict[str, z3.ArithRef] = field(default_factory=dict)
    # Interval parameters that reached a concrete-only position (loop bound, index, branch) and
    # were therefore enumerated exhaustively instead of solved symbolically (D-017).
    enumerated: tuple[str, ...] = ()
    # Conditions on interval parameters under which this Program's run is the real one: e.g.
    # `if vol > 200: raise ...` adds `vol <= 200` (values the protocol itself rejects are out).
    path: list[z3.BoolRef] = field(default_factory=list)
    # Analysis gaps that do not truncate the steps (e.g. only some finite-parameter combinations
    # analysed). Reported like `unsupported`.
    notes: list["Unsupported"] = field(default_factory=list)
    # Findings the front end makes itself (line, context, property, severity, message): e.g. a
    # transfer that Opentrons rejects at runtime, or a keyword argument it silently ignores.
    lint: list[tuple[int, Context, str, str, str]] = field(default_factory=list)
    labware: list[LoadedLabware] = field(default_factory=list)
    pipettes: list[LoadedPipette] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    # The first construct the front end could not model. Analysis covers only the steps before it.
    unsupported: Unsupported | None = None
