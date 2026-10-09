"""Labware and pipette facts snapshotted from opentrons 9.0.0.

Regenerate with scripts/snapshot_opentrons_data.py.

Source: opentrons-shared-data, Apache-2.0, (c) Opentrons Labworks Inc.
"""

import json
from dataclasses import dataclass
from functools import cache
from importlib import resources


@dataclass(frozen=True)
class LabwareDef:
    load_name: str
    is_tiprack: bool
    ordering: tuple[tuple[str, ...], ...]  # columns of well names, as in the definition
    capacities: dict[str, float]  # µL per well (totalLiquidVolume)

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


def labware(load_name: str) -> LabwareDef | None:
    entry = _raw()["labware"].get(load_name)
    if entry is None:
        return None
    ordering = tuple(tuple(col) for col in entry["ordering"])
    if "capacity" in entry:
        caps = {w: float(entry["capacity"]) for col in ordering for w in col}
    else:
        caps = {w: float(v) for w, v in entry["capacities"].items()}
    return LabwareDef(load_name, entry["is_tiprack"], ordering, caps)


def pipette(name: str) -> PipetteDef | None:
    entry = _raw()["pipettes"].get(name)
    if entry is None:
        return None
    return PipetteDef(
        name, float(entry["max_volume"]), float(entry["min_volume"]), entry["channels"]
    )
