# Properties

## Common model
- **Parameters.** `P` is a vector of protocol parameters with a domain `D`: integer or real intervals, finite choice sets, and booleans. A property holds iff it holds on **every** execution for **every** `p ∈ D`. A violation is reported with a witness `p*` and the failing step.
- **State.** Each well `w` has a volume `v(w)` (µL, Z3 `Real`), a capacity `C(w)` = `totalLiquidVolume` from the labware definition, and a set of liquid labels `L(w)`. Each pipette has a tip state (none, or a tip with volume and labels) and a tip-rack cursor.
- **Initial volumes.** Taken from `load_liquid` where it is present. Otherwise **unknown**. See open question Q2 and the "Precise definition" bullets below.
- **Front end.** We parse with `ast`, resolve `load_labware`, `load_instrument` and `params`/`get_values` bindings, and lower `transfer`/`distribute`/`consolidate`/`mix` into primitive `aspirate`, `dispense`, `pick_up_tip` and `drop_tip` steps. Their expansion semantics, such as splitting volumes above the pipette's maximum and `disposal_volume`, must be confirmed against `opentrons==9.0.0` source before we model them. Loops with concrete trip counts are unrolled. Loops whose trip count depends on a parameter need a summary, which is the main technical risk (see ROADMAP M3).

---

## P1. Volume safety (SMT)

**Definition.** For all `p ∈ D`, at every step:
- (a) `dispense(d, w)` ⇒ `v(w) + d ≤ C(w)`. No overflow.
- (b) `aspirate(a, w)` ⇒ `a ≤ v(w)`. No aspirating liquid that is not there.
- (c) `aspirate(a)` ⇒ `tip_vol + a ≤ min(pipette max, tip max)`. The simulator checks this, but only at default values.
- (d) *Candidate:* `a ≥ pipette min volume`. This is an accuracy issue rather than a hard failure. Decide whether it is in scope.

**Example bug.** It passes the simulator at the default value of 100 and overflows for `vol > 120`:
```python
def add_parameters(parameters):
    parameters.add_int("Volume", "vol", default=100, minimum=10, maximum=300, unit="µL")

def run(protocol):
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", 2)   # C = 360 µL
    ...
    for _ in range(3):
        p300.transfer(protocol.params.vol, reservoir["A1"], plate["B1"])   # 3·vol > 360 when vol > 120
```
(This is a cut-down version of `experiments/2026-10-09-simulator-probe/overfill.py`.)

**Check.** Symbolic execution over the lowered step sequence produces path constraints and volume terms. For each guard `g`, ask Z3 whether `D ∧ path ∧ ¬g` is satisfiable. SAT gives a witness `p*`. UNSAT means proved. `unknown` or a timeout is reported as such.

---

## P2. Cross-contamination (taint tracking)

**Definition.** Each well's initial contents carry a label: a sample label for sample wells, a reagent label for reagent wells. A tip carries the union of labels from every well it has entered (aspirate, dispense, mix, `touch_tip`). A violation occurs when:
- (a) a tip carrying sample label `s` enters a well containing a different sample `s'`; or
- (b) a tip enters a **reagent source** while carrying any label that is not already in that source. This contaminates the stock.

`drop_tip` followed by `pick_up_tip` resets the label set. `return_tip` followed by reuse of the same tip is treated as the same tip, and counts as a violation if the tip is later used on another sample.

**Example bug.** One tip is used for every sample, so sample A1's liquid is carried back into the shared reservoir:
```python
p300.pick_up_tip()
for src, dst in zip(samples, dests):
    p300.aspirate(50, reservoir["A1"]); p300.dispense(50, src)   # tip now carries sample label
    p300.mix(3, 50, src)
    p300.aspirate(50, src); p300.dispense(50, dst)
p300.drop_tip()                                                   # next iteration re-enters reservoir: violation (b)
```
In the same way, `transfer(..., new_tip="never")` across samples triggers (a).

**Check.** Forward abstract interpretation over the lowered steps, with label sets as the abstract domain (a powerset lattice; join is set union at merge points). For parameter-dependent control flow, such as `if params.reuse_tips:`, either split the paths and use SMT to decide feasibility, or report on each path. **Labelling policy (open question Q3):** the default heuristic is that a well aspirated into two or more distinct destinations is a reagent source, and other source wells are samples. Users can override this. Dispensing from above with no contact is not modelled in v1 (we assume contact), so expect false positives on top-dispense protocols and measure them.

---

## P3. Resources (AST pass plus SMT for tip counts)

**Definition.**
- (a) Every `load_labware`, `load_module` and `load_instrument` uses a known load name or model and a valid location. The OT-2 has slots `1`–`11`, with `12` as the fixed trash. Confirmed from the deck definition, see RELATED_WORK.
- (b) No two items occupy one slot unless they are stacked through a module or adapter.
- (c) At most one pipette is loaded per mount.
- (d) Every well reference (`plate["H13"]`, `wells()[i]`, `columns()[j]`) exists in that labware's definition for all `p`.
- (e) **Tips:** for all `p ∈ D`, the number of tips picked per pipette is at most the tips available in its `tip_racks` minus the `starting_tip` offset. A multichannel pickup consumes a column.

**Example bug.** It works at the default of 48 samples and runs out of tips for more than 96:
```python
num_samples = get_values("num_samples")[0]          # fields.json label: "Number of samples (1-192)"
tips = [protocol.load_labware("opentrons_96_tiprack_300ul", 1)]
for i in range(num_samples):
    p300.pick_up_tip(); ...; p300.drop_tip()          # num_samples > 96 ⇒ OutOfTipsError
```
**Check.**
- (a)–(c) are a syntactic AST pass that resolves constants against the labware and deck definitions.
- (d) and (e) reduce to SMT, using the symbolic count of `pick_up_tip`s and symbolic indices against the rack and well bounds.
- The simulator catches (a)–(e) **at default values only**. Our contribution here is the ∀`p` part, not the checks themselves.

---

## P4. Timing (AST pass plus SMT)

**Definition.** Each incubation step has a required minimum duration `t_min`. These steps are `protocol.delay(seconds, minutes)`, `pause`, module holds such as temperature-module waits and the thermocycler `hold_time_seconds`/`hold_time_minutes` in profiles, and heater-shaker durations. For all `p ∈ D`, every such step's actual duration is at least its `t_min`.

**Example bug.**
```python
inc_min = get_values("incubation_minutes")[0]       # label: "Incubation time (0-30 min)"
protocol.delay(minutes=inc_min)                     # assay requires >= 5 min; inc_min = 0 is allowed
```
**Check.** This is easy once a specification exists. Duration terms come from the AST, and Z3 checks `D ⇒ duration ≥ t_min`. **The hard part is where `t_min` comes from**, because protocols do not state it. The options are a sidecar spec file, structured comments, or numbers extracted from the README. Without a spec, P4 can only flag durations that can be ≤ 0 or that depend on unconstrained parameters. This makes P4 the weakest property. It is scheduled last and may be descoped (Q5).

---

## Candidate properties, not yet committed
- Aspirating without a tip, or picking up a tip while already holding one. The simulator catches these at default values. They are cheap to add statically.
- Multichannel addressing past the last row or column of the labware.
- A module's temperature setpoint outside its supported range.
- Use-before-fill ordering, i.e. aspirating from a well before anything is dispensed into it. This is the "unknown initial volume" case of P1(b) and depends on Q2.
