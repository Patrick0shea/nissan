"""otverify: check an Opentrons protocol for volume-safety bugs, for all runtime-parameter values.

Exit codes: 0 no violations and the whole protocol was analysed (proved for all parameter
values); 1 violations found; 2 usage or parse error; 3 no violations, but part of the protocol
could not be analysed or the solver could not decide a check.
"""

import argparse
import json
import sys
from pathlib import Path

from otverify.frontend import lower
from otverify.model import ParamSpec, Program
from otverify.volume import Finding, Result, check_volumes, union


def _context(f: Finding) -> str:
    if not f.context:
        return ""
    return "  [" + ", ".join(f"{k}={v}" for k, v in f.context) + "]"


def _value(v: object) -> str:
    return repr(v) if isinstance(v, str) else f"{v:g}" if isinstance(v, float) else str(v)


def _when(first: Finding, group: list[Finding]) -> str:
    """Witness of the representative; violating ranges and defaults over the whole group."""
    parts = []
    if first.witness:
        parts.append("when " + ", ".join(f"{k}={_value(v)}" for k, v in first.witness))
    names = dict.fromkeys(name for f in group for name, _ in f.ranges)
    ranges = []
    for name in names:
        merged = union([i for f in group for n, i in f.ranges if n == name])
        ranges.append(f"{name} ∈ " + " ∪ ".join(str(i) for i in merged))
    if ranges:
        parts.append("violating " + "; ".join(ranges))
    if not any(f.at_default for f in group):
        parts.append("NOT reachable at default parameter values")
    return "      " + "; ".join(parts) if parts else ""


def _representative(f: Finding) -> tuple[bool, tuple[object, ...]]:
    """Order a group's findings: reachable at defaults first, then the smallest witness (for one
    parameter, that is the finding that fails for the widest range of values)."""
    return (not f.at_default, tuple(v for _, v in f.witness if not isinstance(v, str)))


def _domain(p: ParamSpec, enumerated: tuple[str, ...]) -> str:
    if p.name in enumerated:
        text = f"{p.name} ∈ [{_value(p.minimum)}, {_value(p.maximum)}] (enumerated)"
    elif p.minimum is not None:
        text = f"{p.name} ∈ [{_value(p.minimum)}, {_value(p.maximum)}]"
    elif p.source == "default":
        return f"{p.name} = {_value(p.default)} (DEFAULT ONLY: no range known)"
    else:
        text = f"{p.name} ∈ {{{', '.join(_value(c) for c in p.choices or ())}}}"
    return text if p.source in ("declared", "options") else f"{text} [from {p.source}]"


def format_params(programs: list[Program]) -> str:
    if not programs or not programs[0].params:
        return ""
    prog = programs[0]
    return "  parameters: " + "; ".join(_domain(p, prog.enumerated) for p in prog.params)


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
        if when := _when(first, fs):
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


def to_json(path: str, programs: list[Program], result: Result) -> dict[str, object]:
    """Machine-readable result (for the corpus scripts and the evaluation)."""
    prog = programs[0] if programs else Program()
    groups: dict[tuple[str, str, int], list[Finding]] = {}
    for f in result.findings:
        groups.setdefault((f.severity, f.property, f.line), []).append(f)
    findings = []
    for (severity, prop, line), fs in groups.items():
        first = min(fs, key=_representative)
        names = dict.fromkeys(n for f in fs for n, _ in f.ranges)
        findings.append(
            {
                "severity": severity,
                "property": prop,
                "line": line,
                "message": first.message,
                "context": [[k, str(v)] for k, v in first.context],
                "witness": {k: v for k, v in first.witness},
                "ranges": {
                    n: [str(i) for i in union([i for f in fs for m, i in f.ranges if m == n])]
                    for n in names
                },
                "at_default": any(f.at_default for f in fs),
                "occurrences": len(fs),
            }
        )
    return {
        "protocol": path,
        "api_level": ".".join(map(str, prog.api_level)) if prog.api_level else None,
        "programs": len(programs),
        "steps": sum(len(p.steps) for p in programs),
        "parameters": [
            {
                "name": p.name,
                "kind": p.kind,
                "source": p.source,
                "domain": _domain(p, prog.enumerated),
            }
            for p in prog.params
        ],
        "enumerated": list(prog.enumerated),
        "findings": findings,
        "unchecked_aspirations": sum(result.unchecked_aspirations.values()),
        "unsupported": [{"line": u.line, "reason": u.reason} for u in result.unsupported],
        "complete": not result.unsupported,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="otverify", description=__doc__)
    parser.add_argument("protocol", help="path to an Opentrons Python protocol")
    parser.add_argument(
        "--fields",
        help="fields.json for get_values() (default: fields.json next to the protocol, if any)",
    )
    parser.add_argument("--json", action="store_true", help="print a JSON result instead")
    args = parser.parse_args(argv)
    fields_path = Path(args.fields) if args.fields else Path(args.protocol).parent / "fields.json"
    try:
        source = Path(args.protocol).read_text()
        fields = None
        if args.fields or fields_path.is_file():
            fields = json.loads(fields_path.read_text())
        labware_dir = Path(args.protocol).parent / "labware"
        custom = [json.loads(f.read_text()) for f in sorted(labware_dir.glob("*.json"))]
        programs = lower(source, fields, custom)
    except (OSError, SyntaxError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"otverify: {args.protocol}: {exc}", file=sys.stderr)
        return 2
    result = check_volumes(programs)
    if args.json:
        print(json.dumps(to_json(args.protocol, programs, result), default=str))
    else:
        print(format_result(args.protocol, result))
        if params := format_params(programs):
            print(params)
    if result.violations:
        return 1
    return 3 if result.unsupported or result.undecided else 0


if __name__ == "__main__":
    raise SystemExit(main())
