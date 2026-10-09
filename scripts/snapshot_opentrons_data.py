"""Snapshot labware capacities and pipette limits from an installed `opentrons` (D-007, D-009).

Run with the baseline venv, not the analyser venv:
    .venv-sim/bin/python -I scripts/snapshot_opentrons_data.py \
        src/otverify/data/opentrons_9_0_0.json

Source data: opentrons-shared-data, Apache-2.0 (c) Opentrons Labworks Inc.
"""

import glob
import json
import os
import sys

import opentrons
import opentrons_shared_data
from opentrons import simulate

OT2_PIPETTES = [
    "p10_single",
    "p10_multi",
    "p20_single_gen2",
    "p20_multi_gen2",
    "p50_single",
    "p50_multi",
    "p300_single",
    "p300_multi",
    "p300_single_gen2",
    "p300_multi_gen2",
    "p1000_single",
    "p1000_single_gen2",
]


def labware() -> dict:
    root = os.path.join(
        os.path.dirname(opentrons_shared_data.__file__), "data/labware/definitions/2"
    )
    out = {}
    for d in sorted(glob.glob(os.path.join(root, "*"))):
        latest = max(
            glob.glob(os.path.join(d, "*.json")), key=lambda p: int(os.path.basename(p)[:-5])
        )
        j = json.load(open(latest))
        order = [w for col in j["ordering"] for w in col]
        caps = {w: j["wells"][w]["totalLiquidVolume"] for w in order}
        entry = {
            "version": j["version"],
            "is_tiprack": j["parameters"]["isTiprack"],
            "ordering": j["ordering"],
        }
        if len(set(caps.values())) == 1:
            entry["capacity"] = caps[order[0]]
        else:
            entry["capacities"] = caps
        out[j["parameters"]["loadName"]] = entry
    return out


def pipettes() -> dict:
    out = {}
    for name in OT2_PIPETTES:
        ctx = simulate.get_protocol_api("2.13", robot_type="OT-2")
        p = ctx.load_instrument(name, "left")
        out[name] = {"max_volume": p.max_volume, "min_volume": p.min_volume, "channels": p.channels}
    return out


if __name__ == "__main__":
    data = {
        "_source": f"opentrons {opentrons.__version__} / opentrons-shared-data "
        "(Apache-2.0, Opentrons Labworks Inc.)",
        "pipettes": pipettes(),
        "labware": labware(),
    }
    with open(sys.argv[1], "w") as f:
        json.dump(data, f, indent=None, separators=(",", ":"), sort_keys=True)
        f.write("\n")
