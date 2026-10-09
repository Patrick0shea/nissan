from pathlib import Path
from textwrap import dedent

import pytest

from otverify.cli import main
from otverify.frontend import lower
from otverify.volume import NO_TIP, OVERDRAW, OVERFLOW, TIP_VOLUME, Result, check_volumes

FIXTURES = Path(__file__).parent / "fixtures"

HEADER = """\
metadata = {{"apiLevel": "{api}"}}

def run(protocol):
    tips = protocol.load_labware("{tips}", 1)
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 2)
    res = protocol.load_labware("nest_12_reservoir_15ml", 3)
    p = protocol.load_instrument("{pipette}", "left", tip_racks=[tips])
"""


def check(
    body: str,
    api: str = "2.13",
    tips: str = "opentrons_96_tiprack_300ul",
    pipette: str = "p300_single_gen2",
) -> Result:
    src = HEADER.format(api=api, tips=tips, pipette=pipette)
    src += "".join("    " + line + "\n" for line in dedent(body).strip().splitlines())
    return check_volumes(lower(src))


def props(result: Result) -> list[tuple[str, int]]:
    return [(f.property, f.line) for f in result.violations]


# ---- fixtures and CLI ---------------------------------------------------------------------------


def test_toy_overfill_reports_third_iteration() -> None:
    result = check_volumes(lower((FIXTURES / "toy_overfill.py").read_text()))
    assert props(result) == [(OVERFLOW, 20)]
    finding = result.violations[0]
    assert finding.context == (("_", 2),)
    assert "450 µL" in finding.message and "360 µL" in finding.message
    assert result.unsupported is None
    assert not result.unchecked_aspirations  # the reservoir is declared with load_liquid


def test_toy_clean_has_no_findings() -> None:
    result = check_volumes(lower((FIXTURES / "toy_clean.py").read_text()))
    assert result.findings == [] and result.unsupported is None


@pytest.mark.parametrize(("fixture", "code"), [("toy_overfill.py", 1), ("toy_clean.py", 0)])
def test_cli_exit_codes(fixture: str, code: int, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(FIXTURES / fixture)]) == code
    out = capsys.readouterr().out
    assert ("line 20: violation P1a" in out) == (code == 1)


def test_cli_reports_incomplete_analysis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "p.py"
    f.write_text(
        HEADER.format(api="2.13", tips="opentrons_96_tiprack_300ul", pipette="p300_single_gen2")
        + "    p.transfer(100, res['A1'], plate['A1'])\n"
    )
    assert main([str(f)]) == 3
    assert "INCOMPLETE: analysis stopped at line 8" in capsys.readouterr().out


def test_cli_parse_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.py"
    f.write_text("def run(:\n")
    assert main([str(f)]) == 2


# ---- P1a overflow -------------------------------------------------------------------------------


def test_overflow_into_unknown_well_is_certain() -> None:
    result = check(
        """
        p.pick_up_tip()
        p.aspirate(200, res["A1"])
        p.dispense(200, plate["A1"])
        p.aspirate(200, res["A1"])
        p.dispense(200, plate["A1"])
        """
    )
    assert props(result) == [(OVERFLOW, 12)]
    assert "at least 400 µL" in result.violations[0].message


def test_load_liquid_over_capacity() -> None:
    result = check('plate["A1"].load_liquid(protocol.define_liquid("x"), 400)')
    assert props(result) == [(OVERFLOW, 8)]


def test_multichannel_column_fills_eight_wells() -> None:
    result = check(
        """
        p.pick_up_tip()
        for _ in range(2):
            p.aspirate(200, res["A1"])
            p.dispense(200, plate["A1"])
        """,
        pipette="p300_multi_gen2",
    )
    overflowed = {f.message.split(" ")[0] for f in result.violations}
    assert overflowed == {"A1", "B1", "C1", "D1", "E1", "F1", "G1", "H1"}


def test_multichannel_in_reservoir_draws_eight_times() -> None:
    result = check(
        """
        res["A1"].load_liquid(protocol.define_liquid("x"), 1000)
        p.pick_up_tip()
        p.aspirate(150, res["A1"])
        """,
        pipette="p300_multi_gen2",
    )
    # 8 channels x 150 µL = 1200 µL from a well holding 1000 µL: the 7th channel overdraws.
    assert {f.property for f in result.violations} == {OVERDRAW}


# ---- P1b overdraw -------------------------------------------------------------------------------


def test_overdraw_from_declared_well() -> None:
    result = check(
        """
        plate["A1"].load_liquid(protocol.define_liquid("x"), 50)
        p.pick_up_tip()
        p.aspirate(100, plate["A1"])
        """
    )
    assert props(result) == [(OVERDRAW, 10)]


def test_unknown_source_is_not_checked_but_counted() -> None:
    result = check(
        """
        p.pick_up_tip()
        for _ in range(3):
            p.aspirate(100, plate["A1"])
            p.dispense(100, plate["B1"])
        """
    )
    assert result.violations == []
    assert result.unchecked_aspirations == {"A1 of corning_96_wellplate_360ul_flat (slot 2)": 3}


# ---- P1c tip volume -----------------------------------------------------------------------------


def test_tip_capacity_is_min_of_pipette_and_tip() -> None:
    # Confirmed against opentrons 9.0.0: a p300 with 200 µL filter tips cannot aspirate 250 µL.
    result = check(
        'p.pick_up_tip()\np.aspirate(250, res["A1"])',
        tips="opentrons_96_filtertiprack_200ul",
    )
    assert props(result) == [(TIP_VOLUME, 9)]
    assert "of 200 µL" in result.violations[0].message


def test_aspirate_without_volume_fills_tip() -> None:
    result = check(
        """
        p.pick_up_tip()
        p.aspirate(location=res["A1"])
        p.dispense(location=plate["A1"])
        p.aspirate(location=res["A1"])
        p.dispense(location=plate["A1"])
        """,
        tips="opentrons_96_filtertiprack_200ul",
    )
    assert props(result) == [(OVERFLOW, 12)]  # 2 x 200 µL > 360 µL


@pytest.mark.parametrize(("api", "severity"), [("2.13", "warning"), ("2.20", "violation")])
def test_overdispense_depends_on_api_level(api: str, severity: str) -> None:
    result = check(
        'p.pick_up_tip()\np.aspirate(50, res["A1"])\np.dispense(80, plate["A1"])', api=api
    )
    assert [(f.property, f.severity) for f in result.findings] == [(TIP_VOLUME, severity)]


@pytest.mark.parametrize(("api", "fills"), [("2.15", True), ("2.16", False)])
def test_aspirate_zero_depends_on_api_level(api: str, fills: bool) -> None:
    # Below 2.16 aspirate(0) fills the tip, so a following 100 µL aspirate exceeds capacity.
    result = check('p.pick_up_tip()\np.aspirate(0, res["A1"])\np.aspirate(100, res["A1"])', api=api)
    assert bool(result.violations) == fills


def test_aspirate_without_tip() -> None:
    result = check('p.aspirate(50, res["A1"])')
    assert props(result) == [(NO_TIP, 8)]


def test_drop_tip_resets_tip_volume() -> None:
    result = check(
        """
        p.pick_up_tip()
        p.aspirate(250, res["A1"])
        p.drop_tip()
        p.pick_up_tip()
        p.aspirate(250, res["A1"])
        """
    )
    assert result.violations == []


# ---- front end ----------------------------------------------------------------------------------


def test_chained_calls_and_previous_location() -> None:
    result = check(
        """
        p.pick_up_tip()
        p.aspirate(200, res["A1"]).dispense(200, plate["A1"])
        p.move_to(res["A1"].top())
        p.aspirate(200)
        p.dispense(200, plate["A1"].bottom(1))
        """
    )
    assert props(result) == [(OVERFLOW, 12)]


def test_list_comprehension_and_module_constants() -> None:
    src = dedent(
        """
        metadata = {"apiLevel": "2.13"}
        VOL = 120
        N = 3

        def run(ctx):
            tips = ctx.load_labware("opentrons_96_tiprack_300ul", "1")
            plate = ctx.load_labware("corning_96_wellplate_360ul_flat", "2")
            p = ctx.load_instrument("p300_single_gen2", "right", tip_racks=[tips])
            dests = [plate.wells()[i * 8] for i in range(N)]
            for d in dests:
                p.pick_up_tip()
                p.aspirate(VOL, plate["H12"])
                p.dispense(VOL * 4, d)
                p.drop_tip()
        """
    )
    result = check_volumes(lower(src))
    # Each dispense asks for 480 µL from a tip holding 120 µL (warning at API 2.13).
    assert [f.severity for f in result.findings] == ["warning"] * 3
    assert [f.context for f in result.findings] == [(("d", w),) for w in ("A1", "A2", "A3")]
    assert result.unsupported is None


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ('p.transfer(100, res["A1"], plate["A1"])', "pipette.transfer"),
        ("v = protocol.params.vol", "protocol.params.vol"),
        ('v = get_values("vol")', "get_values"),
        ('w = plate["H13"]', "well 'H13' does not exist"),
        ("while True:\n    pass", "While"),
    ],
)
def test_unsupported_constructs_stop_with_reason(body: str, reason: str) -> None:
    result = check('p.pick_up_tip()\np.aspirate(400, res["A1"])\n' + body)
    assert props(result) == [(TIP_VOLUME, 9)]  # steps before the stop are still checked
    assert result.unsupported is not None
    assert result.unsupported.line == 10
    assert reason in result.unsupported.reason


def test_missing_api_level_is_unsupported() -> None:
    prog = lower("def run(protocol):\n    pass\n")
    assert prog.unsupported is not None and "apiLevel" in prog.unsupported.reason


def test_unknown_labware_is_unsupported() -> None:
    prog = lower(
        'metadata = {"apiLevel": "2.13"}\n'
        "def run(protocol):\n"
        '    protocol.load_labware("my_custom_plate", 1)\n'
    )
    assert prog.unsupported is not None and "my_custom_plate" in prog.unsupported.reason
