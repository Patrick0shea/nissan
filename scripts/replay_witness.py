"""Replay otverify witnesses in opentrons_simulate (the ground truth for labelling, EVALUATION.md).

Run in the baseline venv:

    .venv-sim/bin/python scripts/replay_witness.py results/d1.jsonl [property ...]

For every violation that is NOT reachable at the defaults, in a legacy library protocol, this
injects a `get_values` with the defaults plus the witness, runs the simulator with the protocol's
custom labware, and records whether the simulator raises. Only some properties are observable by
the simulator: tip capacity (P1c), CRASH and missing tips (TIP). Well volumes (P1a, P1b) are not,
so they need the B2+ tracker instead.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

OBSERVABLE = {"P1c", "CRASH", "TIP"}


def defaults(fields: list[dict]) -> dict:
    return {
        f["name"]: f["options"][0]["value"] if f["type"] == "dropDown" else f.get("default")
        for f in fields
    }


def replay(protocol: Path, witness: dict, simulate: str) -> tuple[str, str]:
    folder = protocol.parent
    fields_path = folder / "fields.json"
    if not fields_path.is_file():
        return "skip", "not a legacy protocol"
    values = {**defaults(json.loads(fields_path.read_text())), **witness}
    header = f"def get_values(*names):\n    v = {values!r}\n    return [v[n] for n in names]\n\n"
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / protocol.name
        script.write_text(header + protocol.read_text())
        cmd = [simulate]
        if (folder / "labware").is_dir():
            cmd += ["-L", str(folder / "labware")]
        try:
            proc = subprocess.run(cmd + [str(script)], capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return "timeout", ""
    errors = [line for line in (proc.stdout + proc.stderr).splitlines() if "Error" in line]
    return ("raises", errors[-1][:200]) if errors or proc.returncode else ("passes", "")


def main() -> None:
    results = [json.loads(line) for line in open(sys.argv[1])]
    wanted = set(sys.argv[2:]) or OBSERVABLE
    simulate = str(Path(sys.executable).parent / "opentrons_simulate")
    for r in results:
        if r.get("status") != "ok":
            continue
        for f in r["findings"]:
            if f["severity"] != "violation" or f["at_default"] or f["property"] not in wanted:
                continue
            outcome, detail = replay(Path(r["protocol"]), f["witness"], simulate)
            name = Path(r["protocol"]).parent.name
            print(f"{name:32} L{f['line']:<4} {f['property']:5} {outcome:7} {detail}", flush=True)
            break  # one witness per protocol


if __name__ == "__main__":
    main()
