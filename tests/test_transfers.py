"""transfer / distribute / consolidate via the port of opentrons' TransferPlan (D-020).

The port itself is checked against the real TransferPlan by scripts/diff_transfers.py (baseline
venv). These tests cover the integration: steps, findings, parameters and warnings.
"""

from textwrap import dedent

import pytest

from otverify.frontend import lower
from otverify.model import Aspirate, Dispense
from otverify.volume import OVERFLOW, Result, check_volumes

HEADER = """\
metadata = {{"apiLevel": "{api}"}}

def run(ctx):
    tips = ctx.load_labware("{tips}", 1)
    plate = ctx.load_labware("corning_96_wellplate_360ul_flat", 2)
    res = ctx.load_labware("nest_12_reservoir_15ml", 3)
    p = ctx.load_instrument("{pipette}", "left", tip_racks=[tips])
"""


def lower_body(body: str, api: str = "2.13", tips: str = "opentrons_96_tiprack_300ul",
               pipette: str = "p300_single_gen2", fields: list | None = None) -> list:  # fmt: skip
    src = HEADER.format(api=api, tips=tips, pipette=pipette)
    src += "".join("    " + line + "\n" for line in dedent(body).strip().splitlines())
    return lower(src, fields)


def check(body: str, **kw: object) -> Result:
    return check_volumes(lower_body(body, **kw))  # type: ignore[arg-type]


def steps(body: str, **kw: object) -> list[tuple[str, str, float]]:
    [prog] = lower_body(body, **kw)  # type: ignore[arg-type]
    assert prog.unsupported is None, prog.unsupported
    return [
        (type(s).__name__, s.wells[0].well if s.wells else "trash", s.volume)
        for s in prog.steps
        if isinstance(s, Aspirate | Dispense)
    ]


def test_transfer_splits_large_volumes() -> None:
    # 450 µL with a 300 µL tip: split into two halves of 225 µL (expand_for_volume_constraints).
    assert steps('p.transfer(450, res["A1"], plate["A1"])') == [
        ("Aspirate", "A1", 225.0), ("Dispense", "A1", 225.0),
        ("Aspirate", "A1", 225.0), ("Dispense", "A1", 225.0),
    ]  # fmt: skip


def test_transfer_overflow_found() -> None:
    [f] = check('p.transfer(450, res["A1"], plate["A1"])').violations
    assert f.property == OVERFLOW and "450 µL" in f.message


def test_distribute_groups_dispenses_with_disposal_volume() -> None:
    # p300 min volume 20 µL is the default disposal volume; it is blown out to the trash.
    got = steps('p.distribute(50, res["A1"], plate.wells()[:3])')
    assert got == [
        ("Aspirate", "A1", 170.0),
        ("Dispense", "A1", 50.0), ("Dispense", "B1", 50.0), ("Dispense", "C1", 50.0),
    ]  # fmt: skip


def test_consolidate() -> None:
    got = steps('p.consolidate(100, plate.wells()[:3], res["A1"])')
    assert got == [
        ("Aspirate", "A1", 100.0), ("Aspirate", "B1", 100.0), ("Aspirate", "C1", 100.0),
        ("Dispense", "A1", 300.0),
    ]  # fmt: skip


def test_mix_after_and_air_gap() -> None:
    got = steps('p.transfer(50, res["A1"], plate["A1"], mix_after=(2, 30), air_gap=10)')
    assert got == [
        ("Aspirate", "A1", 50.0), ("Dispense", "A1", 60.0),
        ("Aspirate", "A1", 30.0), ("Dispense", "A1", 30.0),
        ("Aspirate", "A1", 30.0), ("Dispense", "A1", 30.0),
    ]  # fmt: skip


def test_silent_noop_distribute_warning() -> None:
    # experiments/2026-10-10-silent-noop-distribute: confirmed with opentrons_simulate 9.0.0.
    result = check(
        'p.distribute(250, res["A1"], plate.wells()[:4])', tips="opentrons_96_filtertiprack_200ul"
    )
    [f] = result.findings
    assert f.severity == "warning" and "moves no liquid" in f.message


def test_ignored_keyword_warning() -> None:
    result = check('p.distribute(20, res["A1"], plate.wells()[:4], disposal_vol=0)')
    assert any("`disposal_vol=` is not a distribute() option" in f.message for f in result.findings)


def test_crash_is_a_violation_and_ends_the_run() -> None:
    # 3 sources onto 2 destinations: "Source and destination lists must be divisible".
    body = """
        p.transfer(50, plate.wells()[:3], plate.wells()[3:5])
        p.pick_up_tip()
    """
    result = check(body)
    [f] = result.violations
    assert f.property == "CRASH" and "divisible" in f.message
    [prog] = lower_body(body)
    assert [type(s).__name__ for s in prog.steps] == ["PickUpTip"]  # the lazy plan's first step


def test_multichannel_skips_wells_outside_first_row() -> None:
    result = check('p.transfer(20, res["A1"], plate.wells()[:16])', pipette="p300_multi_gen2")
    assert any("silently skipped by a 8-channel transfer()" in f.message for f in result.findings)


FIELDS = [
    {"type": "float", "label": "Volume (uL, 10-250)", "name": "vol", "default": 50.0},
    {"type": "int", "label": "Samples (1-24)", "name": "n", "default": 8},
]


def test_symbolic_volume_without_splitting_stays_symbolic() -> None:
    # vol <= 250 <= the 300 µL tip, so no split: one symbolic aspirate per transfer.
    body = """
        [vol, n] = get_values("vol", "n")
        p.transfer(vol, res["A1"], plate["A1"], new_tip="once")
        p.transfer(vol, res["A1"], plate["A1"], new_tip="once")
    """
    result = check(body, fields=FIELDS)
    assert result.unsupported == []
    [f] = result.violations
    assert f.property == OVERFLOW and {str(i) for _, i in f.ranges} == {"(180, 250]"}


def test_symbolic_volume_that_may_need_splitting_is_unsupported() -> None:
    fields = [{"type": "float", "label": "Volume (uL, 10-500)", "name": "vol", "default": 50.0}]
    body = """
        [vol] = get_values("vol")
        p.transfer(vol, res["A1"], plate["A1"])
    """
    [stop] = check(body, fields=fields).unsupported
    assert "transfer() planning" in stop.reason


def test_enumerated_sample_count_in_transfer() -> None:
    body = """
        [vol, n] = get_values("vol", "n")
        p.transfer(30, res["A1"], plate.wells()[:n], new_tip="always")
    """
    programs = lower_body(body, fields=FIELDS)
    assert len(programs) == 24 and programs[0].enumerated == ("n",)
    assert check_volumes(programs).unsupported == []


@pytest.mark.parametrize(
    ("call", "reason"),
    [
        ('p.transfer(10, res["A1"], plate["A1"], gradient_function=lambda x: x)', "lambda"),
        ('p.transfer(10, (res["A1"], res["A2"]), plate["A1"])', "tuple of wells"),
    ],
)
def test_not_ported(call: str, reason: str) -> None:
    [prog] = lower_body(call)
    assert prog.unsupported is not None and reason in prog.unsupported.reason
