"""otverify: check an Opentrons protocol for volume-safety bugs, for all runtime-parameter values.

Exit codes: 0 no violations and the whole protocol was analysed (proved for all parameter
values); 1 violations found; 2 usage or parse error; 3 no violations, but part of the protocol
could not be analysed or the solver could not decide a check.
"""

import argparse
import sys
from pathlib import Path

from otverify.frontend import lower
from otverify.volume import Finding, Result, check_volumes


def _context(f: Finding) -> str:
    if not f.context:
        return ""
    return "  [" + ", ".join(f"{k}={v}" for k, v in f.context) + "]"


def _value(v: object) -> str:
    return repr(v) if isinstance(v, str) else f"{v:g}" if isinstance(v, float) else str(v)


def _when(f: Finding) -> str:
    parts = []
    if f.witness:
        parts.append("when " + ", ".join(f"{k}={_value(v)}" for k, v in f.witness))
    if f.ranges:
        parts.append("violating " + "; ".join(f"{k} ∈ {r}" for k, r in f.ranges))
    if not f.at_default:
        parts.append("NOT reachable at default parameter values")
    return "      " + "; ".join(parts) if parts else ""


def _representative(f: Finding) -> tuple[bool, tuple[object, ...]]:
    """Order a group's findings: reachable at defaults first, then the smallest witness (for one
    parameter, that is the finding that fails for the widest range of values)."""
    return (not f.at_default, tuple(v for _, v in f.witness if not isinstance(v, str)))


def format_result(path: str, result: Result) -> str:
    # The same line usually fails in many loop iterations or parameter combinations: show one
    # representative and count the rest.
    groups: dict[tuple[str, str, int], list[Finding]] = {}
    for f in result.findings:
        groups.setdefault((f.severity, f.property, f.line), []).append(f)
    n = len(result.violations)
    lines = [f"{path}: {n} violation{'s' if n != 1 else ''}"]
    for (severity, prop, line), fs in groups.items():
        first = min(fs, key=_representative)
        more = f" (+{len(fs) - 1} more)" if len(fs) > 1 else ""
        lines.append(f"  line {line}: {severity} {prop}: {first.message}{_context(first)}{more}")
        if when := _when(first):
            lines.append(when)
    if result.unchecked_aspirations:
        total = sum(result.unchecked_aspirations.values())
        wells = ", ".join(sorted(result.unchecked_aspirations))
        lines.append(
            f"  not checked: {total} aspiration(s) from wells with unknown contents: {wells}"
        )
    for u in result.unsupported:
        lines.append(f"  INCOMPLETE: analysis stopped at line {u.line}: {u.reason}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="otverify", description=__doc__)
    parser.add_argument("protocol", help="path to an Opentrons Python protocol")
    args = parser.parse_args(argv)
    try:
        source = Path(args.protocol).read_text()
        programs = lower(source)
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        print(f"otverify: {args.protocol}: {exc}", file=sys.stderr)
        return 2
    result = check_volumes(programs)
    print(format_result(args.protocol, result))
    if result.violations:
        return 1
    return 3 if result.unsupported or result.undecided else 0


if __name__ == "__main__":
    raise SystemExit(main())
