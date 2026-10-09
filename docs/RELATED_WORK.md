# Related work

Status key:
- **VERIFIED**: I read the primary source (repo, package or code) myself.
- **PARTIAL**: I read only search-result summaries. This environment cannot reach arxiv.org, biorxiv.org or docs.opentrons.com, so I could not read the primary source.
- **UNVERIFIED**: the claim is from memory or the user's brief and still needs a source.

Checked 2026-10-09.

## Verifiers and checkers for lab protocols

| Work | What it does | How we differ | Status |
|---|---|---|---|
| **CLAIRify**: Skreta et al., "Errors are Useful Prompts", arXiv 2303.14100 (2023) | An LLM generates XDL. A verifier checks syntactic validity against the XDL rules and feeds errors back to the LLM. | Only well-formedness. We prove semantic safety properties for all parameter values. | PARTIAL |
| **AEGIS**: arXiv 2607.15620 (Jul 2026) | OT-2. Layer 1 checks the protocol's Python source before the run using an assay rule database plus an LLM. Layer 2 is a camera runtime monitor. Benchmark: 24 protocols (11 correct, 13 with injected bugs, e.g. a shared tip between template and no-template control). Reports adjusted F1 0.97. Claims MIT release of code and data. | **Closest prior work.** Its checks are rule-matching plus an LLM, with no proofs and no reasoning over parameter ranges. Its 24-protocol benchmark is a candidate eval set (see EVALUATION.md). | PARTIAL |
| **VeriLab**: github.com/wwpatel/verilab | Deterministic checks for the Flex: cumulative well volume against labware capacity, source depletion, step order, and tip reuse across sources. It runs the protocol Python with instrumented `aspirate`/`drop_tip`. MIT, 1 star, 47 commits. | It checks by concrete execution of one run. We use static and symbolic analysis over all parameter values. It is a potential baseline, but it targets the Flex only. | VERIFIED (README) |
| **Dual-agent NL→protocol translation**: arXiv 2606.20120 | A second LLM agent validates the translated protocols. | An LLM validator, so no proof. | PARTIAL |
| **LLM → OT-2 scripts with simulator feedback**: arXiv 2304.10267 (2023) | GPT-4 writes OT-2 Python. `opentrons_simulate` errors are fed back, for up to 5 iterations. | It uses the simulator as its oracle, so it inherits the simulator's single-run blind spots, which is our gap. | PARTIAL |
| **ABC-Bench**: arXiv 2606.11150 | A biosecurity benchmark. LLMs write Opentrons scripts (Gibson assembly), and one ran on a real Flex. | A possible source of LLM-generated protocols, not a checker. | PARTIAL |
| **PRISM**: arXiv 2601.05356 | "Protocol Refinement through Intelligent Simulation Modeling". It turned up in a search, and I could not read the abstract. | Unknown. Read before claiming novelty. | UNVERIFIED |

## Protocol languages

| Work | What it does | How we differ | Status |
|---|---|---|---|
| **XDL / Chemputer** (Cronin group) | A hardware-independent chemical description language. | Organic synthesis, not liquid handling. Its checks are schema-level. We analyse the vendor's Python directly. | UNVERIFIED (from memory) |
| **Culsma**: github.com/culsma/culsma, bioRxiv 10.64898/2026.05.07.723509 (May 2026) | A formal language and execution kernel for lab protocols (`.culs`). It has a "validate, typecheck" pipeline and is on PyPI (`culsma`), Apache-2.0. The README does not say whether it proves properties or targets Opentrons. | It requires rewriting protocols in a new language. We analyse existing Opentrons code. Its exact guarantees are unconfirmed, so read the preprint. | PARTIAL |
| **BioScript**: Ott et al., OOPSLA 2018 / CACM 2021 | A DSL for digital-microfluidic biochips. Its type system flags unsafe chemical mixes and ensures each fluid is used at most once. | It is the closest PL-theory precedent (affine use of fluids resembles our tip and contamination tracking). But it targets biochips with a new language, not robots running existing Python. | PARTIAL |
| **LAP format**: PMC7615385 | A script-based standard for lab-automation protocols. | Standardisation, not verification. | PARTIAL |
| **PyLabRobot**: github.com/PyLabRobot/pylabrobot | A hardware-agnostic Python SDK (it has an OT-2 backend). Third-party summaries say it has volume and tip tracking. | Runtime tracking for one concrete run. Unconfirmed whether it raises on overflow. | PARTIAL |

## Vendor tooling (Opentrons): confirmed facts

These were confirmed against package source and the corpus. Evidence is in `experiments/2026-10-09-simulator-probe/`.

- **API.** `opentrons` 10.0.0 (PyPI, 2026-09-30) has `MAX_SUPPORTED_VERSION` 2.31. 9.0.0 has 2.29. `ProtocolContext.params` is `@requires_version(2, 18)`. Runtime parameters are declared in `add_parameters(parameters)` via `add_int/add_float(display_name, variable_name, default, minimum=None, maximum=None, choices=None, description=None, unit=None)`, `add_bool` and `add_str(choices=...)`, plus `add_csv_file`. `minimum` and `maximum` are optional.
- **`opentrons_simulate` dropped OT-2 in 9.1.0.** Versions 9.1.0, 9.1.2 and 10.0.0 raise "This protocol is designed for an OT-2 robot…". 9.0.0 still simulates OT-2 protocols, so it is our pinned baseline.
- **The simulator only runs default parameter values.** `simulate.py` (9.0.0) passes `run_time_parameters_with_overrides=None`, and the CLI has no flag to set parameter values.
- **What the simulator catches at default values:** `OutOfTipsError`, aspirating more than tip capacity (`InvalidAspirateVolumeError`), and an invalid deck slot.
- **What it misses:** aspirating from an empty, unloaded well, and dispensing 3×200 µL into a 360 µL well. Both raise no error.
- **Liquid tracking** (`define_liquid`, `Well.load_liquid`, `current_liquid_volume`) exists. A quick probe at API 2.22 hit `LiquidHeightUnknownError` instead of an overflow check. It is unclear whether the simulator enforces capacity when liquids are loaded. **Open item: investigate before the baseline is final.**
- **Labware data.** `opentrons_shared_data` ships 138 v2 labware definitions, with per-well `totalLiquidVolume` (e.g. `corning_96_wellplate_360ul_flat` A1 = 360). The OT-2 deck definition (`deck/definitions/5/ot2_standard.json`) has slots `1`–`12` plus `fixedTrash`.

## Public protocol corpus: confirmed

- `github.com/Opentrons/Protocols`. Default branch `develop`, cloned at `2f447c9` (last commit 2024-07-18). Not archived, 63 stars. **No LICENSE file is present.** 191 MB.
- 833 protocol directories, each with `*.ot2.apiv2.py`. There are 0 OT-3/Flex files and 0 that use `add_parameters`. API levels range from 2.0 to 2.13 (most often 2.11 with 240 files, then 2.9 with 122 and 2.13 with 114).
- Parameters use the legacy library mechanism. 804 directories have a `fields.json` (types: dropDown 1944, int 1154, float 1033, textFile 216, str/string 144). 790 files call `get_values(...)`, which the library build injects (`protolib/parse/parseOT2v2.py: prepend_get_values_fn`); only 3 files define it inline. To run them, inject `get_values` built from `fields.json` defaults.
- **Numeric fields have no min or max.** All 2187 int/float fields have only `type/label/name/default`. 621 labels contain a range hint, e.g. "Number of Samples (1-86)". Parameter domains must therefore be inferred. See DECISIONS D-006.
- A protocol from this corpus (`21u968`, API 2.9) simulates cleanly under 9.0.0.

## To read next
- The full texts of AEGIS, PRISM and Culsma. These determine how we state novelty.
- Prior work on static analysis of SDL or robot code, e.g. model checking of lab workflows. I have not searched for it yet.
- The SDL safety and BO literature that motivates parameter-dependent failures. We need a citation showing optimisers propose boundary values.
