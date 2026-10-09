# Probe: what does `opentrons_simulate` catch?

**Date:** 2026-10-09. **Setup:** `opentrons==9.0.0`, Python 3.12, `opentrons_simulate <file>`.

| File | Bug | Result |
|---|---|---|
| `overfill.py` (`vol` default 100) | Aspirates 3× from empty A1. B1 receives 3·vol µL, and B1's capacity is 360. | **No error.** The run log is clean. |
| `overfill.py` with `default=200` | 600 µL dispensed into a 360 µL well | **No error.** |
| `tips.py` | 97 pickups from a 96-tip rack | `OutOfTipsError` |
| `pipmax.py` | `aspirate(350)` with a p300 | `InvalidAspirateVolumeError: Cannot aspirate 350.0 µL when only 300 is available in the tip.` |
| `slot.py` | `load_labware(..., 13)` | `ValueError: '13' is not a valid deck slot` |
| `liq.py` (API 2.22, `load_liquid` 50 µL, then aspirate 200) | Over-aspirate from a tracked well | `LiquidHeightUnknownError` (meniscus). Inconclusive, follow up. |

Also confirmed:
- `simulate()` always passes `run_time_parameters_with_overrides=None`, so only default values are simulated.
- `opentrons` 9.1.0, 9.1.2 and 10.0.0 refuse every protocol whose `robotType` is OT-2.

**Takeaway.** At default values the simulator already enforces pipette, tip and deck limits. It does not enforce well capacity or source depletion, and it never explores parameter values.
