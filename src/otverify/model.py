"""The lowered protocol: loaded resources plus a flat list of liquid-handling steps."""

from dataclasses import dataclass, field

import z3

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

    @property
    def finite(self) -> bool:
        return self.choices is not None


@dataclass(frozen=True)
class WellRef:
    labware: int  # index into Program.labware
    well: str


@dataclass(frozen=True)
class LoadedLabware:
    load_name: str
    location: str
    line: int


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
    wells: tuple[WellRef, ...]  # one per channel; a multichannel in a reservoir repeats the well
    volume: "float | z3.ArithRef | None"  # None: "everything in the tip"


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
    labware: list[LoadedLabware] = field(default_factory=list)
    pipettes: list[LoadedPipette] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    # The first construct the front end could not model. Analysis covers only the steps before it.
    unsupported: Unsupported | None = None
