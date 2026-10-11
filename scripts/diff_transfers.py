"""Differential test of otverify.transfers against opentrons' own TransferPlan (D-020).

Run in the baseline venv, which has opentrons==9.0.0 (plus z3-solver for importing otverify):

    .venv-sim/bin/python scripts/diff_transfers.py 2000 [seed]

Each scenario draws a pipette, tips, API level, wells and transfer keyword arguments at random.
It runs the real `InstrumentContext.transfer/distribute/consolidate` in the simulator, recording
the top-level commands the plan executes, and compares them with `otverify.transfers.plan`. Both
sides must raise, or both must produce the same command sequence.
"""

import logging
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
logging.disable(logging.CRITICAL)

from opentrons import simulate  # noqa: E402
from opentrons.protocol_api.labware import Well  # noqa: E402
from opentrons.types import Location  # noqa: E402

from otverify import transfers  # noqa: E402

PIPETTES = [
    ("p300_single_gen2", ["opentrons_96_tiprack_300ul", "opentrons_96_filtertiprack_200ul"]),
    ("p20_single_gen2", ["opentrons_96_tiprack_20ul"]),
    ("p1000_single_gen2", ["opentrons_96_tiprack_1000ul"]),
    ("p300_multi_gen2", ["opentrons_96_tiprack_300ul"]),
    ("p20_multi_gen2", ["opentrons_96_tiprack_20ul"]),
]
APIS = ["2.0", "2.2", "2.7", "2.8", "2.9", "2.11", "2.13", "2.15", "2.16", "2.17"]
RECORDED = ["pick_up_tip", "drop_tip", "return_tip", "aspirate", "dispense", "mix", "air_gap",
            "blow_out", "touch_tip"]  # fmt: skip


def loc_key(loc: object) -> object:
    if type(loc).__name__ in ("TrashBin", "WasteChute"):
        return "trash"
    if isinstance(loc, Location):
        loc = loc.labware.as_well() if loc.labware.is_well else loc.labware.object
    if isinstance(loc, Well):
        parent = loc.parent
        if "trash" in parent.load_name:
            return "trash"
        return (parent.load_name, loc.well_name)
    if loc is None:
        return None
    return ("?", str(loc))


def record(instr, log: list) -> None:
    depth = [0]
    for name in RECORDED:
        original = getattr(instr, name)

        def wrapper(*args, _name=name, _original=original, **kwargs):
            if depth[0] == 0:
                log.append(normalise_real(_name, args, kwargs))
            depth[0] += 1
            try:
                return _original(*args, **kwargs)
            finally:
                depth[0] -= 1

        setattr(instr, name, wrapper)


def vol(v: object) -> object:
    return None if v is None else round(float(v), 6)


def normalise_real(name: str, args: tuple, kwargs: dict) -> tuple:
    if name in ("aspirate", "dispense"):
        return (name, vol(args[0]), loc_key(args[1]))
    if name == "mix":
        return (name, kwargs.get("repetitions", 1), vol(kwargs.get("volume")),
                loc_key(kwargs.get("location")))  # fmt: skip
    if name == "air_gap":
        return (name, vol(args[0]))
    if name == "blow_out":
        return (name, loc_key(args[0] if args else kwargs.get("location")))
    return (name,)


def normalise_ours(cmd: tuple) -> tuple:
    name, *args = cmd
    if name in ("aspirate", "dispense"):
        return (name, vol(args[0]), args[1])
    if name == "mix":
        return (name, args[0], vol(args[1]), args[2])
    if name == "air_gap":
        return (name, vol(args[0]))
    if name == "blow_out":
        return (name, args[0])
    return (name,)


def scenario(rng: random.Random) -> dict:
    pip, racks = rng.choice(PIPETTES)
    s = {
        "api": rng.choice(APIS),
        "pipette": pip,
        "tips": rng.choice(racks),
        "mode": rng.choice(["transfer", "transfer", "distribute", "consolidate"]),
        "kwargs": {},
    }
    big = {"p20": 20, "p30": 300, "p10": 1000}[pip[:3]]
    s["volume"] = rng.choice(
        [0, 1, 5, 10, 15, 20, 50, 100, 150, 199, 200, 250, 300, 450, 700, 1500]
    )
    s["volume"] = min(s["volume"], big * 3) if rng.random() < 0.8 else s["volume"]
    if rng.random() < 0.15:
        s["volume_list"] = True
    k = s["kwargs"]
    if rng.random() < 0.5:
        k["new_tip"] = rng.choice(["once", "always", "never"])
    if rng.random() < 0.3:
        k["mix_before"] = (rng.choice([0, 1, 2, 3]), rng.choice([0, 5, 10, 50]))
    if rng.random() < 0.3:
        k["mix_after"] = (rng.choice([0, 1, 2, 3]), rng.choice([0, 5, 10, 50]))
    if rng.random() < 0.3:
        k["air_gap"] = rng.choice([0, 2, 5, 10, 20])
    if rng.random() < 0.3:
        k["disposal_volume"] = rng.choice([0, 1, 5, 10, 20])
    if rng.random() < 0.3:
        k["blow_out"] = True
        if rng.random() < 0.5:
            k["blowout_location"] = rng.choice(["trash", "source well", "destination well"])
    if rng.random() < 0.2:
        k["touch_tip"] = True
    if rng.random() < 0.2:
        k["trash"] = False
    s["n_src"] = rng.choice([1, 1, 2, 3, 4, 8])
    s["n_dst"] = rng.choice([1, 2, 3, 4, 6, 8, 12])
    s["shape"] = rng.choice(["wells", "columns"])
    s["to_trash"] = s["mode"] == "transfer" and rng.random() < 0.15
    return s


def run_real(s: dict) -> tuple[str, object]:
    ctx = simulate.get_protocol_api(s["api"], robot_type="OT-2")
    racks = [ctx.load_labware(s["tips"], slot) for slot in (1, 4, 5, 6, 7, 8, 9, 10, 11)]
    plate = ctx.load_labware("corning_96_wellplate_360ul_flat", 2)
    res = ctx.load_labware("nest_12_reservoir_15ml", 3)
    instr = ctx.load_instrument(s["pipette"], "left", tip_racks=racks)
    src, dst = pick_wells(s, plate, res, ctx)
    if s["kwargs"].get("new_tip") == "never":
        instr.pick_up_tip()
    log: list = []
    record(instr, log)
    volume = (
        [s["volume"] + i for i in range(transfers_count(s, src, dst))]
        if s.get("volume_list")
        else s["volume"]
    )
    try:
        getattr(instr, s["mode"])(volume, src, dst, **s["kwargs"])
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"[:200]
        # An error while executing the plan (log non-empty) is a runtime failure of a command,
        # e.g. an over-capacity aspirate the plan itself generated. The checker reports those.
        return ("runtime-error", message, log) if log else ("error", message)
    return "ok", log


def transfers_count(s: dict, src: object, dst: object) -> int:
    def n(x: object) -> int:
        if isinstance(x, list):
            return sum(len(i) for i in x) if x and isinstance(x[0], list) else len(x)
        return 1

    return max(n(src), n(dst))


def pick_wells(s: dict, plate, res, ctx) -> tuple:
    if s["shape"] == "columns":
        src = (
            res.wells()[: s["n_src"]]
            if s["mode"] != "consolidate"
            else plate.columns()[: s["n_src"]]
        )
        dst = plate.columns()[: s["n_dst"]]
    else:
        src = (
            res.wells()[: s["n_src"]] if s["mode"] != "consolidate" else plate.wells()[: s["n_src"]]
        )
        dst = plate.wells()[: s["n_dst"]]
    if s["mode"] == "distribute":
        src = src[0] if not isinstance(src[0], list) else src[0][0]
    if s["mode"] == "consolidate":
        dst = res.wells()[0]
    if s.get("to_trash"):
        trash = ctx.fixed_trash
        dst = trash if type(trash).__name__ == "TrashBin" else trash["A1"]
    if len(src) == 1 if isinstance(src, list) else False:
        src = src[0]
    return src, dst


def run_ours(s: dict) -> tuple[str, object]:
    ctx = simulate.get_protocol_api(s["api"], robot_type="OT-2")
    racks = [ctx.load_labware(s["tips"], slot) for slot in (1, 4, 5, 6, 7, 8, 9, 10, 11)]
    plate = ctx.load_labware("corning_96_wellplate_360ul_flat", 2)
    res = ctx.load_labware("nest_12_reservoir_15ml", 3)
    instr = ctx.load_instrument(s["pipette"], "left", tip_racks=racks)
    src, dst = pick_wells(s, plate, res, ctx)
    api = tuple(int(x) for x in s["api"].split("."))

    def conv(x: object) -> object:
        if isinstance(x, list):
            return [conv(i) for i in x]
        return loc_key(x)

    rows = {}
    for lw in (plate, res):
        layout = [[w.well_name for w in row] for row in lw.rows()]
        rows[lw.load_name] = layout

    def valid_row(key: tuple) -> bool:
        layout = rows[key[0]]
        first = layout[:2] if api >= (2, 2) and len(layout) == 16 else layout[:1]
        return any(key[1] in r for r in first)

    volume = (
        [s["volume"] + i for i in range(transfers_count(s, src, dst))]
        if s.get("volume_list")
        else s["volume"]
    )
    tip_max = min(instr.max_volume, racks[0].wells()[0].max_volume)
    try:
        opts = transfers.options_from_kwargs(s["mode"], s["kwargs"], api, instr.min_volume, bool)
        cmds = transfers.plan(
            volume, conv(src), conv(dst), opts,
            pipette_max=instr.max_volume, tip_max=tip_max, channels=instr.channels, api=api,
            valid_row=valid_row, decide=bool, trash="trash",
        )  # fmt: skip
    except transfers.PlanError as exc:
        return (
            ("runtime-error", str(exc), [normalise_ours(c) for c in exc.commands])
            if (exc.commands)
            else ("error", str(exc))
        )
    except transfers.NotPorted as exc:
        return "not-ported", str(exc)
    return "ok", [normalise_ours(c) for c in cmds]


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    rng = random.Random(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
    agree = errors_both = runtime = 0
    mismatches = []
    for i in range(n):
        s = scenario(rng)
        real, ours = run_real(s), run_ours(s)
        if real[0] == ours[0] == "error" or (
            real[0] == ours[0] == "runtime-error" and real[2] == ours[2]
        ):
            errors_both += 1
        elif real == ours:
            agree += 1
        elif real[0] == "runtime-error" and ours[0] == "ok" and ours[1][: len(real[2])] == real[2]:
            runtime += 1  # same plan up to the command that fails when executed
        else:
            mismatches.append((i, s, real, ours))
    print(f"{n} scenarios: {agree} identical plans, {errors_both} both raise at the same point, "
          f"{runtime} same plan up to a failing command, {len(mismatches)} mismatches")  # fmt: skip
    for i, s, real, ours in mismatches[:8]:
        print(f"--- #{i} {s}")
        print(f"  real: {str(real)[:600]}")
        print(f"  ours: {str(ours)[:600]}")


if __name__ == "__main__":
    main()
