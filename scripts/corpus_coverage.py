"""Run otverify over the Protocol Library corpus (D1) and summarise coverage and findings.

    .venv/bin/python scripts/corpus_coverage.py data/corpus/Protocols results/d1.jsonl

Each protocol runs in its own `otverify --json` subprocess with a timeout. One JSON object per
protocol is written to the output file. A summary is printed.
"""

import collections
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TIMEOUT_S = 120


def run_one(path: Path) -> dict[str, object]:
    start = time.monotonic()
    cmd = [sys.executable, "-m", "otverify.cli", "--json", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"protocol": str(path), "status": "timeout", "seconds": TIMEOUT_S}
    seconds = round(time.monotonic() - start, 2)
    if proc.returncode in (0, 1, 3) and proc.stdout.strip():
        return {**json.loads(proc.stdout), "status": "ok", "seconds": seconds}
    return {
        "protocol": str(path),
        "status": "error",
        "seconds": seconds,
        "stderr": proc.stderr[-2000:],
    }


def reason_class(reason: str) -> str:
    """Collapse a stop reason to a category for the summary."""
    if re.match(r"labware .([^.]*). is not in", reason):
        return "labware not in snapshot or protocol folder"
    if "depends on parameter" in reason:
        return re.sub(r"`[^`]*`", "`…`", reason.split(" depends on")[0]) + " depends on a parameter"
    if re.match(r"(only \d+ of \d+ combinations|\d+ combinations)", reason):
        return "finite-parameter combinations sampled"
    reason = re.sub(r"\d{2,}", "N", reason)
    return reason[:90]


def main() -> None:
    root, out = Path(sys.argv[1]), Path(sys.argv[2])
    ignored = {p.parent for p in root.glob("protocols/*/.ignore")}
    files = sorted(p for p in root.glob("protocols/*/*.ot2.apiv2.py") if p.parent not in ignored)
    out.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=int(sys.argv[3]) if len(sys.argv) > 3 else 4) as pool:
        results = list(pool.map(run_one, files))
    with out.open("w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    status = collections.Counter(r["status"] for r in results)
    ok = [r for r in results if r["status"] == "ok"]
    complete = [r for r in ok if r["complete"]]
    stops = collections.Counter(
        reason_class(r["unsupported"][0]["reason"]) for r in ok if r["unsupported"]
    )
    with_findings = [r for r in ok if any(f["severity"] == "violation" for f in r["findings"])]
    param_only = [
        r
        for r in with_findings
        if any(f["severity"] == "violation" and not f["at_default"] for f in r["findings"])
    ]
    print(f"{len(files)} protocols: {dict(status)}")
    print(
        f"fully analysed: {len(complete)}; with ≥1 violation: {len(with_findings)} "
        f"(any parameter-only: {len(param_only)})"
    )
    print(f"median steps checked: {sorted(r['steps'] for r in ok)[len(ok) // 2] if ok else 0}")
    print("top stop reasons:")
    for reason, n in stops.most_common(25):
        print(f"  {n:4d}  {reason}")


if __name__ == "__main__":
    main()
