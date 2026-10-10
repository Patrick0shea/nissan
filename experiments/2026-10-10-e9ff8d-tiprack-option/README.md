# Finding: a tip-rack option that makes a library protocol crash (parameter-only)

**Protocol:** `Opentrons/Protocols` @ `2f447c9`, `protocols/e9ff8d_part2`. `e9ff8d_part4` has the same pattern.

**What otverify reports** (from `get_values` + `fields.json`, API 2.x):
```
line 112: violation P1c: aspirating 300 µL into a tip holding 0 µL of 200 µL
    when tip_rack='opentrons_96_filtertiprack_200ul'; NOT reachable at default parameter values
```

**Cause.** `fields.json` offers two tip racks, "GEB 300ul Tips" (the default) and "Opentrons 200ul Filter tips". The ethanol-wash step aspirates `m300.max_volume`. That is the **pipette's** maximum (300 µL), not the tip's capacity, so with the 200 µL option the aspirate cannot fit.

**Ground truth** (`reproduce.sh`, opentrons_simulate 9.0.0 with the protocol's custom labware):

| tip_rack | opentrons_simulate |
|---|---|
| `opentrons_96_tiprack_300ul` (default) | passes |
| `opentrons_96_filtertiprack_200ul` | `AssertionError: Cannot aspirate more than pipette max volume` |

**Classification.**
- TP, parameter-only: the default simulation, which is what the library build runs, passes.
- It is caught by B2 sampled simulation only if the sampler picks this dropDown option.
- It is the first confirmed H1/H2 instance in D1.
