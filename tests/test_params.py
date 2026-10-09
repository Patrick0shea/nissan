"""M2: runtime parameters, checked for all values with Z3."""

from pathlib import Path
from textwrap import dedent

import pytest

from otverify.cli import main
from otverify.frontend import MAX_ASSIGNMENTS, lower
from otverify.volume import OVERDRAW, OVERFLOW, TIP_VOLUME, Result, check_volumes

FIXTURES = Path(__file__).parent / "fixtures"


def protocol(params: str, body: str, api: str = "2.20") -> str:
    """A protocol with a p300 (300 µL tips), a 360 µL plate in slot 2 and a reservoir in slot 3."""
    head = f'requirements = {{"robotType": "OT-2", "apiLevel": "{api}"}}\n\n'
    head += "def add_parameters(parameters):\n"
    head += "".join("    " + line + "\n" for line in dedent(params).strip().splitlines())
    head += dedent(
        """
        def run(protocol):
            tips = protocol.load_labware("opentrons_96_tiprack_300ul", 1)
            plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 2)
            res = protocol.load_labware("nest_12_reservoir_15ml", 3)
            p = protocol.load_instrument("p300_single_gen2", "left", tip_racks=[tips])
            v = protocol.params
        """
    )
    return head + "".join("    " + line + "\n" for line in dedent(body).strip().splitlines())


def check(params: str, body: str, api: str = "2.20") -> Result:
    return check_volumes(lower(protocol(params, body, api)))


VOL = 'parameters.add_int("Volume", "vol", default=100, minimum=10, maximum={max})'
THREE_INTO_B1 = """
p.pick_up_tip()
for _ in range(3):
    p.aspirate(v.vol, res["A1"])
    p.dispense(v.vol, plate["B1"])
"""


# ---- the M2 target: parameter-only overflow -----------------------------------------------------


def test_fixture_overflow_found_with_smallest_witness() -> None:
    result = check_volumes(lower((FIXTURES / "param_overfill.py").read_text()))
    worst = min(result.violations, key=lambda f: f.witness)
    assert worst.property == OVERFLOW and worst.line == 13
    assert worst.witness == (("vol", 121),)
    assert worst.ranges == (("vol", "[121, 400]"),)
    assert worst.context == (("_", 2),)
    assert "363 µL" in worst.message  # amounts are evaluated at the witness
    assert not worst.at_default


def test_cli_prints_witness(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(FIXTURES / "param_overfill.py")]) == 1
    out = capsys.readouterr().out
    assert "when vol=121; violating vol ∈ [121, 400]" in out
    assert "NOT reachable at default parameter values" in out


@pytest.mark.parametrize(("maximum", "violated"), [(120, False), (121, True)])
def test_boundary_is_exact(maximum: int, violated: bool) -> None:
    result = check(VOL.format(max=maximum), THREE_INTO_B1)
    assert bool(result.violations) == violated
    assert result.unsupported == []


def test_reachable_at_default() -> None:
    params = 'parameters.add_int("Volume", "vol", default=200, minimum=10, maximum=300)'
    result = check(params, THREE_INTO_B1)
    assert any(f.at_default for f in result.violations)


def test_float_parameter_gives_open_bound() -> None:
    params = 'parameters.add_float("Volume", "vol", default=100.0, minimum=10.0, maximum=300.0)'
    result = check(params, THREE_INTO_B1)
    ranges = {f.ranges for f in result.violations}
    assert (("vol", "(120, 300]"),) in ranges


def test_tip_capacity_for_all_values() -> None:
    params = 'parameters.add_float("Volume", "vol", default=50.0, minimum=10.0, maximum=400.0)'
    result = check(params, 'p.pick_up_tip()\np.aspirate(v.vol, res["A1"])')
    [f] = result.violations
    assert f.property == TIP_VOLUME and f.ranges == (("vol", "(300, 400]"),)


def test_two_parameters() -> None:
    params = """
        parameters.add_int("A", "a", default=100, minimum=0, maximum=300)
        parameters.add_int("B", "b", default=100, minimum=0, maximum=300)
    """
    body = """
        p.pick_up_tip()
        p.aspirate(v.a, res["A1"])
        p.dispense(v.a, plate["A1"])
        p.aspirate(v.b, res["A1"])
        p.dispense(v.b, plate["A1"])
    """
    [f] = check(params, body).violations
    assert dict(f.witness).keys() == {"a", "b"}
    assert sum(dict(f.witness).values()) > 360  # type: ignore[arg-type]
    assert dict(f.ranges) == {"a": "[61, 300]", "b": "[61, 300]"}


def test_overdraw_from_declared_well_with_parameter() -> None:
    params = 'parameters.add_int("Samples", "n_ul", default=50, minimum=10, maximum=100)'
    body = """
        plate["A1"].load_liquid(protocol.define_liquid("x"), 150)
        p.pick_up_tip()
        for _ in range(2):
            p.aspirate(v.n_ul, plate["A1"])
            p.dispense(v.n_ul, plate["B1"])
    """
    found = {(f.property, f.ranges) for f in check(params, body).violations}
    assert found == {(OVERDRAW, (("n_ul", "[76, 100]"),))}


def test_true_division_of_int_parameter() -> None:
    # total / 3 must be real division: 4 * total / 3 > 360 iff total >= 271.
    params = 'parameters.add_int("Total", "total", default=150, minimum=30, maximum=300)'
    body = """
        p.pick_up_tip()
        for _ in range(4):
            p.aspirate(v.total / 3, res["A1"])
            p.dispense(v.total / 3, plate["A1"])
    """
    worst = min(check(params, body).violations, key=lambda f: f.witness)
    assert worst.witness == (("total", 271),)


def test_min_max_builtins_on_parameters() -> None:
    result = check(VOL.format(max=400), THREE_INTO_B1.replace("v.vol", "min(v.vol, 100)"))
    assert result.violations == []


# ---- finite parameters are enumerated -----------------------------------------------------------


def test_bool_parameter_branch() -> None:
    params = 'parameters.add_bool("Extra wash", "extra", default=False)'
    body = """
        p.pick_up_tip()
        for _ in range(2):
            p.aspirate(150, res["A1"])
            p.dispense(150, plate["A1"])
        if v.extra:
            p.aspirate(150, res["A1"])
            p.dispense(150, plate["A1"])
    """
    [f] = check(params, body).violations
    assert f.witness == (("extra", True),) and not f.at_default


def test_str_choice_selects_labware() -> None:
    params = """
        parameters.add_str("Plate", "plate", default="corning_96_wellplate_360ul_flat",
            choices=[{"display_name": "Corning", "value": "corning_96_wellplate_360ul_flat"},
                     {"display_name": "Bio-Rad PCR", "value": "biorad_96_wellplate_200ul_pcr"}])
    """
    body = """
        dest = protocol.load_labware(v.plate, 4)
        p.pick_up_tip()
        p.aspirate(250, res["A1"])
        p.dispense(250, dest["A1"])
    """
    [f] = check(params, body).violations
    assert f.witness == (("plate", "biorad_96_wellplate_200ul_pcr"),)
    assert "capacity 200 µL" in f.message


def test_finite_and_interval_parameters_combine() -> None:
    params = """
        parameters.add_int("Repeats", "reps", default=1, choices=[
            {"display_name": "1", "value": 1}, {"display_name": "3", "value": 3}])
        parameters.add_int("Volume", "vol", default=100, minimum=10, maximum=200)
    """
    body = """
        p.pick_up_tip()
        for _ in range(v.reps):
            p.aspirate(v.vol, res["A1"])
            p.dispense(v.vol, plate["A1"])
    """
    worst = min(check(params, body).violations, key=lambda f: f.witness)
    assert worst.witness == (("reps", 3), ("vol", 121))


def test_too_many_combinations() -> None:
    params = "\n".join(f'parameters.add_bool("B{i}", "b{i}", default=False)' for i in range(9))
    assert 2**9 > MAX_ASSIGNMENTS
    [prog] = lower(protocol(params, "pass"))
    assert prog.unsupported is not None and "combinations" in prog.unsupported.reason


# ---- what M2 does not model: reported, never silent ---------------------------------------------


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("if v.vol > 150:\n    pass", "branch on parameter-dependent condition"),
        ("for i in range(v.vol):\n    pass", "`range()` of a parameter-dependent value"),
        ("w = plate.wells()[v.vol]", "index depends on a parameter"),
        ("x = v.vol // 2", "FloorDiv"),
        ("x = 100 / v.vol", "division by a parameter-dependent value"),
        ("x = v.nope", "no runtime parameter named `nope`"),
    ],
)
def test_symbolic_control_flow_is_unsupported(body: str, reason: str) -> None:
    result = check(VOL.format(max=400), body)
    [stop] = result.unsupported
    assert reason in stop.reason


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ('parameters.add_int("V", "vol", default=5)', "needs choices, or both minimum and maximum"),
        ('parameters.add_int("V", "vol", default=5, minimum=10, maximum=20)', "outside [10, 20]"),
        ('parameters.add_csv_file("Plate map", "map")', "CSV"),
        ('parameters.add_str("S", "s", default="a")', "needs choices"),
        (
            'parameters.add_int("V", "vol", default=1, minimum=0, maximum=5)\n'
            'parameters.add_int("W", "vol", default=1, minimum=0, maximum=5)',
            "duplicate",
        ),
    ],
)
def test_invalid_parameter_definitions(params: str, reason: str) -> None:
    [prog] = lower(protocol(params, "pass"))
    assert prog.unsupported is not None and reason in prog.unsupported.reason


def test_add_parameters_needs_api_218() -> None:
    [prog] = lower(protocol(VOL.format(max=400), "pass", api="2.17"))
    assert prog.unsupported is not None and "2.18" in prog.unsupported.reason
