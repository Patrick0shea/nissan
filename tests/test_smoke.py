import ast
from pathlib import Path

import z3

import otverify
from otverify.cli import main

PROBE = Path(__file__).parent.parent / "experiments" / "2026-10-09-simulator-probe" / "overfill.py"


def test_package_imports() -> None:
    assert otverify.__version__


def test_z3_finds_overfill_witness() -> None:
    # The M2 query in miniature: exists vol in [10, 300] with 3*vol > 360?
    vol = z3.Real("vol")
    s = z3.Solver()
    s.add(vol >= 10, vol <= 300, 3 * vol > 360)
    assert s.check() == z3.sat
    assert s.model().eval(3 * vol > 360)


def test_probe_protocol_parses() -> None:
    tree = ast.parse(PROBE.read_text())
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {"add_parameters", "run"} <= names


def test_cli_stub_reports_not_implemented() -> None:
    assert main([str(PROBE)]) == 2
