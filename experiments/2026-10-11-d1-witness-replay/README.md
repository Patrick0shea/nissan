# D1 at commit c55493f: corpus results and witness replay

**Inputs:**
- `Opentrons/Protocols` @ `2f447c9`, all 832 protocol files (833 directories, one with `.ignore`).
- `otverify` at commit `c55493f`.
- Baseline `opentrons==9.0.0`.

**Files:**
- `d1_results.jsonl`: one `otverify --json` result per protocol, from `scripts/corpus_coverage.py`.
- `replay.txt`: from `scripts/replay_witness.py`.

## Coverage
| | Protocols |
|---|---|
| Analysed without error | 831 (1 timeout at 120 s: `072463`) |
| Fully analysed (no stop point, all finite combinations) | 476 |
| ≥ 1 violation | 157 |
| ≥ 1 violation **not reachable at the defaults** | 41 |
| With an int parameter enumerated (D-017) | 274 |
| With at least one DEFAULT-ONLY parameter (no range known, D-016) | 594 |

Runtime per protocol: median 0.2 s, p95 6.6 s, max 84 s.

## Violations by property (finding groups / protocols)
| Property | Groups | Protocols | Protocols with a parameter-only violation |
|---|---|---|---|
| P1a overflow | 156 | 86 | 16 |
| P1b overdraw (delivered, D-025) | 257 | 106 | 25 |
| P1c tip volume | 28 | 13 | 8 |
| TIP no tip | 2 | 1 | 1 |
| CRASH | 3 | 3 | 2 |

Warnings: API 14 groups (11 protocols), P1b removal 18 (14), P1c legacy over-dispense 56 (36).

## Witness replay (ground truth for what the simulator can observe)
For each protocol with a parameter-only P1c/TIP/CRASH violation:
1. Inject the defaults; the simulator must pass (the control).
2. Inject the defaults plus the witness; it must fail at the reported line.

| Outcome | Protocols |
|---|---|
| **Confirmed** (defaults pass, witness fails at the reported line) | **9**: 17cb2d, 1eeb01, 2c62b7, 3db190, 412ec7, 51df08, customizable_serial_dilution_ot2, e9ff8d_part2, e9ff8d_part4 |
| Unreplayable (protocol imports a module 9.0.0 lacks) | 1: generic_pcr_prep_2 |
| Refuted | 0 (`6f4e2c` was refuted at c3ed8f4 and fixed in a5fed56: the fixed trash passes Opentrons' first-row filter) |

In 8 of the 9, a pipette or tip-rack **dropDown option** (or a sample count) makes a volume exceed the tip. `opentrons_simulate` at the defaults, which is what the library's own build runs, passes all 9. This is direct H1 evidence, and H2 evidence against B1.

## Not yet labelled
- **P1a/P1b:** the simulator cannot observe well volumes, so these need the B2+ tracker and manual labelling (EVALUATION.md).
- **Known false-positive sources found so far, and fixed:**
  - over-aspiration for complete removal (D-025);
  - refills at `pause()` (D-026);
  - well geometry and pipette state (D-024).
- **Expected remaining false-positive sources:**
  - custom labware JSON with wrong `totalLiquidVolume`;
  - nominal capacities below what labs actually load;
  - waste emptied without a pause.

  Labelling will measure these.
