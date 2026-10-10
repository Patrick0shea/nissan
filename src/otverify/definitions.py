"""Labware and pipette facts snapshotted from opentrons 9.0.0.

Regenerate with scripts/snapshot_opentrons_data.py.

Source: opentrons-shared-data, Apache-2.0, (c) Opentrons Labworks Inc.
"""

import json
from dataclasses import dataclass, field
from functools import cache
from importlib import resources


@dataclass(frozen=True)
class LabwareDef:
    load_name: str
    is_tiprack: bool
    ordering: tuple[tuple[str, ...], ...]  # columns of well names, as in the definition
    capacities: dict[str, float]  # µL per well (totalLiquidVolume)
    # mm per well: (depth, diameter, width, length) as the API reports them; diameter is None
    # for rectangular wells, width/length None for circular ones (D-024).
    geometry: dict[str, tuple[float | None, ...]] = field(default_factory=dict)

    def wells(self) -> list[str]:
        return [w for col in self.ordering for w in col]

    def columns(self) -> list[list[str]]:
        return [list(col) for col in self.ordering]

    def rows(self) -> list[list[str]]:
        n = max(len(col) for col in self.ordering)
        return [[col[i] for col in self.ordering if i < len(col)] for i in range(n)]


@dataclass(frozen=True)
class PipetteDef:
    name: str
    max_volume: float
    min_volume: float
    channels: int


@cache
def _raw() -> dict:
    text = resources.files("otverify.data").joinpath("opentrons_9_0_0.json").read_text()
    return json.loads(text)


def from_definition(definition: dict) -> LabwareDef:
    """A LabwareDef from a full Opentrons labware definition (e.g. a protocol's custom labware
    JSON, loaded like the Protocol Library build does from `protocols/<name>/labware/*.json`)."""
    ordering = tuple(tuple(col) for col in definition["ordering"])
    caps = {w: float(definition["wells"][w]["totalLiquidVolume"]) for col in ordering for w in col}
    params = definition["parameters"]
    geometry = {w: _geometry(definition["wells"][w]) for col in ordering for w in col}
    return LabwareDef(params["loadName"], bool(params.get("isTiprack")), ordering, caps, geometry)


def _geometry(well: dict) -> tuple[float | None, ...]:
    """Well.width is yDimension and Well.length is xDimension (confirmed in the simulator)."""
    return (well.get("depth"), well.get("diameter"), well.get("yDimension"), well.get("xDimension"))


def labware(load_name: str, custom: dict[str, LabwareDef] | None = None) -> LabwareDef | None:
    if custom and load_name in custom:
        return custom[load_name]
    entry = _raw()["labware"].get(load_name)
    if entry is None:
        return None
    ordering = tuple(tuple(col) for col in entry["ordering"])
    wells = [w for col in ordering for w in col]
    if "capacity" in entry:
        caps = {w: float(entry["capacity"]) for w in wells}
    else:
        caps = {w: float(v) for w, v in entry["capacities"].items()}
    if "geometry" in entry:
        geometry = {w: tuple(entry["geometry"]) for w in wells}
    else:
        geometry = {w: tuple(g) for w, g in entry.get("geometries", {}).items()}
    return LabwareDef(load_name, entry["is_tiprack"], ordering, caps, geometry)


def pipette(name: str) -> PipetteDef | None:
    entry = _raw()["pipettes"].get(name)
    if entry is None:
        return None
    return PipetteDef(
        name, float(entry["max_volume"]), float(entry["min_volume"]), entry["channels"]
    )
