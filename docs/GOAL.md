# Goal

## Problem
Self-driving labs (SDLs) run protocols whose parameters (volumes, sample counts, times) are chosen at runtime by an optimiser, usually Bayesian optimisation. LLMs increasingly write these protocols. Today's checks cover one point in parameter space (the simulator), only well-formedness (syntax verifiers), or nothing provable (LLM validators). An optimiser that explores the parameter space will eventually pick the value that overflows a well.

## Research question
Can static analysis plus an SMT solver catch bugs in real Opentrons protocols that `opentrons_simulate` and LLM-based validation miss, especially bugs that appear only for some of the parameter values an optimiser may choose?

## Hypotheses
- **H1 (existence).** Real public protocols and LLM-generated protocols contain property violations that are reachable only for non-default parameter values.
- **H2 (detection).** The verifier finds violations that `opentrons_simulate` at default values misses. It also finds some that random-sampled simulation misses at an equal time budget.
- **H3 (precision).** On the public corpus the verifier's precision is high enough to be usable. Target: ≥ 70% of reported findings are true bugs. This target is provisional and will be fixed before evaluation (see EVALUATION.md).
- **H4 (scope).** Most corpus protocols fall inside the analysable subset (no `UNSUPPORTED` construct on the property path). Target ≥ 60%, also provisional.

H2 is the central claim. If sampling-based simulation matches us, we report that.

## Scope
- Opentrons Python Protocol API v2. The OT-2 comes first, because the public corpus is all OT-2 (see DECISIONS D-005). The Flex comes second.
- The properties are in PROPERTIES.md: volume safety, cross-contamination, resources, and timing.
- Parameter sources are `add_parameters` runtime parameters (API ≥ 2.18) and the legacy library `get_values` / `fields.json` mechanism.
- Static analysis of a single protocol file, with labware geometry taken from the official labware definitions.

## Non-goals
- Physical correctness beyond the model: liquid-class behaviour, evaporation, dead volume (unless declared), meniscus tracking, collisions, motion planning.
- Biological or chemical correctness of the protocol: whether this is the right assay.
- Full Python semantics. We support the subset the corpus uses, and report everything else as `UNSUPPORTED`.
- Verifying the optimiser or the SDL orchestration layer.
- Other vendors' languages (XDL, PyLabRobot, Culsma). We may mention them as future work only.
- A production tool. This is a research prototype.

## Success looks like
1. A tool that, given a protocol and parameter domains, outputs for each property either *proved*, *violated* with a concrete witness, or *unknown/unsupported* with a reason.
2. An evaluation on (a) the public library, (b) LLM-generated protocols and (c) a seeded-bug set, against `opentrons_simulate` (default values and sampled values) and an LLM validator. It reports precision, recall on seeded bugs, and the set of bugs found only by us.
3. At least a handful of confirmed, explained, parameter-dependent real bugs. Or, if there are none, a clear negative result with an explanation of why.
