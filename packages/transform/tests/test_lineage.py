"""P1-18: OpenLineage events — datasets, column lineage and the official JSON Schema."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import jsonschema
from orvex.contracts.model_config.v1 import ModelConfig
from orvex.contracts.project.v1 import ParquetConnection, PostgresConnection
from orvex.transform.lineage import (
    Connection,
    Dataset,
    ModelRun,
    Scope,
    column_lineage,
    events,
    inputs,
    project_key,
)
from orvex.transform.models import Model, discover_models
from referencing import Registry, Resource

SPEC = Path(__file__).parent / "openlineage"
LAKE = "ducklake://test"
SCOPE = Scope("urn:product:transform:0.0.0", "product://test", LAKE)


def validator(definition: str, schema_file: Path) -> jsonschema.Draft202012Validator:
    """A validator for one definition of the vendored OpenLineage schemas."""
    resources = []
    for path in [SPEC / "OpenLineage.json", *sorted((SPEC / "facets").glob("*.json"))]:
        document = json.loads(path.read_text(encoding="utf-8"))
        resources.append((document["$id"], Resource.from_contents(document)))
    registry: Registry[Any] = Registry().with_resources(resources)
    root = json.loads(schema_file.read_text(encoding="utf-8"))["$id"]
    return jsonschema.Draft202012Validator(
        {"$ref": f"{root}#/$defs/{definition}"}, registry=registry
    )


def check(event: dict[str, Any]) -> None:
    """The event against RunEvent, and every facet against its own schema."""
    validator("RunEvent", SPEC / "OpenLineage.json").validate(event)
    facets = [*event["run"].get("facets", {}).items()]
    for dataset in event.get("outputs", []):
        facets += [*dataset.get("facets", {}).items(), *dataset.get("outputFacets", {}).items()]
    names = {
        "parent": "ParentRunFacet",
        "errorMessage": "ErrorMessageRunFacet",
        "schema": "SchemaDatasetFacet",
        "columnLineage": "ColumnLineageDatasetFacet",
        "outputStatistics": "OutputStatisticsOutputDatasetFacet",
    }
    for key, facet in facets:
        name = names[key]
        validator(name, SPEC / "facets" / f"{name}.json").validate(facet)


def project(tmp_path: Path, files: dict[str, str]) -> dict[str, Model]:
    for relative, text in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    found = discover_models(tmp_path, {})
    assert found.problems == ()
    return {model.name: model for model in found.models}


CONNECTIONS: dict[str, Connection] = {
    "archive": ParquetConnection(type="parquet", path="/data/readings.parquet"),  # type: ignore[arg-type]
    "production": PostgresConnection.model_validate(
        {
            "type": "postgres",
            "host": "db.internal",
            "port": 5432,
            "database": "billing",
            "user": "reader",
            "password": "${env:SECRET_PASSWORD}",
            "read_only": True,
        }
    ),
}


def test_inputs_are_sources_and_models_without_secrets(tmp_path: Path) -> None:
    models = project(
        tmp_path,
        {
            "models/staging/readings.sql": "SELECT * FROM archive.data",
            "models/staging/consumers.sql": "SELECT * FROM production.consumers",
            "models/marts/m.sql": (
                "WITH c AS (SELECT * FROM staging.consumers) "
                "SELECT * FROM staging.readings JOIN c USING (consumer_id) "
                "JOIN production.public.tariffs USING (tariff)"
            ),
        },
    )
    found = inputs(models["marts.m"], models, CONNECTIONS, LAKE)
    assert found == [
        Dataset(LAKE, "staging.consumers"),
        Dataset(LAKE, "staging.readings"),
        Dataset("postgres://db.internal:5432", "billing.public.tariffs"),
    ]
    assert inputs(models["staging.readings"], models, CONNECTIONS, LAKE) == [
        Dataset("file", "/data/readings.parquet")
    ]
    assert "reader" not in repr(found)


def test_column_lineage_only_when_provable(tmp_path: Path) -> None:
    models = project(
        tmp_path,
        {
            "models/staging/readings.sql": "SELECT 1 AS consumer_id, 1.0 AS kwh",
            "models/marts/total.sql": (
                "SELECT r.consumer_id, sum(r.kwh) AS kwh, r.kwh * 2 AS twice "
                "FROM staging.readings AS r GROUP BY ALL"
            ),
            "models/marts/star.sql": "SELECT * FROM staging.readings",
            "models/marts/nested.sql": "SELECT consumer_id FROM (SELECT * FROM staging.readings)",
        },
    )
    output = [("consumer_id", "INTEGER"), ("kwh", "DECIMAL(38,1)"), ("twice", "DECIMAL(18,1)")]
    found = column_lineage(models["marts.total"], output, models, CONNECTIONS, LAKE)

    def field(name: str, subtype: str) -> dict[str, Any]:
        return {
            "namespace": LAKE,
            "name": "staging.readings",
            "field": name,
            "transformations": [{"type": "DIRECT", "subtype": subtype}],
        }

    assert found == {
        "consumer_id": [field("consumer_id", "IDENTITY")],
        "kwh": [field("kwh", "AGGREGATION")],
        "twice": [field("kwh", "TRANSFORMATION")],
    }
    star = [("consumer_id", "INTEGER"), ("kwh", "DECIMAL(2,1)")]
    assert column_lineage(models["marts.star"], star, models, CONNECTIONS, LAKE) == {}
    nested = [("consumer_id", "INTEGER")]
    assert column_lineage(models["marts.nested"], nested, models, CONNECTIONS, LAKE) == {}


def test_events_follow_the_specification(tmp_path: Path) -> None:
    models = project(tmp_path, {"models/s/m.sql": "SELECT 1 AS id"})
    common = {
        "model": models["s.m"],
        "started_at": "2026-10-28T10:00:00.000000+00:00",
        "finished_at": "2026-10-28T10:00:01.000000+00:00",
        "output": [("id", "INTEGER")],
        "inputs": [Dataset("file", "/data/x.parquet")],
        "column_lineage": {
            "id": [
                {
                    "namespace": "file",
                    "name": "/data/x.parquet",
                    "field": "id",
                    "transformations": [{"type": "DIRECT", "subtype": "IDENTITY"}],
                }
            ]
        },
    }
    runs = [
        ModelRun(
            run_id="0192f0c0-0000-7000-8000-000000000001",
            outcome="success",
            rows_written="170141183460469231731687303715884105727",
            error=None,
            **common,  # type: ignore[arg-type]
        ),
        ModelRun(
            run_id="0192f0c0-0000-7000-8000-000000000002",
            outcome="failed",
            rows_written=None,
            error="MODEL_TEST_FAILED: s.m: unique(id) failed on 2 rows.",
            **common,  # type: ignore[arg-type]
        ),
    ]
    found = events(SCOPE, "3f1b9a52-7e1c-4b7f-9a52-0c1d2e3f4a5b", runs)
    assert [event["eventType"] for event in found] == ["START", "COMPLETE", "START", "FAIL"]
    for event in found:
        check(event)
    assert found[1]["outputs"][0]["outputFacets"]["outputStatistics"]["rowCount"] == (
        170141183460469231731687303715884105727
    )
    assert found[3]["run"]["facets"]["errorMessage"]["message"].startswith("MODEL_TEST_FAILED")
    assert found[0]["run"]["facets"]["parent"]["run"]["runId"] == (
        "3f1b9a52-7e1c-4b7f-9a52-0c1d2e3f4a5b"
    )


def test_project_keys() -> None:
    assert project_key("Energía — Facturación 2026") == "energ-a-facturaci-n-2026"
    assert project_key("  ") == "project"


def test_unqualified_names_must_be_real_columns(tmp_path: Path) -> None:
    models = project(
        tmp_path,
        {
            "models/staging/readings.sql": "SELECT 1 AS consumer_id, 1.0 AS kwh",
            "models/marts/lateral.sql": (
                "SELECT kwh * 2 AS doubled, doubled + 1 AS plus FROM staging.readings"
            ),
            "models/marts/lambda.sql": (
                "SELECT list_transform([1, 2], kwh -> kwh + 1) AS l FROM staging.readings"
            ),
        },
    )

    def columns_of(path: list[str]) -> list[str] | None:
        return ["consumer_id", "kwh"] if path == ["staging", "readings"] else None

    lateral = [("doubled", "DECIMAL(18,1)"), ("plus", "DECIMAL(18,1)")]
    found = column_lineage(models["marts.lateral"], lateral, models, CONNECTIONS, LAKE, columns_of)
    assert list(found) == ["doubled"]  # `doubled` in `plus` is an alias, not a column
    assert found["doubled"][0]["field"] == "kwh"
    assert column_lineage(models["marts.lateral"], lateral, models, CONNECTIONS, LAKE) == {}
    lambdas = [("l", "INTEGER[]")]
    assert (
        column_lineage(models["marts.lambda"], lambdas, models, CONNECTIONS, LAKE, columns_of) == {}
    )


def test_extraction_models_have_no_invented_input(tmp_path: Path) -> None:
    models = project(tmp_path, {"models/raw/events.sql": "SELECT 1"})
    extraction = dataclasses.replace(
        models["raw.events"], config=ModelConfig(version=1, connection="ch")
    )
    assert inputs(extraction, models, CONNECTIONS, LAKE) == []
