# Probe: a distribute that silently moves no liquid

**Date:** 2026-10-10. **Setup:** `opentrons==9.0.0`. A p300 single-channel with 200 µL filter tips runs `p.distribute(250, res["A1"], plate.wells()[:4])`.

**`opentrons_simulate` output** (in full, no error or warning):
```
Distributing 250.0 from A1 of NEST 12 Well Reservoir 15 mL on 3 to A1 of Corning 96 Well Plate 360 µL Flat on 2
	Transferring 250.0 from A1 of NEST 12 Well Reservoir 15 mL on 3 to A1 of Corning 96 Well Plate 360 µL Flat on 2
		Picking up tip from A1 of Opentrons OT-2 96 Filter Tip Rack 200 µL on 1
		Dropping tip into A1 of Opentrons Fixed Trash on 12
```

**Why.** Here is how `TransferPlan._plan_distribute` handles this case:
- It splits volumes against the **pipette's** maximum: 300 µL minus the 20 µL default disposal volume = 280 µL. So 250 µL is not split.
- It then groups dispenses against the **tip's** capacity: 250 + 20 > 200. So the first group is empty, and the loop `break`s.

The run picks up a tip, drops it, and moves nothing.

**otverify:** `line 7: warning API: distribute() moves no liquid: ... Opentrons silently skips it`.

**Takeaway.** This is a silent-failure class that a clean simulator run hides. When the volume is a runtime parameter, it is also parameter-dependent: it appears only for volumes above the tip capacity minus the disposal volume.
