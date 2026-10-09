# Evaluation plan

Everything below is fixed **before** running the full evaluation: metrics, the labelling protocol, baselines and time budgets. Any later change gets a DECISIONS.md entry.

## Datasets

| ID | Source | Size | Parameters | Use |
|---|---|---|---|---|
| **D1** | `Opentrons/Protocols`, `develop` @ `2f447c9` | 833 OT-2 protocols, 804 with `fields.json` | Legacy `get_values`. Numeric domains must be inferred (D-006). | Real-world precision, H1, H4 |
| **D2** | LLM-generated protocols | Target ~200 | `add_parameters` with min/max requested in the prompt | Precision and unique detections on LLM code |
| **D3** | Seeded bugs: mutants of a clean subset of D1 and D2 | Target ~300 mutants | As in the parent protocol | **Recall**, since D1 and D2 have no ground-truth recall |
| **D4** (optional) | AEGIS benchmark (24 protocols, 13 buggy), if its release is obtainable | 24 | Unknown | External comparison against the closest prior work |

- **D1.** Fetch with `scripts/fetch_corpus.sh`, which pins the commit. We do not vendor the corpus, because it has no licence file (Q9). Exclude directories that have `.ignore`. Each protocol is run as a single file with `get_values` injected from `fields.json`, as the library build does.
- **D2.** Prompts are built from D1 README descriptions, so the tasks are realistic and paired with a human-written reference. We use 2 or more models, a fixed temperature, and record the model ID and date. Every output is kept, including outputs that fail `opentrons_simulate` at default values, which are reported as a separate stratum.
- **D3 mutation operators:**
  - Scale a volume constant by 1.5–3×.
  - Widen a parameter's maximum.
  - Remove a `drop_tip`/`pick_up_tip` pair, or switch `new_tip` to `"never"`.
  - Shrink `tip_racks`.
  - Change the labware to a smaller-capacity variant.
  - Shorten or parameterise a `delay`.

  Each mutant is tagged with its property and with whether the bug is **default-reachable** (visible at default parameter values) or **parameter-only**. This tag is what lets us test H2 honestly.

## Baselines
- **B1. `opentrons_simulate`** (opentrons==9.0.0, the last release that simulates OT-2) at default parameter values. This is what users actually run.
- **B2. Sampled simulation.** Simulate at *k* parameter assignments: domain bounds, the default, and uniform random samples. B2 gets the same wall-clock budget per protocol as our verifier. Running B2 needs a harness that overrides parameter values, because the CLI cannot (see RELATED_WORK). This is the **strong** baseline, and it is roughly what a BO loop "tests" by accident. If B2 matches us, H2 fails, and we say so.
- **B2+.** B1 and B2 with our own concrete volume, tip and taint tracker attached. This separates "the simulator doesn't check well capacity" from "single-point testing misses parameter-dependent bugs". These are two different claims, and the second is ours.
- **B3. LLM validator.** A fixed prompt asks for a list of bugs with line numbers, using a fixed model, temperature 0 and 3 runs, with majority voting. We record cost.
- **B4** (if obtainable): AEGIS Layer 1, or VeriLab (Flex only, so it applies only to Flex protocols in D2).

## Metrics
- **Per finding:** TP or FP (labelled as below).
  - **Precision** = TP / (TP + FP), per property and per dataset.
- **Recall:** on D3 only, per property, split into default-reachable and parameter-only bugs.
- **Unique detections:** a Venn/UpSet chart of true bugs found by us, B1, B2, B2+ and B3. **This is the headline result for H2.**
- **Coverage (H4):** the percentage of protocols fully analysed, and the percentage per `UNSUPPORTED` construct. This tells us which Python features to support next.
- **Cost:** runtime per protocol (median and p95), Z3 timeouts, and LLM cost for B3.

## Labelling true and false positives
A finding is a **true positive** iff there is a parameter assignment `p*` in the *declared or inferred* domain such that executing the protocol at `p*` violates the property as defined in PROPERTIES.md.

1. **Replay.** Run the witness `p*` through B2+, i.e. concrete simulation plus our tracker at `p*`.
   - If the violation reproduces, the finding is *TP-confirmed*.
   - If it does not reproduce, investigate. The cause is either analyser imprecision (FP) or a tracker bug.
2. **Domain check.** Is `p*` actually allowed? For D1 this means checking the `fields.json` label and README. A violation only outside the documented range is **FP-domain**, and we record it separately, because it measures how bad our domain inference is.
3. **Assumption check.** If the finding depends on a modelling assumption (unknown initial volume, tip contact on dispense, sample/reagent labelling), label it **FP-assumption** or TP, and record which assumption was involved.
4. **Second rater.** A second person independently labels a random sample of at least 20% of the findings (Q4). We report Cohen's κ. Disagreements are resolved by discussion and logged.

All labels go in `eval/labels.csv` with these columns: finding ID, protocol, property, witness, label, sub-label, rationale, rater.

FP sub-labels: **imprecision**, **domain**, **assumption** and **spec** (the property definition does not fit the protocol's intent, e.g. a deliberate top-dispense with a reused tip).

## Threats to validity, tracked up front
- D1 is old: API ≤ 2.13 and no `add_parameters`. Its parameter domains are inferred, not declared, so D1 alone cannot test the "optimiser-chosen parameters" story. D2 and D3 carry that story.
- The mutation operators are ours, so recall on D3 is biased toward what we model. We mitigate this by defining the operators before implementation and logging them in DECISIONS.
- The simulator version is pinned at 9.0.0, and later behaviour may differ. Users on 9.1 or later cannot simulate OT-2 protocols at all.
