# Decision log

This log is append-only. Never edit or delete an entry. To change a decision, add a new entry that says `Supersedes D-xxx`.

Format: **D-NNN, date: title.** Then the decision, the alternatives considered, and the reason.

---

**D-001, 2026-10-09: Target Opentrons Python API v2, OT-2 first.**
- *Decision:* Analyse `*.ot2.apiv2.py`-style protocols. The Flex is a later extension.
- *Alternatives:* XDL (Chemputer), PyLabRobot, Culsma.
- *Reason:* Opentrons protocols are plain Python, so we can reuse the user's AST skills. There is a large public corpus. All 833 public library protocols are OT-2.

**D-002, 2026-10-09: Purely static analysis over stdlib `ast`. Protocol code is never executed by the analyser.**
- *Alternatives:* Instrumented concrete execution (as VeriLab does), symbolic execution by running the code under a mocked API with Z3 values, or libcst.
- *Reason:* Executing the code gives one path per run. Mocked execution breaks on Python control flow over symbolic values (`if z3_expr:`). Static lowering keeps all paths and makes `UNSUPPORTED` explicit. `ast` is enough because we do not rewrite source, so libcst is not needed.

**D-003, 2026-10-09: Z3 (`z3-solver` on PyPI) as the SMT back end. Volumes are `Real`, counts are `Int`.**
- *Alternatives:* CVC5, or interval abstract interpretation only.
- *Reason:* Z3 has mature Python bindings and good documentation for a newcomer to SMT. Intervals alone cannot produce witnesses or handle relational constraints such as `3·vol ≤ 360`. Using `Real` avoids floating-point artefacts. µL rounding is not modelled.
- *Confirmed:* `z3-solver` 5.1.0.0 is the current release on PyPI and installs on Python 3.13.

**D-004, 2026-10-09: Baseline simulator pinned to `opentrons==9.0.0`, in a separate venv.**
- *Alternatives:* `opentrons` 10.0.0 (latest), or making opentrons a dependency of the analyser.
- *Reason:* I confirmed that 9.1.0, 9.1.2 and 10.0.0 raise "This protocol is designed for an OT-2 robot…" in `simulate.py`, and 9.0.0 does not. Keeping it separate means the analyser does not depend on a 100+ package tree.
- *Evidence:* `experiments/2026-10-09-simulator-probe/`.

**D-005, 2026-10-09: Corpus is `github.com/Opentrons/Protocols` `develop` @ `2f447c9` (2024-07-18). It is fetched by script, not vendored.**
- *Alternatives:* Vendoring it, or scraping protocols.opentrons.com.
- *Reason:* It is 191 MB and has no LICENSE file, so redistribution is unclear (Q9). Pinning the commit makes the evaluation reproducible. The website was not reachable from this environment, and the repo is its source anyway.

**D-006, 2026-10-09 (provisional, pending Q1): Parameter domains.**
- *Decision:*
  - For `add_parameters`, use `minimum`/`maximum`/`choices` as declared.
  - For legacy `fields.json`:
    - `dropDown` is the finite set of option values.
    - `int`/`float` uses the range parsed from the label when one is present (e.g. "(1-86)").
    - Otherwise use **default only**, mark the field `DOMAIN_UNKNOWN`, and report it.
  - An `add_int`/`add_float` with no min/max is unbounded. We flag it as a finding in its own right ("unbounded parameter").
- *Alternatives:* Default ± factor (arbitrary), manual annotation of every protocol (does not scale), or unbounded (gives mostly FP-domain).
- *Reason:* I confirmed that no `fields.json` numeric field has min or max (0/2187), and that 621 labels contain a range hint. Inventing ranges would inflate findings.

**D-007, 2026-10-09: Labware capacities and deck slots come from the `opentrons_shared_data` definitions, at the same version as the baseline (9.0.0).**
- *Alternatives:* A hand-written capacity table.
- *Reason:* This gives 138 definitions with per-well `totalLiquidVolume`, and the analyser and simulator agree on geometry. *How the analyser gets this data without depending on `opentrons` is still open.* The options are to depend on `opentrons-shared-data` alone, or to snapshot the JSON files into the repo (check the licence first). `opentrons-shared-data==9.0.0` is on PyPI. Its dependencies are jsonschema, numpy~=1.26.4, pydantic 2 and typing-extensions. PyPI shows no licence field for it.

**D-008, 2026-10-09: Python ≥ 3.11 for the analyser. Packaging uses setuptools, a `src/` layout and the package name `otverify`.**
- *Reason:* 3.11 is the oldest Python still in common use. `ast` covers `match` statements. The baseline venv uses 3.12. The package name is a placeholder and can be renamed cheaply now.

**D-009, 2026-10-09 (provisional, pending Q2): Unknown starting volumes are tracked as a lower bound starting at 0.**
- *Decision:* A well has a known volume only if the protocol declares it with `load_liquid`. Otherwise we track a lower bound starting at 0.
  - An overflow is reported only when the lower bound exceeds capacity. That is certain whatever the starting volume was.
  - An aspirate from a well with unknown contents is not checked. It is counted and reported as "not checked".
- *Alternatives:* Assume empty (every aspirate from an undeclared reservoir becomes a false positive), assume full (hides depletion bugs), or require annotations (cannot be applied to the corpus).
- *Reason:* No false positives come from the starting-volume assumption, and the gap stays visible. Revisit once Q2 is decided. P1(b) recall depends on it.

**D-010, 2026-10-09: Labware and pipette data are snapshotted into `src/otverify/data/opentrons_9_0_0.json`. This resolves the open part of D-007.**
- *Decision:* `scripts/snapshot_opentrons_data.py` runs in the baseline venv. It records all 138 v2 labware definitions (latest version of each; ordering plus `totalLiquidVolume`, stored per well when not uniform) and the max/min volume and channels of the 12 OT-2 pipettes, read from the live `opentrons` 9.0.0 API.
- *Alternatives:* Depend on `opentrons-shared-data` at runtime (pulls in numpy and pydantic).
- *Reason:* The analyser stays dependency-light. I confirmed that `opentrons-shared-data` 9.0.0 is Apache-2.0 (dist-info `License-Expression`), so its licence ships alongside the snapshot (`data/LICENSE-opentrons-shared-data`).

**D-011, 2026-10-09: The front end stops at the first construct it cannot model. Analysis covers that prefix only.**
- *Alternatives:* Skip the unknown statement and continue.
- *Reason:* A skipped `drop_tip`, `dispense` or assignment silently corrupts the later state, causing false positives and false negatives. Stopping keeps every reported finding sound with respect to the model. The stop point and reason are always reported (CLI exit code 3), and give coverage numbers for H4.

**D-012, 2026-10-09: P1 semantics confirmed against opentrons 9.0.0 (docstrings and simulator probes).**
- *Facts used by `volume.py`:*
  - `aspirate(None)` fills the tip to min(pipette max, tip capacity). A p300 with 200 µL filter tips aspirates 200, and 250 raises "Cannot aspirate more than pipette max volume".
  - `aspirate(0)` behaves like `None` below API 2.16 and does nothing from 2.16.
  - `dispense(None)` empties the tip.
  - `dispense(0)` empties the tip up to API 2.16 and does nothing from 2.17.
  - Dispensing more than the tip holds raises `InvalidDispenseVolumeError` from API 2.17. At 2.13 the simulator logs "Dispensing 80.0 uL" with 50 in the tip and raises no error. We report a *warning* and model "empties the tip", as the docstring says.
  - Aspirating without a tip raises `UnexpectedTipRemovalError`.
  - A p300 with 20 µL tips raises `KeyError: PipetteTipType.t20`, i.e. an incompatible tip. This is a future P3 check.
  - Multichannel (8): in a labware whose column has 8 wells, starting at row A, each channel uses one well. In a single-row labware (reservoir), all channels use the same well. Other layouts are reported as unsupported.

**D-013, 2026-10-09: Runtime parameters. Finite parameters are enumerated. Interval parameters are Z3 constants.**
- *Confirmed in opentrons 9.0.0 `protocols/parameters/validation.py`:*
  - Every parameter needs either `choices` or both `minimum` and `maximum`. The two are mutually exclusive, both bounds are inclusive, and the default is validated against the domain.
  - `add_str` also requires `choices`.
  - `add_bool` is choices `[True, False]`.
  - So every valid domain is either finite or a closed interval.
- *Decision:*
  - bool, str and numeric-with-choices parameters are enumerated, one lowered `Program` per combination with the defaults first. The cap is 256 combinations; beyond that, lowering stops with a reason.
  - int/float parameters with min/max become `z3.Int`/`z3.Real`, and arithmetic on them builds terms that flow into volumes. `/` is lowered as real division (z3 `/` on Ints is integer division).
  - A parameter-dependent value needed concretely stops lowering. That covers a branch condition, `range()` bound, index, slice, `//`, `%`, `int()` and a divisor.
  - Invalid definitions (no domain, default outside its domain, duplicate name, CSV parameters, `add_parameters` below API 2.18) stop lowering and name the Opentrons error they would raise.
- *Alternatives:* All parameters symbolic, which needs symbolic control flow everywhere (M3). Or all enumerated, which is sampling, not proof, for real intervals.
- *Reason:* Finite domains are typically small (labware choice, mount, flags) and used in control flow, so enumerating them is exact. Intervals are where an optimiser searches, and where SMT gives an all-values answer.

**D-014, 2026-10-09: How a symbolic check is reported.**
- *Decision:* Each guard is checked as `SAT(domain ∧ ¬guard)`. On SAT:
  - **Witness:** the minimum violating value of the first parameter, alphabetically, via `z3.Optimize`.
  - **Violating range:** per parameter, its min and max over the violating set, marking open bounds, e.g. `(120, 300]`.
  - **at_default:** whether the all-defaults assignment violates the guard. This is the H2 split between default-reachable and parameter-only bugs.
  - Message amounts are evaluated at the witness.
  - Z3 `unknown` (timeout 10 s) is reported as severity `unknown` (exit code 3). It is never reported as proved.
  - After a violation the state is clamped, as in M1, so independent bugs still surface.
- *Caveat:* A range is a per-parameter projection. With two or more parameters it over-approximates the violating region; e.g. `a ∈ [61, 300]` holds only together with `a + b > 360`.
- *CLI:* findings are grouped by line. The representative is a default-reachable finding if there is one, otherwise the smallest witness.

**D-015, 2026-10-09: Supersedes the last clause of D-006.** "An `add_int`/`add_float` with no min/max is unbounded" cannot happen. Opentrons rejects such a definition (D-013), so we report it as an invalid definition rather than an unbounded parameter.

**D-016, 2026-10-10: Legacy `get_values` / `fields.json` parameters (implements D-006).**
- *Confirmed:* the library build (`protolib/parse/parseOT2v2.py`) prepends a `get_values` that returns each field's default. For a dropDown that is `options[0].value`, otherwise `default`.
- *Decision:* `legacy.py` maps fields to `ParamSpec`s:
  - **dropDown:** the finite set of option values, default first.
  - **int/float with a range in the label:** an interval. Recognised forms are `a-b`, `a–b`, `a to b`, `between a and b`, and `up to N` / `max N`, the last two giving `[min(1, default), N]`. If the default lies outside the parsed range, the range is widened to include it and tagged `label+default`.
  - **Anything else:** the default value only, tagged `default` and printed as `DEFAULT ONLY` in every report.
- *Corpus (833 protocols):*
  - dropDown: 1944 fields.
  - int/float with a label range: 453 fields, 9 of them widened.
  - int/float at default only: 1731 fields.
  - str/textFile at default only: 363 fields.

  So most numeric fields are analysed at their default value, and D1 alone cannot carry the parameter-only claim (EVALUATION.md, threats).

**D-017, 2026-10-10: Enumerate int parameters lazily where a concrete value is needed.**
- *Decision:* An int interval parameter whose domain has at most 400 values may reach a concrete-only position: a loop bound, index, slice, undecided branch, divisor, `int()`, `//`, `%`, labware name or slot. Lowering then restarts with that parameter enumerated, as option (a) from M2.
- *Reporting:* Programs record `enumerated`. The CLI prints `(enumerated)` per parameter, so enumeration is never confused with SMT proof. Float parameters and larger domains stop lowering, as before.
- *Reason:* Sample counts (1–96, 1–384) drive loops in almost every corpus protocol. Enumerating them is exact. Path forking (b) is still needed for float-dependent branches.

**D-018, 2026-10-10: Semantics of mix, blow_out, air_gap and the trash, confirmed in opentrons 9.0.0.**
- *Source:*
  - `mix(n, v, loc)` is aspirate(v, loc), then (n−1) × [dispense(v), aspirate(v)], then dispense(v). It always does at least one cycle, and `None`/0 follow the aspirate/dispense API rules.
  - `blow_out(loc)` expels the tip contents into the location. With no location it uses the current well.
- *Probes* (`experiments/2026-10-09-simulator-probe` style, scratch only):
  - Air gaps count against tip capacity at both API 2.13 and 2.22: 280 µL + 30 µL air fails.
  - `mix` with liquid already in the tip can exceed capacity.
  - `fixed_trash["A1"]` is a valid location.
- *Modelling choice:* the tip tracks liquid and air separately. The air gap leaves first on dispense, because it was drawn in last, and air is never counted as liquid in a well. Opentrons' own liquid tracking (API ≤ 2.21) counts it, but physically it is air.

**D-019, 2026-10-10: After a symbolic violation, continue under an assumption. Supersedes the symbolic half of the clamp-and-continue rule (D-014).**
- *Decision:* A symbolic guard that fails is added as an assumption to the well or tip state it constrains. Assumptions flow with the liquid: a well filled from a tip inherits the tip's assumptions. Later checks on that state only consider parameter values for which the earlier guard held. Concrete violations are still clamped.
- *Why:* Clamping with `If(after > cap, cap, after)` nested one `If` per violating step. A 96-iteration loop with a symbolic volume took more than 100 s in Z3. With assumptions it takes 3 s, and every term stays linear.
- *Consequences:*
  - A finding's range is now the parameter region where it is the **first** failure on that state.
  - The CLI and the JSON output take the union of ranges per line, e.g. `[121, 180] ∪ [181, 400]` becomes `[121, 400]`.
  - Duplicate reports of an already-overflowing well disappear.
- *Supporting changes:*
  - Solver results are cached by (assumptions, guard, amounts) Z3 AST ids across a protocol's Programs. The terms are kept alive in the cache, so their ids cannot be reused by other terms.
  - Programs whose steps are identical (e.g. differing only in the pipette mount) are checked once.

**D-020, 2026-10-10: transfer / distribute / consolidate by porting `TransferPlan` and testing the port differentially.**
- *Decision:* `transfers.py` ports opentrons 9.0.0 `TransferPlan` and the `transfer()` option handling. This includes splitting against the pipette maximum but grouping against the tip, mix-before/after only when the tip is empty, falsy mix options falling back to `mix()` defaults, disposal blow-out, lazy errors, and multichannel first-row filtering.
- *Volume-dependent decisions:* concrete; or proved over the parameter domain with Z3; or enumerated (int) or stopped (float).
- *Validation:* `scripts/diff_transfers.py` runs random scenarios through the real `InstrumentContext.transfer/distribute/consolidate` in the simulator and through the port, and compares the command sequences. Result: **600 scenarios, 0 mismatches**. 421 plans were identical, 143 raised at the same point, and 36 were identical up to a command that fails when executed (an over-capacity aspirate the plan generates itself, which the checker reports).
- *Assumption:* the tip is empty when a transfer starts. That only matters for `new_tip="never"` after a manual aspirate.
- *Findings this enables:*
  - **CRASH (violation):** Opentrons raises mid-plan, e.g. source/destination lists that are not divisible, or a disposal volume ≥ the pipette maximum.
  - **API warnings:** keyword arguments Opentrons silently ignores (e.g. `disposal_vol=`, 15 corpus uses). Wells outside the first row silently skipped by a multichannel transfer. A distribute or consolidate that moves no liquid because the volume plus disposal exceeds the tip. Opentrons silently skips that last case, confirmed in `experiments/2026-10-10-silent-noop-distribute/`.
- *Not ported (reported as unsupported):* `gradient_function`, tuples of wells, and partial-nozzle configurations.

**D-021, 2026-10-10: Front-end semantics for real protocols.**
- *Branches:*
  - A parameter-dependent condition is first decided with Z3 over the domain and the path so far.
  - If it is still undecided and one branch only raises (input validation), the run continues on the other branch with the condition added to `Program.path`, so values the protocol rejects are not analysed.
  - Otherwise D-017 applies.
- *Exceptions and loops:*
  - `raise` ends that run. It is not an analysis gap: the protocol rejected its inputs.
  - In `try`, the body and `finally` run and the handlers are ignored, because we never model API exceptions such as OutOfTipsError. Tip exhaustion is P3/M4.
  - `while`, `break` and `continue` are supported, with an iteration cap.
- *Too many combinations:* beyond 1024 finite-parameter combinations, each finite parameter is varied one at a time from the defaults, and the gap is reported as INCOMPLETE.

**D-022, 2026-10-10: Real-run-only code and the robot environment.**
- `protocol.is_simulating()` returns **True**, so we follow the branch `opentrons_simulate` takes. In the corpus, real-run-only branches hold tip-state files and rail-light blinking. Supersedes the M2 choice of False.
- **Files:** `os.path.isfile/exists` return False (a fresh robot with no saved state). Path helpers work on concrete strings, and `makedirs` and `json.dump` are no-ops.
- **Inert objects:** threads, Points/Locations, file handles, module status and well geometry are opaque values. They absorb arithmetic and cannot reach a volume.
- **Custom labware:** read from the protocol folder's `labware/*.json`, as the library build does. 522 corpus folders ship such definitions.
- **User classes:** plain classes with methods and instance attributes are supported. Custom attributes set on wells, labware and pipettes (e.g. `well.liq_vol`) are tracked.

**D-023, 2026-10-10: Wells with unknown contents also get an upper bound. Refines D-009.**
- *Decision:*
  - An unknown well's contents are bounded above by min(capacity, upper). `upper` starts at the capacity, goes down with each aspirate and up with each dispense.
  - An aspirate asking for more than that bound is a **certain** P1b violation ("which can hold at most …").
  - After a concrete violation, the tip takes in only what the well could have held, so there is no cascading overflow downstream.
  - Below the bound, aspirates are still assumed to get the volume they ask for. This is the protocol's intent and the D-009 reading: "at least X (even if it started empty)" means assuming the sources supplied what was asked.
- *Found by:* triage of corpus finding `05f673`. `mix(3, 1000)` in a 360 µL well was being reported as a 1000 µL *overflow* of that well. The real bug is that no 360 µL well can supply 1000 µL.
- *Effect on the probe fixture:* `overfill.py` aspirates 3 × vol from A1 of the same 360 µL plate. Its first certain failure is now that overdraw, for vol ∈ [121, 400]. The B1 overflow is no longer certain, because A1 could not have supplied the liquid.

**D-024, 2026-10-10: Model well geometry, pipette state and module state instead of making them opaque.**
- *What exposed it:* in the M3 corpus run, comparisons with opaque values crashed (45 protocols) or, worse, silently evaluated (`_Opaque == "multi"` is False). The fix makes such comparisons stop with a reason. That dropped "fully analysed" from 364 to an honest 325, and these values are now modelled for real.
- *Labware definition versions:*
  - Capacities and well ordering are identical across every version of all 55 multi-version labware.
  - Geometry differs between versions.
  - API 2.13 and 2.20 both load version 1 (`uri …/1`), so the snapshot takes geometry from version 1.
- *Geometry, confirmed in the simulator:* `Well.width` is the definition's **yDimension** and `Well.length` its **xDimension** (`nest_12_reservoir_15ml` A1: width 71.2, length 8.2). `diameter` is None for rectangular wells, and width and length are None for circular ones.
- *Pipette state:*
  - `pip.type` is "single" or "multi" by channel count (9.0.0 source).
  - `hw_pipette` returns a dict with `has_tip`, `channels`, `max_volume`, `min_volume` and `name`.
  - `current_volume` is the plunger volume (liquid + air), tracked in the front end with Opentrons' zero-volume rules.
- *Module state:* the magnetic module's `status` is engaged/disengaged (starts disengaged; 9.0.0 source), and the thermocycler's `lid_position` follows open/close calls. Other module state stays opaque and stops on a comparison.
- *Fixed trash:* below API 2.16 the fixed trash is a labware in slot 12, so `ctx.loaded_labwares[12]` is the trash. That accounted for 49 stops.
