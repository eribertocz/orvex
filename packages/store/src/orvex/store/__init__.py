"""Store and read analytical data in DuckLake through DuckDB (contract `store` v1).

The API lives in `orvex.store.lake` (`open_lake`, `Lake`, `Limits`, `Transaction`) and
`orvex.store.errors` (`StoreError`); importing the package itself needs no dependency.
"""

from importlib.metadata import version

__version__ = version("orvex-store")
