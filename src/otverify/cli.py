"""otverify: check an Opentrons protocol for volume-safety bugs (M1: constant values only).

Exit codes: 0 no violations and the whole protocol was analysed; 1 violations found;
2 usage or parse error; 3 no violations, but part of the protocol could not be analysed.
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


def format_result(path: str, result: Result) -> str:
    # The same line usually fails in many loop iterations: show the first, count the rest.
    groups: dict[tuple[str, str, int], list[Finding]] = {}
    for f in result.findings:
        groups.setdefault((f.severity, f.property, f.line), []).append(f)
    n = len(result.violations)
    lines = [f"{path}: {n} violation{'s' if n != 1 else ''}"]
    for (severity, prop, line), fs in groups.items():
        more = f" (+{len(fs) - 1} more)" if len(fs) > 1 else ""
        lines.append(f"  line {line}: {severity} {prop}: {fs[0].message}{_context(fs[0])}{more}")
    if result.unchecked_aspirations:
        total = sum(result.unchecked_aspirations.values())
        wells = ", ".join(sorted(result.unchecked_aspirations))
        lines.append(
            f"  not checked: {total} aspiration(s) from wells with unknown contents: {wells}"
        )
    if result.unsupported:
        u = result.unsupported
        lines.append(f"  INCOMPLETE: analysis stopped at line {u.line}: {u.reason}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="otverify", description=__doc__)
    parser.add_argument("protocol", help="path to an Opentrons Python protocol")
    args = parser.parse_args(argv)
    try:
        source = Path(args.protocol).read_text()
        prog = lower(source)
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        print(f"otverify: {args.protocol}: {exc}", file=sys.stderr)
        return 2
    result = check_volumes(prog)
    print(format_result(args.protocol, result))
    if result.violations:
        return 1
    return 3 if result.unsupported else 0


if __name__ == "__main__":
    raise SystemExit(main())
