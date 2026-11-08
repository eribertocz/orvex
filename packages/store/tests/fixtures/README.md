# Store test data

`decimal_logical_only.parquet` is a synthetic file (#253) with three columns:
- `w`: `DECIMAL(50,2)`, value 1.10;
- `n`: `DECIMAL(10,2)`, value 1.10;
- `k`: `BIGINT`, value 1.

It was written by pyarrow 26.0.0 with `store_schema=False`. Its Parquet footer was then edited to remove the older *converted* decimal annotation (`converted_type`, `scale`, `precision`), so only the newer *logical* annotation remains. pyarrow cannot write such a file directly, but other writers can. No real data.

`lake_interface.json` is the record of the names importable from `orvex.store.lake` (#246): the public group with each name's shape, and the internal group, which may only shrink (`test_lake_interface.py`). It was written from `main` (the public group) and the split (the internal group).
