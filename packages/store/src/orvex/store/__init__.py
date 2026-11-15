"""Store and read analytical data in DuckLake through DuckDB (contract `store` v1).

Its one public face is `orvex.store.api.Store`, the implementation of the interface contract
`orvex.contracts.store.interface.STORE` (#486); everything else is internal. Importing the
package itself needs no dependency.
"""

from importlib.metadata import version

__version__ = version("orvex-store")
