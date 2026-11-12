"""A security conformance fixture as data (#441): its compiled model, its policies, the managed-
lake tables its policies read, and every table's rows, loaded into a temporary managed lake.

The fixtures live in `conformance/security/fixtures/<name>/`: `model.ossie.json`, `security.yaml`,
`tables.toml` (each table's columns and CSV file) and, for an engine other than the managed
lake, `lake.toml`. A CSV is read as `bi_platform_adapter.read_csv` reads it, with one addition
the fixtures use: the text `NULL` is a null. Values stay text, cast to their column types by the
lake. The case runner (`tests/conformance/test_row_policy_cases.py`, and #447 after it) builds
on this module; it never copies the reading.
"""

from __future__ import annotations

import csv
import io
import tempfile
import tomllib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce

SECURITY = Path(__file__).resolve().parents[1] / "security"
# Groups the fixtures' policies name beside those their cases' callers carry.
GROUPS = frozenset({"collections", "engineering", "management", "operations"})

Rows = list[list[str | None]]
Catalog = dict[str, dict[str, SourceColumn]]


@dataclass(frozen=True)
class Fixture:
    name: str
    folder: Path
    catalog: Catalog  # every table, `schema.table` or unqualified
    rows: dict[str, Rows]  # every table's rows, in file order, `None` for `NULL`
    managed: dict[str, list[str]]  # the `security.*` tables the policies may read
    compiled: CompiledModel

    def policies(self, groups: Iterable[str] = ()) -> PolicyModel:
        text = (self.folder / "security.yaml").read_text("utf-8")
        return read_policies(
            text, self.compiled, groups=GROUPS | set(groups), managed_tables=self.managed
        )

    def lake_settings(self) -> dict[str, Any]:
        """The fixture's `lake.toml`: the existing DuckLake an engine other than the managed
        lake reads its datasets from."""
        return tomllib.loads((self.folder / "lake.toml").read_text("utf-8"))


def fixture_catalog(folder: Path) -> tuple[Catalog, dict[str, list[str]], dict[str, Rows]]:
    """The fixture's tables: their columns, the managed-lake tables, and their rows. A CSV whose
    header is not its columns, or with a row of another width, is refused."""
    tables = tomllib.loads((folder / "tables.toml").read_text("utf-8"))["tables"]
    catalog: Catalog = {
        name: {c: SourceColumn(t, nullable) for c, t, nullable in table["columns"]}
        for name, table in tables.items()
    }
    rows: dict[str, Rows] = {}
    for name, table in tables.items():
        found = list(csv.reader(io.StringIO((folder / table["file"]).read_text("utf-8"))))
        if found[0] != [c for c, _, _ in table["columns"]]:
            raise ValueError(f"{name}: the CSV header is not the table's columns")
        if any(len(row) != len(found[0]) for row in found):
            raise ValueError(f"{name}: a row is not as wide as the header")
        rows[name] = [[None if v == "NULL" else v for v in row] for row in found[1:]]
    managed = {
        n: [c for c, _, _ in t["columns"]] for n, t in tables.items() if n.startswith("security.")
    }
    return catalog, managed, rows


@cache
def fixture(name: str, vendor: str) -> Fixture:
    folder = SECURITY / "fixtures" / name
    catalog, managed, rows = fixture_catalog(folder)
    text = (folder / "model.ossie.json").read_text("utf-8").replace("{{vendor}}", vendor)
    compiled = compile_model(
        read(text.encode(), "model.ossie.json", ce.VENDORS), catalog, ce.VENDORS
    )
    return Fixture(name, folder, catalog, rows, managed, compiled)


@contextmanager
def managed_lake(found: Fixture, tables: Iterable[str] | None = None) -> Iterator[Lake]:
    """A temporary managed lake holding `tables` (every table of the fixture by default), with
    their rows; the empty field stands for a null, as `read_csv` reads it."""
    chosen = sorted(found.catalog if tables is None else tables)
    with tempfile.TemporaryDirectory() as folder:
        by_schema: dict[str | None, list[Path]] = {}
        for name in chosen:
            schema, _, table = name.rpartition(".")
            place = Path(folder) / (schema or "_")
            place.mkdir(exist_ok=True)
            path = place / f"{table}.csv"
            with path.open("w", encoding="utf-8", newline="") as file:
                written = csv.writer(file, lineterminator="\n")
                written.writerow(found.catalog[name])
                written.writerows(["" if v is None else v for v in row] for row in found.rows[name])
            by_schema.setdefault(schema or None, []).append(path)
        schemas = sorted(by_schema, key=lambda s: s or "")  # unqualified tables first
        with ce.case_lake(by_schema[schemas[0]], found.catalog, schemas[0]) as lake:
            for other in schemas[1:]:
                ce.load_tables(lake, by_schema[other], found.catalog, other)
            yield lake
