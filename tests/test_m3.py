"""M3: mix / blow_out / air_gap / trash, and legacy get_values + fields.json parameters."""

import json
from pathlib import Path
from textwrap import dedent

import pytest

from otverify.cli import main
from otverify.frontend import lower
from otverify.legacy import label_range, params_from_fields
from otverify.model import Aspirate, Dispense
from otverify.volume import NO_TIP, OVERDRAW, OVERFLOW, TIP_VOLUME, Result, check_volumes

HEADER = """\
metadata = {{"apiLevel": "{api}"}}

def run(protocol):
    tips = protocol.load_labware("opentrons_96_tiprack_300ul", 1)
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 2)
    res = protocol.load_labware("nest_12_reservoir_15ml", 3)
    p = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])
"""


def src(body: str, api: str = "2.13") -> str:
    return HEADER.format(api=api) + "".join(
        "    " + line + "\n" for line in dedent(body).strip().splitlines()
    )


def check(body: str, api: str = "2.13", fields: list[dict] | None = None) -> Result:
    return check_volumes(lower(src(body, api), fields))


def props(result: Result) -> list[tuple[str, int]]:
    return [(f.property, f.line) for f in result.violations]


# ---- mix ----------------------------------------------------------------------------------------


def test_mix_expands_like_opentrons() -> None:
    [prog] = lower(src('p.pick_up_tip()\np.mix(3, 50, plate["A1"])'))
    kinds = [type(s).__name__ for s in prog.steps[1:]]
    assert kinds == ["Aspirate", "Dispense"] * 3
    assert all(s.volume == 50 for s in prog.steps if isinstance(s, Aspirate | Dispense))


def test_mix_zero_repetitions_still_mixes_once() -> None:
    [prog] = lower(src('p.pick_up_tip()\np.mix(0, 50, plate["A1"])'))
    assert len(prog.steps) == 3  # pick_up_tip, aspirate, dispense (the `while` never runs)


def test_mix_with_liquid_in_tip_exceeds_capacity() -> None:
    # Confirmed with opentrons_simulate 9.0.0: "Cannot aspirate more than pipette max volume".
    result = check('p.pick_up_tip()\np.aspirate(200, res["A1"])\np.mix(2, 150, plate["B1"])')
    assert (TIP_VOLUME, 10) in props(result)


def test_mix_in_declared_well_needs_enough_liquid() -> None:
    body = """
        plate["A1"].load_liquid(protocol.define_liquid("x"), 20)
        p.pick_up_tip()
        p.mix(2, 50, plate["A1"])
    """
    assert (OVERDRAW, 10) in props(check(body))


def test_mix_without_location_uses_current_well() -> None:
    [prog] = lower(src('p.pick_up_tip()\np.aspirate(10, plate["C3"])\np.mix(1, 20)'))
    assert {s.wells[0].well for s in prog.steps if isinstance(s, Aspirate)} == {"C3"}


# ---- blow_out, trash, air_gap -------------------------------------------------------------------


@pytest.mark.parametrize("blow_out", ['p.blow_out(plate["A1"])', "p.blow_out()"])
def test_blow_out_into_well_counts_remaining_liquid(blow_out: str) -> None:
    # A1 gets 100 µL + the 200 µL left in the tip = 300 µL; 100 µL more overflows (360 µL).
    # Without the blow-out counting, A1 would hold 200 µL and the last dispense would fit.
    body = f"""
        p.pick_up_tip()
        p.aspirate(300, res["A1"])
        p.dispense(100, plate["A1"])
        {blow_out}
        p.aspirate(100, res["A1"])
        p.dispense(100, plate["A1"])
    """
    [f] = check(body).violations
    assert f.property == OVERFLOW and f.line == 13 and "400 µL" in f.message


@pytest.mark.parametrize(
    "trash",
    ['protocol.fixed_trash["A1"]', "protocol.fixed_trash", "p.trash_container.wells()[0].top()"],
)
def test_trash_takes_anything(trash: str) -> None:
    body = f"""
        p.pick_up_tip()
        for _ in range(5):
            p.aspirate(300, res["A1"])
            p.dispense(300, {trash})
        p.blow_out({trash})
    """
    result = check(body)
    assert result.findings == [] and result.unsupported == []


def test_air_gap_counts_against_capacity() -> None:
    # Confirmed with opentrons_simulate 9.0.0: 280 µL + 30 µL air fails, + 20 µL passes.
    over = check('p.pick_up_tip()\np.aspirate(280, res["A1"])\np.air_gap(30)')
    ok = check('p.pick_up_tip()\np.aspirate(280, res["A1"])\np.air_gap(20)')
    assert [f.property for f in over.violations] == [TIP_VOLUME] and ok.violations == []


def test_air_is_not_liquid_in_the_well() -> None:
    # 340 µL liquid + 20 µL air dispensed into a 360 µL well three times would overflow if air
    # counted; each time only 280 µL liquid lands... so check one well receiving exactly 360.
    body = """
        p.pick_up_tip()
        p.aspirate(180, res["A1"])
        p.air_gap(20)
        p.dispense(200, plate["A1"])
        p.aspirate(180, res["A1"])
        p.air_gap(20)
        p.dispense(200, plate["A1"])
    """
    assert check(body).violations == []  # 2 x 180 µL = 360 µL of liquid; air leaves first


def test_air_gap_without_tip() -> None:
    assert props(check("p.air_gap(10)")) == [(NO_TIP, 8)]


# ---- legacy get_values + fields.json ------------------------------------------------------------

FIELDS = [
    {"type": "int", "label": "Number of samples (1-96)", "name": "n", "default": 8},
    {"type": "float", "label": "Volume (uL, up to 200)", "name": "vol", "default": 50.0},
    {"type": "int", "label": "Mix repetitions", "name": "reps", "default": 3},
    {
        "type": "dropDown",
        "label": "P300 mount",
        "name": "mount",
        "options": [{"label": "Left", "value": "left"}, {"label": "Right", "value": "right"}],
    },
]


def test_params_from_fields() -> None:
    specs = {p.name: p for p in params_from_fields(FIELDS)}
    assert (specs["n"].minimum, specs["n"].maximum, specs["n"].source) == (1, 96, "label")
    assert (specs["vol"].minimum, specs["vol"].maximum, specs["vol"].kind) == (1, 200.0, "float")
    assert specs["reps"].choices == (3,) and specs["reps"].source == "default"
    assert specs["mount"].choices == ("left", "right") and specs["mount"].default == "left"


@pytest.mark.parametrize(
    ("label", "kind", "default", "expected"),
    [
        ("number of samples (1-96)", "int", 96, (1, 96, "label")),
        ("Sample Count (between 1 and 12)", "int", 12, (1, 12, "label")),
        ("Number of Samples (max 288)", "int", 288, (1, 288, "label")),
        ("Elution Volume [50-200µL]", "int", 100, (50, 200, "label")),
        ("Overage Percent (0-10%)", "float", 7.5, (0.0, 10.0, "label")),
        ("wash volume (in ul, up to 500ul)", "float", 600.0, (1, 600.0, "label+default")),
        ("Engage Time for Magnetic Module (minutes)", "int", 10, None),
        ("Volume of 96-well plate", "int", 5, None),
        ("Minimum Well Bottom Clearance (mm)", "float", 1.0, None),
    ],
)
def test_label_range(label: str, kind: str, default: float, expected: object) -> None:
    assert label_range(label, kind, default) == expected


def test_get_values_with_enumerated_sample_count() -> None:
    body = """
        [n, vol, reps, mount] = get_values("n", "vol", "reps", "mount")
        p.pick_up_tip()
        for i in range(n):
            p.aspirate(vol, res["A1"])
            p.dispense(vol, plate["B1"])
            p.mix(reps, vol)
    """
    result = check(body, fields=FIELDS)
    assert result.unsupported == []
    # n is enumerated (1..96), vol stays symbolic in [1, 200]: B1 overflows when n * vol > 360.
    at_n2 = [f for f in result.violations if dict(f.witness).get("n") == 2]
    assert at_n2 and all(str(i) == "(180, 200]" for f in at_n2 for n, i in f.ranges)


def test_get_values_without_fields_is_unsupported() -> None:
    [stop] = check('n = get_values("n")[0]').unsupported
    assert "fields.json" in stop.reason


def test_get_values_unknown_field() -> None:
    [stop] = check('x = get_values("nope")[0]', fields=FIELDS).unsupported
    assert "nope" in stop.reason


def test_cli_reads_fields_json_next_to_protocol(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "fields.json").write_text(json.dumps(FIELDS))
    proto = tmp_path / "p.ot2.apiv2.py"
    proto.write_text(
        src(
            """
            [n, vol] = get_values("n", "vol")
            p.pick_up_tip()
            for i in range(n):
                p.aspirate(vol, res["A1"])
                p.dispense(vol, plate["B1"])
            """
        )
    )
    assert main([str(proto)]) == 1
    out = capsys.readouterr().out
    assert "n ∈ [1, 96] (enumerated) [from label]" in out
    assert "reps = 3 (DEFAULT ONLY: no range known)" in out


def test_aspirating_more_than_a_well_can_hold_is_a_certain_overdraw() -> None:
    # 05f673 with the 96-well option: mix(3, 1000) in a 360 µL well. Whatever the well held,
    # it cannot supply 1000 µL (D-023), and the tip then holds at most 360 µL: no cascade.
    p1000 = """
        p2 = ctx.load_instrument("p1000_single_gen2", "right",
                                 tip_racks=[ctx.load_labware("opentrons_96_tiprack_1000ul", 4)])
        p2.pick_up_tip()
        p2.mix(3, 1000, plate["A1"])
    """
    found = check(p1000.replace("ctx.", "protocol.")).violations
    assert len(found) == 3  # one per mix repetition, grouped by the CLI
    assert all(f.property == OVERDRAW and "can hold at most 360 µL" in f.message for f in found)


def test_unknown_well_upper_bound_tracks_removals() -> None:
    body = """
        p.pick_up_tip()
        p.aspirate(300, plate["A1"])
        p.dispense(300, res["A1"])
        p.aspirate(100, plate["A1"])
    """
    [f] = check(body).violations
    assert f.property == OVERDRAW and f.line == 11 and "can hold at most 60 µL" in f.message


def test_over_aspiration_sent_to_trash_is_a_warning() -> None:
    # Removal of 2 x 200 µL from a 360 µL well (whatever it held) straight to the trash.
    body = """
        p.pick_up_tip()
        p.aspirate(200, plate["A1"])
        p.dispense(200, protocol.fixed_trash["A1"])
        p.aspirate(200, plate["A1"])
        p.dispense(200, protocol.fixed_trash["A1"])
        p.drop_tip()
    """
    result = check(body)
    assert result.violations == []
    assert {f.property for f in result.findings} == {OVERDRAW}
    assert all("intended over-aspiration?" in f.message for f in result.findings)


def test_over_aspiration_delivered_to_a_well_stays_a_violation() -> None:
    body = """
        p.pick_up_tip()
        p.aspirate(300, plate["A1"])
        p.dispense(100, plate["A2"])
        p.aspirate(100, plate["A1"])
        p.dispense(100, plate["A3"])
    """
    [f] = check(body).violations
    assert f.property == OVERDRAW and f.line == 11


def test_shortfall_enters_the_tip_as_air() -> None:
    # 50 µL declared, 100 µL aspirated: 50 µL liquid + 50 µL air. Dispensing 100 µL is then
    # fine (no P1c), and the destination receives only 50 µL of liquid.
    body = """
        plate["A1"].load_liquid(protocol.define_liquid("x"), 50)
        p.pick_up_tip()
        p.aspirate(100, plate["A1"])
        p.dispense(100, plate["B1"])
        plate["B1"].load_liquid(protocol.define_liquid("y"), 0)
    """
    result = check(body)
    assert [f.property for f in result.findings] == [OVERDRAW]
