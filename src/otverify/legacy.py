"""Parameters of legacy Protocol Library protocols: `get_values(...)` backed by `fields.json`.

The library build prepends a `get_values` that returns each field's default: `options[0].value`
for a dropDown, else `default` (Opentrons/Protocols `protolib/parse/parseOT2v2.py`). Numeric
fields declare no minimum or maximum, so their domains are inferred from the label (D-016):
- `a-b`, `a–b`, `a to b` and `between a and b` give [a, b].
- `up to N`, `max N` and `maximum N` give [min(1, default), N].
- If the default lies outside the parsed range, the range is widened to include it, and the
  source is recorded as "label+default".
- With no recognisable range, the field is analysed at its default only (source "default").
"""

import re
from typing import Any

from otverify.model import ParamSpec

_NUM = r"(\d+(?:\.\d+)?)"
_TWO_SIDED = [
    re.compile(rf"between\s+{_NUM}\s+and\s+{_NUM}", re.I),
    re.compile(rf"(?<![\d.]){_NUM}\s*(?:-|–|\bto\b)\s*{_NUM}(?![\d.])", re.I),
]
_UPPER = re.compile(rf"\b(?:up\s+to|max(?:imum)?)\s*:?\s*{_NUM}", re.I)


def _number(text: str, kind: str) -> float | int:
    v = float(text)
    return int(v) if kind == "int" and v.is_integer() else v


def label_range(label: str, kind: str, default: float) -> tuple[float, float, str] | None:
    """(minimum, maximum, source) parsed from a field label, or None."""
    bounds: tuple[float, float] | None = None
    for pattern in _TWO_SIDED:
        if m := pattern.search(label):
            lo, hi = _number(m.group(1), kind), _number(m.group(2), kind)
            if lo <= hi:
                bounds = (lo, hi)
                break
    if bounds is None and (m := _UPPER.search(label)):
        hi = _number(m.group(1), kind)
        bounds = (min(1, default), hi)
    if bounds is None:
        return None
    lo, hi = bounds
    if lo <= default <= hi:
        return lo, hi, "label"
    return min(lo, default), max(hi, default), "label+default"


def _kind_of(value: object) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "str"


def params_from_fields(fields: list[dict[str, Any]]) -> list[ParamSpec]:
    """ParamSpecs for a fields.json list. Raises ValueError on a malformed field."""
    specs = []
    for field in fields:
        name, ftype = field.get("name"), field.get("type")
        if not isinstance(name, str):
            raise ValueError(f"field without a name: {field!r}")
        if ftype == "dropDown":
            options = [o["value"] for o in field.get("options", [])]
            if not options:
                raise ValueError(f"dropDown field {name!r} has no options")
            # A list (not a set): keep the order, the first option is the library default.
            choices = tuple(dict.fromkeys(options))
            default = options[0]
            specs.append(
                ParamSpec(name, _kind_of(default), default, None, None, choices, 0, "options")
            )
            continue
        default = field.get("default")
        kind = _kind_of(default)
        if ftype in ("int", "float") and kind in ("int", "float"):
            kind = "int" if ftype == "int" and kind == "int" else "float"
            parsed = label_range(str(field.get("label", "")), kind, default)
            if parsed is not None:
                lo, hi, source = parsed
                specs.append(ParamSpec(name, kind, default, lo, hi, None, 0, source))
                continue
        specs.append(ParamSpec(name, kind, default, None, None, (default,), 0, "default"))
    return specs
