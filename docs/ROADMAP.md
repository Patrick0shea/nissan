# Roadmap

Timeframes are rough and assume about 15 h/week. The weeks are relative to the start (2026-10-12). Re-plan once the deadline is known (Q8).

| # | Milestone | Done when | Weeks |
|---|---|---|---|
| **M0** ✅ | Project setup | Docs, skeleton, simulator probe (this commit) | 0 |
| **M1** ✅ | Concrete volume checker on one toy protocol | A front end handles `load_labware`, `load_instrument`, `aspirate`, `dispense`, `pick_up_tip`, `drop_tip` and constant `for` loops. P1(a–c) are checked with concrete values on `tests/fixtures/toy_overfill.py`, with capacities from the labware definitions. | 1–2 |
| **M2** ✅ | Symbolic parameters + Z3 | `add_parameters` domains become Z3 variables. P1 is checked ∀`p`, with witness output. `overfill.py` reports `vol > 120`. | 3–5 |
| **M3** | Corpus front end | `get_values` and `fields.json` ingestion. Domain inference (D-006). `transfer`, `distribute`, `consolidate` and `mix` are lowered, with semantics checked against 9.0.0 source. Parameter-dependent loop bounds are handled for `range(param)` patterns. `UNSUPPORTED` reporting. Coverage is measured on D1. | 6–9 |
| **M4** | P3 resources | Slot, labware, mount, well-index and tip-count checks, ∀`p`. | 10–11 |
| **M5** | P2 contamination | Taint domain, labelling heuristic, and `new_tip` modes. | 12–14 |
| **M6** | P4 timing | A spec format is decided, and delay and module-hold checks are implemented. May be descoped. | 15 |
| **M7** | Baselines + D3 | B1, B2 and B2+ harness (parameter override for 9.0.0). B3 prompt frozen. Mutation generator built. Recall measured. | 16–18 |
| **M8** | D2 + full evaluation | LLM protocols generated, all tools run, labelling with a second rater, results tables. | 19–22 |
| **M9** | Write-up | Paper or thesis chapter, plus an artefact package. | 23–26 |

## Progress notes
- **M1 (2026-10-09).** `otverify` checks P1(a–c) plus "liquid handling without a tip", with constant values, on `tests/fixtures/toy_overfill.py` and `toy_clean.py`. Coverage on D1 is **0/833 fully analysed**:
  - 767 protocols stop at `get_values`.
  - 12 stop at `load_module`.
  - 48 stop at other unmodelled statements or expressions.
  - 6 use labware that is not in the snapshot (custom labware).

  So `get_values` ingestion (M3) is the gate to any corpus result.
- **M2 (2026-10-09).** `add_parameters` is supported (D-013, D-014). The probe `overfill.py` reports P1a violated for `vol ∈ [121, 400]`, witness `vol=121`, and not reachable at the default of 100. The tip-capacity boundary agrees with the simulator on both sides (`experiments/2026-10-09-m2-tip-boundary/`). D1 coverage is unchanged (no `add_parameters` in the corpus), with 0 crashes over 833 files.
  - **Carried into M3:** symbolic control flow (branches, `range(param)`, indices). The options:
    - (a) Lazily enumerate an int parameter when it reaches a concrete-only position and its domain is small. This is exact, but it is enumeration rather than SMT, so report it separately.
    - (b) Fork paths on symbolic branches, with path conditions in the domain.
    - (c) Summarise loops.

    Decide (a) vs (b) at the start of M3.

## Risks
- **Loop summarisation (M3).** Corpus loops over `range(num_samples)` with list slicing need symbolic trip counts. The fallback is bounded unrolling up to the domain maximum when that maximum is small (≤ 96 or 384), with the bound reported.
- **Transfer semantics.** `transfer` and `distribute` have many keyword arguments. We support the common subset, mark the rest `UNSUPPORTED`, and measure how often each case occurs.
- **Legacy domains.** If label parsing yields usable ranges for only about 25% of numeric fields (621/2187 have a hint), D1 results depend on inferred domains. This pushes the weight of the evaluation onto D2 and D3.
- **The simulator already catches a lot at default values.** B2+ may close most of the gap. That is still a publishable, honest result if it is measured properly.

## Open questions (need a decision from the user)
- **Q1 / D-006.** How should numeric parameter domains for D1 be inferred?
- **Q2.** What is the semantics of unknown initial well volumes?
- **Q3.** What is the default sample/reagent labelling policy for P2?
- **Q4.** Who is the second rater for labelling?
- **Q5.** Is P4 (timing) in scope, and where does `t_min` come from?
- **Q6 / Q7.** Which LLMs and what budget for B3 and D2?
- **Q8.** What is the deadline and weekly time commitment, and how does this relate to the C++ analyser thesis?
- **Q9.** The corpus has no licence. Can we publish derived data, or only IDs plus scripts?
- **Q10.** Is the Flex in scope, or OT-2 only?
