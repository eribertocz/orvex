# 0023 — Extension path for Python models, ML predictions and Python visuals (after v1.0.0)

**Status:** Accepted (2026-10-26) — design direction only; implementation is **not** part of v1.0.0.

## Context
Power BI offers Python scripts and visuals (pandas-based). The Owner wants Sextant to support, after the MVP, machine-learning predictions and Python analysis compatible with Polars, Apache Arrow, DuckDB, XGBoost, LightGBM, scikit-learn, Plotly and Matplotlib. Contracts designed for v1.0.0 must not block this.

## Decision
1. **Python models in `transform`.** A model may later be a `.py` file (beside the same YAML sidecar) exposing one function that receives Arrow tables or DuckDB relations of its declared inputs and returns an Arrow table. It gets the same materializations, incremental strategies, tests, write–audit–publish, run records and lineage as SQL models. Typical use: train or apply XGBoost, LightGBM or scikit-learn models and write predictions as a table.
2. **Predictions are ordinary datasets.** Prediction tables enter the semantic model like any other dataset (with grain, evidence and policies), so dashboards, the matrix, receipts and security apply unchanged. A prediction metric carries metadata about the model version that produced it.
3. **Isolation.** Python code never runs inside the API process. It runs in an isolated worker (separate process or container) with CPU, memory and time limits, no network by default, and a per-project locked environment (`requirements` lock file). Data enters and leaves only as Arrow.
4. **Python visuals.** A visual may later run Python (Plotly or Matplotlib) in the same isolated worker. It receives data **only** from semantic queries executed with the viewer's security context, so row and column policies always apply. Output is Plotly figure JSON (validated, no executable code) or a static SVG/PNG.
5. **Contract reservations in v1.0.0:** `model-config` reserves the model kind `python`; `visual-spec` reserves the visual type `python`. They are rejected with `UNSUPPORTED_MODEL_KIND` and `UNSUPPORTED_VISUAL_TYPE` respectively until implemented.

## Alternatives considered
- Python inside the server process (Power BI Desktop style): unacceptable security and stability risk.
- pandas as the interchange: Arrow is the standard columnar interchange and is zero-copy with Polars and DuckDB.

## Consequences
After v1.0.0, a new phase and an ADR define the worker runtime, resource limits and supported libraries (always latest stable, ADR-0022). Nothing in v1.0.0 has to be redesigned.
