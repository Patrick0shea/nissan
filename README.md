# otverify

A research prototype. It checks Opentrons Python protocols (API v2) for volume, contamination, resource and timing bugs **for every parameter value in a declared range**, using static analysis plus the Z3 SMT solver. The target users are self-driving labs, where an optimiser picks parameter values at runtime.

Status: milestone M2. `otverify protocol.py` checks well overflow, overdraw and tip volume for **every** value of the `add_parameters` runtime parameters (Z3). It reports a witness, the violating range, and whether the bug is reachable at the default values. Not yet supported: `get_values`, `transfer`/`distribute`, or control flow that depends on a parameter. See [docs/ROADMAP.md](docs/ROADMAP.md).

## Why
`opentrons_simulate` checks one run, at default parameter values. For example, it accepts a protocol that dispenses 3 × `vol` µL into a 360 µL well when `vol` defaults to 100, even though `vol` may go up to 300. See `experiments/2026-10-09-simulator-probe/`.

## Docs
- [GOAL](docs/GOAL.md): research question, hypotheses, scope.
- [PROPERTIES](docs/PROPERTIES.md): what is verified, and how.
- [EVALUATION](docs/EVALUATION.md): datasets, baselines, metrics.
- [RELATED_WORK](docs/RELATED_WORK.md)
- [DECISIONS](docs/DECISIONS.md)

## Development
```
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
Baseline simulator (separate venv, OT-2 support ends at 9.0.0):
```
uv venv -p 3.12 .venv-sim && uv pip install -p .venv-sim/bin/python opentrons==9.0.0
```
Corpus: `scripts/fetch_corpus.sh` (Opentrons Protocol Library, pinned commit).
