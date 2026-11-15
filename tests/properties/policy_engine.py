"""The engine side of the non-broadening properties (P3-14, #448): a generated policy model
read by the engine, a generated world and its mapping data as a managed lake, and a question
asked of the engine on it as one caller, every page of a detail question included. What is
generated is `policy_world`'s; what is expected, `policy_oracle`'s.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from orvex.contracts.store.client import Lake
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.execution import Answer, answer
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.semantic.result.cursors import SealedForm

from conformance.runners import corpus_execution as ce
from conformance.runners.security_engines import column_name
from tests.properties.policy_world import GROUPS, MANAGED, MAPPINGS, Document, Mappings, Policies
from tests.properties.world import CATALOG, COMPILED, World


def compiled_policies(policies: Policies) -> PolicyModel:
    return read_policies(
        yaml.safe_dump(policies.document()),
        COMPILED,
        groups=frozenset(GROUPS),
        managed_tables=MANAGED,
    )


@contextmanager
def lake_with(world: World, data: Mappings) -> Iterator[Lake]:
    """The world's tables, and the mapping tables in schema `security`."""
    with tempfile.TemporaryDirectory() as folder:
        inputs = [
            _csv(Path(folder), "customers", CATALOG["customers"], world.customers),
            _csv(Path(folder), "sales", CATALOG["sales"], world.sales),
        ]
        tables: dict[str, list[dict[str, str | None]]] = {
            "channel_allow": [{"user_id": u, "channel": c} for u, c in data.allow],
            "region_deny": [{"user_id": u, "region": r} for u, r in data.deny],
            "registered": [{"user_id": u} for u in data.registered],
        }
        security = Path(folder) / "security"
        security.mkdir()
        mapped = [_csv(security, n, MAPPINGS[n], rows) for n, rows in tables.items()]
        with ce.case_lake(inputs, CATALOG) as lake:
            ce.load_tables(
                lake, mapped, {f"security.{n}": c for n, c in MAPPINGS.items()}, "security"
            )
            yield lake


def _csv(
    folder: Path,
    name: str,
    columns: Mapping[str, SourceColumn],
    rows: list[dict[str, str | None]],
) -> Path:
    path = folder / f"{name}.csv"
    names = list(columns)
    lines = [",".join(names)]
    lines += [",".join("" if r[n] is None else str(r[n]) for n in names) for r in rows]
    path.write_text("\n".join(lines) + "\n", "utf-8")
    return path


def ask(document: Document, lake: Lake, policy: PolicyModel, context: Mapping[str, Any]) -> Answer:
    snapshot = lake.current_snapshot()
    pinned = {"id": snapshot.id, "committed_at": snapshot.committed_at.isoformat()}
    engine = ce.engine_for(
        lake,
        cursors=SealedForm(b"p" * 32),
        pinned=lambda version: pinned if version == str(snapshot.id) else None,
    )
    return answer(json.dumps(document), COMPILED, policy, dict(context), engine)


def every_page(
    query: Document, lake: Lake, policy: PolicyModel, context: Mapping[str, Any]
) -> tuple[list[list[str | None]], list[str], list[str]]:
    """A detail question's rows over all its pages, its columns and its first page's notices."""
    rows: list[list[str | None]] = []
    document: Document = query
    columns: list[str] = []
    notices: list[str] = []
    while True:
        found = ask(document, lake, policy, context)
        envelope = found.result.envelope()
        if not columns:
            columns = [column_name(c) for c in envelope["columns"]]
            notices = [str(n["code"]) for n in envelope.get("notices", [])]
        rows += ce.rows_of(found)
        cursor = envelope["page"].get("next")
        if cursor is None:
            return rows, columns, notices
        document = {**query, "page": {**query["page"], "after": cursor}}
