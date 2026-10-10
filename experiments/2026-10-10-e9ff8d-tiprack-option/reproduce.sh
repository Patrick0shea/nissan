#!/usr/bin/env bash
# Reproduce the e9ff8d_part2 finding with opentrons_simulate 9.0.0 (baseline venv).
# Usage: reproduce.sh <path to Opentrons/Protocols checkout @2f447c9> <opentrons_simulate>
set -euo pipefail
P="$1/protocols/e9ff8d_part2"; SIM="$2"; OUT=$(mktemp -d)
for choice in opentrons_96_tiprack_300ul opentrons_96_filtertiprack_200ul; do
  python3 - "$P" "$choice" > "$OUT/run_$choice.py" <<'PY'
import json, pathlib, sys
P, choice = pathlib.Path(sys.argv[1]), sys.argv[2]
fields = json.load(open(P / "fields.json"))
values = {f["name"]: f["options"][0]["value"] if f["type"] == "dropDown" else f["default"] for f in fields}
values["tip_rack"] = choice
print(f"def get_values(*names):\n    v = {values!r}\n    return [v[n] for n in names]\n")
print(next(P.glob("*.ot2.apiv2.py")).read_text())
PY
  echo "== tip_rack=$choice"
  "$SIM" -L "$P/labware" "$OUT/run_$choice.py" 2>&1 | grep -E "Error" | head -1 || echo "(no error)"
done
