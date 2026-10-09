# Probe: does the M2 tip-capacity boundary agree with the simulator?

**Date:** 2026-10-09. **Setup:** `opentrons==9.0.0`. A p300 with 300 µL tips aspirates `vol`, a float parameter in [10, 400]. The simulator only runs default values, so the default is set on each side of the boundary.

| File | Default | `opentrons_simulate` | `otverify` |
|---|---|---|---|
| `tip_default_300.py` | 300.0 | passes | P1c violated for `vol ∈ (300, 400]`, witness 301, **not reachable at defaults** |
| `tip_default_300_5.py` | 300.5 | `InvalidAspirateVolumeError: Cannot aspirate 300.5 µL when only 300 is available in the tip.` | P1c violated for `vol ∈ (300, 400]`, reachable at defaults |

**Takeaways.**
- The boundary and the "reachable at defaults" flag agree with the simulator on both sides.
- With the default at 300, the simulator passes a protocol that fails for every `vol > 300` in its declared range. This is the parameter-only case that H2 is about.
- One caveat: our guards allow 1e-6 µL of slack (`EPS`), so 300.0000005 counts as safe here but would be rejected by the robot.
