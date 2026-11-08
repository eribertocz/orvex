"""Engines of a model's datasets: the Direct Query foundation (P2-29, #370; ADR-0027 §3 to §6).

- **Engine per dataset.** A dataset names its connection in its product extension
  (`connection`); absent, it is in the managed lake. The engine is never declared in the
  model: `server` derives an `EngineCatalog` from `connections.yaml`, one `EngineEntry` per
  Direct Query connection, and `route_model` binds every dataset to its entry.
- **Same engine means the same environment.** Two connections share an engine only when their
  `environment` (the resolved endpoint and database or catalog, never a secret) is equal;
  sharing a dialect is never enough.
- **One engine per question.** A relationship between datasets on different engines is refused
  when the model is bound, and a question that would read two engines is refused before any
  read; both with `CROSS_ENGINE_QUERY`. Results of two engines are never joined.
- **Capabilities.** Each entry states what its adapter can run (`CAPABILITIES`); a plan needing
  anything else is refused before any read with `UNSUPPORTED_OPERATION`, naming it.
- **Enabled engines.** An entry is usable only with an adapter. `server` supplies adapters only
  for engines whose whole semantic corpus passes with results identical to the managed lake
  (ADR-0027 §4); none is enabled in this release, so outside the test suite a dataset on
  another engine is refused (`UNSUPPORTED_OPERATION`, `operation: route`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import sql
from orvex.semantic.ossie.model import OssieModel

if TYPE_CHECKING:
    import pyarrow as pa
    from orvex.semantic.adapters.duckdb import PhysicalQuery, PolicyValues

MANAGED = "managed"
Route = Literal["managed", "existing_ducklake", "live"]
# What an adapter can run; a plan names what it needs (`required`).
CAPABILITIES = frozenset(
    {
        "count_distinct",  # a holistic distinct count
        "window",  # running totals and other windows after aggregation (S9)
        "grouping_sets",  # totals and subtotals in one statement (P2-23)
        "time_zone",  # converting an instant to a zone before bucketing (C6)
        "detail_pages",  # keyset pages at the base grain (P2-24)
        "multi_fact",  # aligning facts on conformed dimensions (C5)
        "bridge",  # many-to-many allocation through a bridge (P2-18)
        "as_of",  # temporal as-of joins (P2-20)
        "exact_decimal",  # DECIMAL arithmetic without floats: every plan needs it
    }
)


@dataclass(frozen=True)
class EngineEntry:
    """One Direct Query connection as `server` derived it from `connections.yaml`."""

    connection: str
    kind: str  # the engine's kind, e.g. `ducklake`; its adapter's dialect
    route: Route  # `existing_ducklake` (snapshots) or `live` (read instants)
    environment: str  # endpoint and database or catalog: equal means the same engine
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.route == MANAGED:
            raise ValueError("the managed lake is not a Direct Query connection")
        unknown = set(self.capabilities) - CAPABILITIES
        if unknown:
            raise ValueError(f"unknown capabilities: {sorted(unknown)}")


@dataclass(frozen=True)
class EngineCatalog:
    """The project's Direct Query connections, by name; `others` names the connections that
    exist but are not Direct Query (Route A sources), so a dataset naming one is told why."""

    entries: Mapping[str, EngineEntry] = field(default_factory=dict)
    others: frozenset[str] = frozenset()


NO_ENGINES = EngineCatalog()


@dataclass(frozen=True)
class Routing:
    """Every dataset's engine: None for the managed lake. Names are matched normalized, as
    every other reference to a dataset is."""

    datasets: Mapping[str, EngineEntry | None]

    def __post_init__(self) -> None:
        normalized = {sql.normalize(name): entry for name, entry in self.datasets.items()}
        object.__setattr__(self, "datasets", normalized)

    def of(self, dataset: str) -> EngineEntry | None:
        return self.datasets.get(sql.normalize(dataset))

    def route(self, dataset: str) -> Route:
        entry = self.of(dataset)
        return "managed" if entry is None else entry.route

    def environment(self, dataset: str) -> str:
        entry = self.of(dataset)
        return MANAGED if entry is None else f"{entry.kind}:{entry.environment}"

    @property
    def external(self) -> bool:
        return any(entry is not None for entry in self.datasets.values())


ALL_MANAGED = Routing({})


class ExternalAdapter(Protocol):
    """What `server` supplies for one enabled Direct Query connection (none in this release).

    - `pin()`: the data version every read of one question uses, fixed once, before the first
      read: `{"id", "committed_at", "project"}` for an existing DuckLake snapshot, or
      `{"read_at"}` for a live engine, whose reads are not repeatable.
    - `compile(plan, values)`: the plan's statement in the engine's dialect, with every mapping
      subquery bound from `values` (resolved on the managed lake); it never reads.
    - `read(...)`: one read-only statement at that version, every value bound."""

    connection: str
    engine: str
    engine_version: str

    def pin(self) -> Mapping[str, object]: ...

    def compile(self, plan: Any, values: Mapping[int, PolicyValues]) -> PhysicalQuery: ...  # noqa: ANN401

    def read(
        self,
        statement: str,
        parameters: Sequence[Mapping[str, str | None]],
        version: Mapping[str, object],
        batch_size: int,
        timeout_ms: int | None,
    ) -> pa.RecordBatchReader: ...


def _items(node: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    found = node.get(key, ())
    return (
        [item for item in found if isinstance(item, Mapping)] if isinstance(found, Sequence) else []
    )


def dataset_connections(ossie: OssieModel) -> dict[str, str]:
    """Each dataset's written connection, by normalized name: what `server` must know before
    it compiles, since a dataset's columns come from its own engine."""
    return {dataset: connection for dataset, (connection, _) in _written(ossie).items()}


def _connections(compiled: CompiledModel) -> dict[str, tuple[str, str]]:
    """Each dataset's written connection, with the pointer it is written at."""
    return _written(compiled.ossie)


def _written(ossie: OssieModel) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for extension in ossie.product_extensions:
        payload = extension.payload
        if payload.get("element") != "dataset" or "connection" not in payload:
            continue
        owner = extension.pointer.rsplit("/custom_extensions/", 1)[0]
        index = int(owner.rsplit("/", 1)[1])
        dataset = _items(ossie.document, "datasets")[index]
        found[sql.normalize(str(dataset["name"]))] = (str(payload["connection"]), extension.pointer)
    return found


def dataset_label(compiled: CompiledModel, dataset: str) -> str:
    """A dataset as its model writes it: what an error names."""
    for node in _items(compiled.ossie.document, "datasets"):
        if sql.normalize(str(node["name"])) == dataset:
            return str(node["name"])
    return dataset


def route_model(compiled: CompiledModel, catalog: EngineCatalog, file: str = "") -> Routing:
    """Every dataset bound to its engine, or the model's error:
    - `OSSIE_INVALID` (`rule: reference`) for a connection that is unknown, or is not Direct
      Query, at the dataset's extension;
    - `CROSS_ENGINE_QUERY` for a relationship between datasets on different engines."""
    datasets: dict[str, EngineEntry | None] = {
        sql.normalize(str(node["name"])): None
        for node in _items(compiled.ossie.document, "datasets")
    }
    problems: list[dict[str, object]] = []
    for dataset, (connection, pointer) in sorted(_connections(compiled).items()):
        entry = catalog.entries.get(connection)
        if entry is None:
            problems.append(
                {"pointer": f"{pointer}/custom_extensions", "line": "0", "rule": "reference"}
            )
            continue
        datasets[dataset] = entry
    if problems:
        raise SemanticError(
            "OSSIE_INVALID",
            {
                "file": file or "semantic",
                "problems": problems[:50],
                "problems_omitted": str(max(0, len(problems) - 50)),
            },
        )
    routing = Routing(datasets)
    for relationship in _items(compiled.ossie.document, "relationships"):
        ends = [sql.normalize(str(relationship[end])) for end in ("from", "to")]
        refuse_mixed(compiled, routing, ends)
    return routing


def refuse_mixed(compiled: CompiledModel, routing: Routing, datasets: Iterable[str]) -> None:
    """`CROSS_ENGINE_QUERY` when `datasets` are not all on one engine: the datasets are public,
    the engines (their connections) builder-only."""
    chosen = sorted(set(datasets))
    environments = {routing.environment(d) for d in chosen}
    if len(environments) <= 1:
        return
    engines = sorted(
        {MANAGED if routing.of(d) is None else routing.of(d).connection for d in chosen}  # type: ignore[union-attr]
    )
    if len(engines) < 2:  # two connections, one name: never, but the details need two
        engines = [*engines, MANAGED]
    raise SemanticError(
        "CROSS_ENGINE_QUERY",
        {"datasets": [dataset_label(compiled, d) for d in chosen], "engines": engines},
    )


def required(plan: Any) -> set[str]:  # noqa: ANN401 - a `SemanticPlan`
    """The capabilities a plan needs from its engine."""
    needs: set[str] = {"exact_decimal"}  # exact values are never optional (laws)
    if plan.mode == "detail":
        needs.add("detail_pages")
    if plan.windows:
        needs.add("window")
    if plan.totals not in (None, "none"):
        needs.add("grouping_sets")
    if len(plan.facts) > 1 or plan.derived:
        needs.add("multi_fact")
    compiled = plan.compiled
    if any(_distinct(compiled.members[a.metric].parsed.tree) for a in plan.aggregates):
        needs.add("count_distinct")
    if any(group.zone for group in plan.groups) or any(
        source.latest is not None and source.latest.zone for source in plan.sources
    ):
        needs.add("time_zone")
    if any(a.allocation is not None for a in plan.aggregates):
        needs.add("bridge")
    if any(join.as_of is not None for join in plan.joins):
        needs.add("as_of")
    return needs


def _distinct(node: object) -> bool:
    """Whether an expression tree holds a `DISTINCT` aggregate call, at any depth."""
    if isinstance(node, sql.Call) and node.distinct:
        return True
    if isinstance(node, tuple | list):
        return any(_distinct(item) for item in node)
    if hasattr(node, "__dataclass_fields__"):
        return any(_distinct(getattr(node, name)) for name in node.__dataclass_fields__)
    return False


def refuse_unable(entry: EngineEntry, plan: Any) -> None:  # noqa: ANN401
    """`UNSUPPORTED_OPERATION`, naming the first capability the engine lacks, before any read."""
    missing = sorted(required(plan) - entry.capabilities)
    if missing:
        raise SemanticError("UNSUPPORTED_OPERATION", {"operation": missing[0]})
