"""M3 front-end coverage: the Python subset and API surface real library protocols use."""

from textwrap import dedent

import pytest

from otverify.frontend import lower
from otverify.model import Aspirate, Dispense
from otverify.volume import OVERFLOW, Result, check_volumes

HEADER = """\
import math
import csv
from opentrons import types

metadata = {"apiLevel": "2.13"}

def run(ctx):
    tips = ctx.load_labware("opentrons_96_tiprack_300ul", 1)
    plate = ctx.load_labware("corning_96_wellplate_360ul_flat", 2)
    res = ctx.load_labware("nest_12_reservoir_15ml", 3)
    p = ctx.load_instrument("p300_single_gen2", "left", tip_racks=[tips])
"""


def lower_body(body: str, fields: list | None = None, labware: list | None = None) -> list:
    src = HEADER + "".join("    " + line + "\n" for line in dedent(body).strip().splitlines())
    return lower(src, fields, labware)


def check(body: str, fields: list | None = None, labware: list | None = None) -> Result:
    return check_volumes(lower_body(body, fields, labware))


def dispensed(body: str) -> list[tuple[str, float]]:
    [prog] = lower_body(body)
    assert prog.unsupported is None, prog.unsupported
    return [(s.wells[0].well, s.volume) for s in prog.steps if isinstance(s, Dispense)]


def test_helper_function_with_defaults_and_return() -> None:
    body = """
        def fill(dest, vol=50):
            p.aspirate(vol, res["A1"])
            p.dispense(vol, dest)
            return vol
        p.pick_up_tip()
        total = fill(plate["A1"]) + fill(plate["A2"], vol=100)
        fill(plate["A3"], total)
    """
    assert dispensed(body) == [("A1", 50.0), ("A2", 100.0), ("A3", 150.0)]


def test_helper_context_names_the_function() -> None:
    body = """
        def overfill(dest):
            for _ in range(2):
                p.aspirate(200, res["A1"])
                p.dispense(200, dest)
        p.pick_up_tip()
        overfill(plate["B1"])
    """
    [f] = check(body).violations
    assert f.property == OVERFLOW and f.context == (("in", "overfill()"), ("_", 1))


def test_math_csv_fstrings_and_str_methods() -> None:
    body = """
        plate_map = "well,vol\\nA1,20\\nB2,35.5\\n"
        rows = [r for r in csv.DictReader(plate_map.splitlines())]
        p.pick_up_tip()
        for r in rows:
            p.aspirate(float(r["vol"]), res["A1"])
            p.dispense(float(r["vol"]), plate[f"{r['well'][0]}{int(r['well'][1:])}"])
        n_cols = math.ceil(len(rows) / 8)
        p.aspirate(10 * n_cols, res["A1"])
        p.dispense(10 * n_cols, plate.columns()[n_cols - 1][0])
    """
    assert dispensed(body) == [("A1", 20.0), ("B2", 35.5), ("A1", 10.0)]


def test_while_break_continue() -> None:
    body = """
        i = 0
        p.pick_up_tip()
        while True:
            i += 1
            if i % 2 == 0:
                continue
            if i > 5:
                break
            p.aspirate(i, res["A1"])
            p.dispense(i, plate["A1"])
    """
    assert [v for _, v in dispensed(body)] == [1.0, 3.0, 5.0]


def test_settings_assignments_and_modules_are_ignored() -> None:
    body = """
        ctx.max_speeds["X"] = 100
        p.flow_rate.aspirate = 50
        p.well_bottom_clearance.dispense = 1
        mag = ctx.load_module("magnetic module gen2", 4)
        mag_plate = mag.load_labware("nest_96_wellplate_2ml_deep")
        mag.engage(height_from_base=5)
        temp = ctx.load_module("temperature module gen2", 7)
        temp.set_temperature(4)
        if not ctx.is_simulating():
            ctx.delay(minutes=5)
        p.pick_up_tip()
        p.aspirate(100, res["A1"])
        p.dispense(100, mag_plate["A1"].top().move(types.Point(x=1, y=0, z=-2)))
        mag.disengage()
    """
    assert dispensed(body) == [("A1", 100.0)]


def test_custom_labware_definition() -> None:
    definition = {
        "ordering": [["A1"], ["A2"]],
        "wells": {"A1": {"totalLiquidVolume": 100}, "A2": {"totalLiquidVolume": 100}},
        "parameters": {"loadName": "my_tube_rack", "isTiprack": False},
    }
    body = """
        rack = ctx.load_labware("my_tube_rack", 5)
        p.pick_up_tip()
        p.aspirate(150, res["A1"])
        p.dispense(150, rack["A2"])
    """
    [f] = check(body, labware=[definition]).violations
    assert "A2 of my_tube_rack" in f.message and "capacity 100 µL" in f.message


def test_try_body_runs_and_handlers_are_ignored() -> None:
    body = """
        try:
            p.pick_up_tip()
        except Exception:
            ctx.pause("refill tips")
            p.pick_up_tip()
        p.aspirate(10, res["A1"])
    """
    [prog] = lower_body(body)
    assert prog.unsupported is None
    assert [type(s).__name__ for s in prog.steps] == ["PickUpTip", "Aspirate"]


def test_raise_ends_the_run_without_a_gap() -> None:
    body = """
        p.pick_up_tip()
        p.aspirate(10, res["A1"])
        raise Exception("stop here")
        p.aspirate(10, res["A1"])
    """
    [prog] = lower_body(body)
    assert prog.unsupported is None and len(prog.steps) == 2


# ---- parameter-dependent branches ---------------------------------------------------------

FIELDS = [
    {"type": "float", "label": "Volume (uL, 10-300)", "name": "vol", "default": 100.0},
]


def test_branch_decided_over_the_domain() -> None:
    # vol is in [10, 300], so `vol > 500` is never true: no enumeration, nothing unsupported.
    body = """
        [vol] = get_values("vol")
        p.pick_up_tip()
        if vol > 500:
            p.aspirate(1000, res["A1"])
        p.aspirate(vol, res["A1"])
    """
    result = check(body, fields=FIELDS)
    assert result.unsupported == [] and result.violations == []


def test_validation_raise_becomes_a_path_condition() -> None:
    # The protocol rejects vol > 150 itself, so the overflow of 3 x vol only happens for
    # vol in (120, 150], not up to 300.
    body = """
        [vol] = get_values("vol")
        if vol > 150:
            raise Exception("volume too high")
        p.pick_up_tip()
        for _ in range(3):
            p.aspirate(vol, res["A1"])
            p.dispense(vol, plate["A1"])
    """
    result = check(body, fields=FIELDS)
    assert result.unsupported == []
    ranges = {str(i) for f in result.violations for _, i in f.ranges}
    assert ranges == {"(120, 150]"}


def test_undecided_float_branch_is_unsupported() -> None:
    body = """
        [vol] = get_values("vol")
        if vol > 150:
            p.pick_up_tip()
    """
    [stop] = check(body, fields=FIELDS).unsupported
    assert "branch condition `vol > 150` depends on parameter(s) ['vol']" in stop.reason


def test_aspirate_steps_record_helper_context() -> None:
    [prog] = lower_body("def f():\n    p.pick_up_tip()\n    p.aspirate(5, res['A1'])\nf()")
    [asp] = [s for s in prog.steps if isinstance(s, Aspirate)]
    assert asp.context == (("in", "f()"),)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("x = lambda v: v", "lambda"),
        ("def f(*a):\n    pass\nf(1)", "*args"),
        ("def f():\n    f()\nf()", "call depth"),
        ("x = p.current_volume", "pipette attribute `current_volume`"),
        ("import os\nos.listdir('.')", "call to `os.listdir`"),
    ],
)
def test_still_unsupported(body: str, reason: str) -> None:
    [prog] = lower_body(body)
    assert prog.unsupported is not None and reason in prog.unsupported.reason


def test_user_class_and_threads() -> None:
    # The CancellationToken pattern from the library: blinking lights during a pause.
    body = """
        import threading
        class Token:
            def __init__(self):
                self.go = False
            def set_true(self):
                self.go = True
        token = Token()
        token.set_true()
        t = threading.Thread(target=None, args=(ctx, token))
        t.start()
        p.pick_up_tip()
        if token.go:
            p.aspirate(10, res["A1"])
            p.dispense(10, plate["A1"])
    """
    assert dispensed(body) == [("A1", 10.0)]


def test_protocol_bookkeeping_attributes() -> None:
    body = """
        for w in plate.wells()[:2]:
            w.liq_vol = 0
        setattr(res["A1"], "liq_vol", 1000)
        p.pick_up_tip()
        for w in plate.wells()[:2]:
            p.aspirate(40, res["A1"])
            res["A1"].liq_vol -= 40
            p.dispense(40, w)
            w.liq_vol += 40
        p.aspirate(getattr(res["A1"], "liq_vol") / 92, res["A1"])
        p.dispense(plate["A1"].liq_vol, plate.well(1))
    """
    assert dispensed(body) == [("A1", 40.0), ("B1", 40.0), ("B1", 40.0)]


def test_pipette_and_context_state() -> None:
    body = """
        if not p.has_tip:
            p.pick_up_tip()
        assert p.has_tip
        lw = ctx.loaded_labwares[2]
        pip = ctx.loaded_instruments["left"]
        pip.aspirate(p.max_volume / 3, res["A1"])
        pip.dispense(100, lw["A2"].top(-2))
        p.drop_tip()
        del lw
    """
    assert dispensed(body) == [("A2", 100.0)]


def test_tip_state_files_follow_the_simulation_branch() -> None:
    body = """
        import os, json
        path = os.path.join(os.path.dirname(__file__), "tip_log.json")
        if ctx.is_simulating():
            starting = 0
        else:
            with open(path) as f:
                starting = json.load(f)["count"]
        if not os.path.isfile(path):
            os.makedirs(os.path.dirname(path))
        p.pick_up_tip()
        p.aspirate(20 + starting, res["A1"])
        p.dispense(20 + starting, plate["A1"])
    """
    assert dispensed(body) == [("A1", 20.0)]


def test_protocol_raise_inside_try_runs_the_handler() -> None:
    body = """
        p.pick_up_tip()
        try:
            raise ValueError("bad input")
        except ValueError:
            p.aspirate(15, res["A1"])
            p.dispense(15, plate["A1"])
    """
    assert dispensed(body) == [("A1", 15.0)]
