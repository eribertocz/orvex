"""The project's semantic model, compiled against the lake at one snapshot (P2-26a, #358).

`load_semantic` reads the one Ossie file under `semantic/` and `security.yaml`, and compiles them
against the lake's tables and columns as of exactly the snapshot given (`store/v1`
`TablesRequest`): no metadata of any other version is read, the newest one included. The model
and its policies are one unit: any problem in any file returns no model, never a partial one.

It compiles; it never authorizes. It takes no caller identity and returns no permission: who may
use the model is each caller's own decision (the CLI's local builder, the HTTP API's roles
checked through `access`), and one never carries over to the other.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from orvex.contracts.store.v1 import TablesRequest, TablesResult
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.engines import (
    ALL_MANAGED,
    ExternalAdapter,
    Routing,
    dataset_connections,
    route_model,
)
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import sql
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.server.engine_catalog import engine_catalog
from orvex.server.problems import Problem
from orvex.server.regular_file import read_regular_file
from orvex.store.errors import StoreError
from orvex.store.lake import Lake

FOLDER = "semantic"
POLICIES = "security.yaml"
SUFFIXES = (".ossie.yaml", ".ossie.json")
Audience = Literal["builder", "other"]


@dataclass(frozen=True, slots=True)
class SemanticModel:
    compiled: CompiledModel
    policies: PolicyModel | None  # none without `security.yaml`
    snapshot: int  # the one snapshot its tables and columns were read at
    file: str  # project-relative
    routing: Routing = ALL_MANAGED  # every dataset's engine (P2-29)
    # The adapter of each existing lake the model was compiled on, pinned at the snapshot its
    # tables were read at, so a question reads exactly what it was compiled against (#399).
    adapters: Mapping[str, ExternalAdapter] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Loaded:
    model: SemanticModel | None  # none: no semantic model, or problems
    problems: tuple[Problem, ...] = ()


@dataclass(frozen=True, slots=True)
class ExternalTables:
    """A Direct Query engine's tables: an existing lake's tables and views at its served
    snapshot (P2-30 PR 3), or a ClickHouse database's tables now (P2-31 PR 4): what the datasets
    on that connection are compiled against. Given only when its engine is enabled."""

    tables: TablesResult
    views: frozenset[str]  # `schema.view`, lower-cased: no dataset may read one
    adapter: ExternalAdapter | None = None  # its reads, pinned at the snapshot of `tables`
    # `schema.table`, lower-cased: a table no dataset may read, as the error saying why (a
    # ClickHouse column with no exact type, `UNSUPPORTED_SOURCE_TYPE`).
    refused: Mapping[str, StoreError] = dataclasses.field(default_factory=dict)


def model_files(folder: Path) -> list[Path]:
    """The Ossie files directly under `semantic/`, in name order."""
    root = folder / FOLDER
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_file() and p.name.endswith(SUFFIXES))


def _problems(error: SemanticError, file: str, audience: Audience) -> list[Problem]:
    """Each problem of a semantic error, with its code, file and pointer, in the caller's
    audience: what a builder may see, or the generic message for anyone else."""
    listed = error.details.get("problems")
    if audience == "builder" and isinstance(listed, list) and listed:
        found = [
            Problem(
                error.code,
                file,
                p.get("line") if isinstance(p.get("line"), int) else None,
                f"{p.get('rule', 'invalid')} at {p.get('pointer') or '/'}",
            )
            for p in listed
            if isinstance(p, dict)
        ]
        omitted = error.details.get("problems_omitted")
        if omitted not in (None, "0", 0):
            found.append(Problem(error.code, file, None, f"{omitted} more problems"))
        return found
    text = str(error.to_message("en", audience).model_dump()["message"])
    return [Problem(error.code, file, None, text)]


def _catalog(
    document: Mapping[str, object],
    tables: TablesResult,
    file: str,
    only: Callable[[str], bool] = lambda _: True,
    views: frozenset[str] = frozenset(),
    refused: Mapping[str, StoreError] | None = None,
) -> tuple[dict[str, dict[str, SourceColumn]], list[Problem]]:
    """Each dataset's source table as of the snapshot, by its `source` text: `table` (schema
    `main`) or `schema.table`, matched as DuckDB matches a name (case aside). A source with no
    such table is a problem naming the dataset, never a guess. Only the datasets `only` keeps
    (by normalized name) are read here; a source that is one of `views` is refused: an existing
    lake's datasets read its tables only (ADR-0027 §3); one in `refused` is its error."""
    known = {(t.schema_.casefold(), t.table.casefold()): t for t in tables.tables}
    datasets = document.get("datasets", ())
    catalog: dict[str, dict[str, SourceColumn]] = {}
    problems: list[Problem] = []
    for at, dataset in enumerate(datasets if isinstance(datasets, Sequence) else ()):
        if not isinstance(dataset, Mapping) or not only(sql.normalize(str(dataset.get("name")))):
            continue
        source = str(dataset.get("source", dataset.get("name", "")))
        schema, _, name = source.rpartition(".")
        if f"{schema or 'main'}.{name}".casefold() in views:
            problems.append(
                Problem(
                    "STORE_SOURCE_INVALID",
                    file,
                    None,
                    f"reference at /datasets/{at}/source: dataset {dataset.get('name')!r} reads "
                    f"{source!r}, a view of an existing lake; only its tables can be read",
                )
            )
            continue
        why = (refused or {}).get(f"{schema or 'main'}.{name}".casefold())
        if why is not None:
            problems.append(Problem(why.code, file, None, why.message))
            continue
        table = known.get(((schema or "main").casefold(), name.casefold()))
        if table is None:
            problems.append(
                Problem(
                    "OSSIE_INVALID",
                    file,
                    None,
                    f"reference at /datasets/{at}/source: dataset {dataset.get('name')!r} reads "
                    f"{source!r}, which is not a lake table at snapshot {tables.snapshot}",
                )
            )
            continue
        catalog[source] = {c.name: SourceColumn(c.type, c.nullable) for c in table.columns}
    return catalog, problems


def _on(datasets: Mapping[str, str], connection: str) -> Callable[[str], bool]:
    """Whether a dataset (normalized name) is on `connection`."""
    return lambda dataset: datasets.get(dataset) == connection


def _route_refused(file: str, connections: Sequence[str]) -> Problem:
    """Datasets on a Direct Query connection whose engine is not enabled in this process."""
    return Problem(
        "UNSUPPORTED_OPERATION",
        file,
        None,
        f"operation: route; datasets read the Direct Query connection {', '.join(connections)},"
        " an engine enabled only for development",
    )


def _two_engines(file: str, source: str) -> Problem:
    """One source text read on two engines: which table's columns it has would be ambiguous."""
    return Problem(
        "OSSIE_INVALID",
        file,
        None,
        f"source {source!r} is read by datasets on two engines; name each lake's table apart",
    )


def _named_groups(text: bytes) -> set[str]:
    """The groups `security.yaml` names, read leniently: what the policy reader then checks."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return set()
    found: set[str] = set()
    if not isinstance(document, dict):
        return found
    for key, field in (("row_policies", "exempt_groups"), ("column_policies", "visible_to_groups")):
        for policy in document.get(key) or ():
            if isinstance(policy, dict) and isinstance(policy.get(field), list):
                found |= {g for g in policy[field] if isinstance(g, str)}
    return found


def load_semantic(
    folder: Path,
    lake: Lake,
    snapshot: int,
    vendor_names: Sequence[str],
    *,
    audience: Audience,
    groups: Collection[str] | None = None,
    experimental: bool = False,
    connections: Mapping[str, object] | Collection[str] = (),
    external: Mapping[str, ExternalTables | StoreError] | None = None,
) -> Loaded:
    """The project's semantic model compiled against `lake` as of `snapshot`, or its problems,
    written for `audience`. `groups`: the project's groups as `access` knows them; none in local
    mode (Owner decision on #358), where the groups `security.yaml` names are taken as they
    are and nobody belongs to any, so no exemption ever applies (fail closed). `connections`:
    the names in `connections.yaml`, which a dataset's `connection` must be one of (P2-29).
    `external`: the tables of each existing lake whose engine is enabled in this process (P2-30
    PR 3); a dataset on an existing lake whose engine is not is `UNSUPPORTED_OPERATION`
    (`operation: route`), and the model is not compiled. A lake given as the `StoreError` that
    kept its tables from being read (no verified snapshot to serve) is that error's problem."""
    files = model_files(folder)
    if not files:
        return Loaded(None)
    if len(files) > 1:
        names = ", ".join(f"{FOLDER}/{p.name}" for p in files)
        return Loaded(
            None,
            (Problem("OSSIE_INVALID", f"{FOLDER}/", None, f"one model per project: {names}"),),
        )
    path = files[0]
    file = f"{FOLDER}/{path.name}"
    security = folder / POLICIES
    try:
        content = path.read_bytes()
    except OSError as error:
        return Loaded(
            None, (Problem("OSSIE_INVALID", file, None, f"unreadable: {error.strerror}"),)
        )
    try:
        rules = read_regular_file(security)
    except OSError as error:
        why = f"unreadable: {error.strerror}"
        return Loaded(None, (Problem("POLICY_INVALID", POLICIES, None, why),))
    try:
        model = read(content, file, vendor_names)
    except SemanticError as error:
        return Loaded(None, tuple(_problems(error, file, audience)))
    engines = engine_catalog(connections)
    on = {d: c for d, c in dataset_connections(model).items() if c in engines.entries}
    disabled = sorted({c for c in on.values() if external is None or c not in external})
    if disabled:
        return Loaded(None, (_route_refused(file, disabled),))
    try:
        tables = lake.table_columns(TablesRequest(kind="tables", snapshot=snapshot))
    except StoreError as error:
        return Loaded(None, (Problem(error.code, file, None, error.message),))
    catalog, problems = _catalog(model.document, tables, file, lambda d: d not in on)
    adapters: dict[str, ExternalAdapter] = {}
    for connection in sorted(set(on.values())):
        given = (external or {})[connection]
        if isinstance(given, StoreError):
            problems.append(Problem(given.code, file, None, given.message))
            continue
        if given.adapter is not None:
            adapters[connection] = given.adapter
        found, more = _catalog(
            model.document,
            given.tables,
            file,
            _on(on, connection),
            given.views,
            given.refused,
        )
        problems += more
        problems += [_two_engines(file, source) for source in sorted(set(found) & set(catalog))]
        catalog.update(found)
    if problems:
        return Loaded(None, tuple(problems))
    try:
        compiled = compile_model(model, catalog, vendor_names, experimental=experimental)
    except SemanticError as error:
        return Loaded(None, tuple(_problems(error, file, audience)))
    try:
        routing = route_model(compiled, engines, file)
    except SemanticError as error:
        return Loaded(None, tuple(_problems(error, file, audience)))
    policies: PolicyModel | None = None
    if rules is not None:
        managed = {f"{t.schema_}.{t.table}": [c.name for c in t.columns] for t in tables.tables}
        try:
            policies = read_policies(
                rules,
                compiled,
                groups=_named_groups(rules) if groups is None else groups,
                managed_tables=managed,
                file=POLICIES,
            )
        except SemanticError as error:
            return Loaded(None, tuple(_problems(error, POLICIES, audience)))
    return Loaded(SemanticModel(compiled, policies, snapshot, file, routing, adapters))
