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
