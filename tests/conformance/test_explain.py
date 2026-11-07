"""P2-27b (#364): `explain` says what the engine would do with a question, and why, with no
data read; its question, plan and evidence are the receipt's; HTTP errors stay errors."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import jsonschema
import pyarrow as pa
import pytest
import yaml
from fastapi.testclient import TestClient
from orvex.contracts.store.v1 import ChangesRequest, ReadRequest
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.errors import SemanticError
from orvex.semantic.execution import Engine
from orvex.semantic.explanation import explain
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.semantic.receipt import NO_ONE
from orvex.semantic.receipt.viewer import CallerViewer
from orvex.server import semantic_http, semantic_model
from orvex.server.app import LocalSemantic, create_app
from orvex.server.local_access import new_access
from orvex.server.versions import Versions
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from conformance.runners.semantic_corpus import CORPUS, ROOT
from tests.conformance import test_asof_windows as windows
from tests.conformance.test_execution import corpus_case, lake_for, no_policies

SCHEMA = json.loads((ROOT / "contracts/semantic-explain/v1/schema.json").read_text("utf-8"))
RECEIPT = json.loads((ROOT / "contracts/receipt/v1/schema.json").read_text("utf-8"))
CASES = sorted(
    p.name for p in (ROOT / CORPUS / "corpus" / "cases").iterdir() if p.name in ce.QUERIES
)
CANARIES = ("SELECT", " FROM ", '"#', "$p_", "AT (VERSION")
PORT = 7878
EVIDENCE_RECORDS = "SELECT element, name, kind, snapshot, declaration, status, violations, "


class NoRead:
    """A lake that serves only the metadata an explanation may use, and fails on anything
    else: no request that returns a dataset's rows."""

    def __init__(self, lake: Lake) -> None:
        self._lake = lake
        self.used: list[str] = []

    def snapshot(self, snapshot_id: int) -> Any:  # noqa: ANN401
        self.used.append("snapshot")
        return self._lake.snapshot(snapshot_id)

    def current_snapshot(self) -> Any:  # noqa: ANN401
        self.used.append("current_snapshot")
        return self._lake.current_snapshot()

    def table_columns(self, request: Any) -> Any:  # noqa: ANN401
        self.used.append("table_columns")
        return self._lake.table_columns(request)

    def statistics(self, request: Any) -> Any:  # noqa: ANN401
        self.used.append("statistics")
        return self._lake.statistics(request)

    def changes(self, request: ChangesRequest) -> Any:  # noqa: ANN401
        assert request.count_removed is False, "counting removed rows reads them"
        self.used.append("changes")
        return self._lake.changes(request)

    def read(self, request: ReadRequest) -> pa.RecordBatchReader:
        # Module state only: the evidence records' own statement, never a dataset.
        assert request.sql.startswith(EVIDENCE_RECORDS), "an explanation read data"
        assert "FROM _meta.evidence_records QUALIFY" in request.sql
        self.used.append("read:_meta")
        return self._lake.read(request)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        raise AssertionError(f"an explanation used Lake.{name}")


class CaseSource:
    def __init__(self, lake: NoRead, compiled: CompiledModel, policies: PolicyModel) -> None:
        self.lake, self.compiled, self.policies = lake, compiled, policies

    @contextmanager
    def session(self) -> Iterator[Any]:
        yield self.lake

    def model(self, lake: Any, snapshot: int) -> semantic_model.Loaded:  # noqa: ANN401
        return semantic_model.Loaded(
            semantic_model.SemanticModel(self.compiled, self.policies, snapshot, "semantic/case")
        )


def client(
    lake: NoRead, compiled: CompiledModel, policies: PolicyModel, context: Mapping[str, Any]
) -> tuple[TestClient, dict[str, str]]:
    access = new_access("conformance", PORT)
    local = LocalSemantic(
        "conformance",
        access,
        semantic_http.Semantic(
            CaseSource(lake, compiled, policies),
            lambda request, policies, snapshot: context,
            semantic_http.session_settings(),
        ),
    )
    app = create_app(Versions("0.0.0", (), ()), None, PORT, local)
    http = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=True)
    return http, {"Authorization": f"Bearer {access.bearer}"}


def case_policies(case: str, compiled: CompiledModel) -> PolicyModel:
    _, _, model = corpus_case(case)
    return read_policies(
        yaml.safe_dump(ce.policies_of(model)), compiled, groups=frozenset(), managed_tables={}
    )


def builder(context: Mapping[str, Any]) -> dict[str, Any]:
    return {**context, "role": "builder"}


@pytest.mark.parametrize("case", CASES)
def test_every_corpus_question_is_explained_without_reading_data(case: str) -> None:
    if case == "CASE-017":  # a model the compiler refuses: no question to explain
        with pytest.raises(SemanticError, match="INCOMPATIBLE_UNITS"):
            corpus_case(case)
        return
    compiled, _, _ = corpus_case(case)
    policies = case_policies(case, compiled)
    context = builder(ce.viewer_context(attributes=ce.CALLERS.get(case, {})))
    with lake_for(case) as lake:
        guarded = NoRead(lake)
        http, headers = client(guarded, compiled, policies, context)
        response = http.post("/api/v1/semantic/explain", json=ce.QUERIES[case], headers=headers)
    if case == "CASE-016":  # an ambiguous path is the question's own error, never explained
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "AMBIGUOUS_RELATIONSHIP_PATH"
        return
    assert response.status_code == 200, response.text
    document = response.json()
    assert list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(document)) == []
    assert not any(c in json.dumps(document) for c in CANARIES)  # no SQL, ever
    expected = {"CASE-020": "ILLEGAL_TEMPORAL_REAGGREGATION"}
    if case in expected:
        assert document["outcome"] == "refused"
        assert document["refusal"]["code"] == expected[case]
    else:  # CASE-008 included: its total is answered (P2-23); only the runner skips it
        assert (document["outcome"], document["stage"]) == ("planned", "planned")
    assert not any(use.startswith("read:") and use != "read:_meta" for use in guarded.used)


@pytest.mark.parametrize("case", ["CASE-001", "CASE-004", "CASE-015", "CASE-021"])
def test_the_question_plan_and_evidence_are_the_receipt_s(case: str) -> None:
    compiled, _, _ = corpus_case(case)
    policies = case_policies(case, compiled)
    context = builder(ce.viewer_context(attributes=ce.CALLERS.get(case, {})))
    query = json.dumps(ce.QUERIES[case])
    viewer = CallerViewer(compiled, policies, context)
    with lake_for(case) as lake:
        from orvex.semantic.execution import answer

        found = answer(query, compiled, policies, context, ce.engine_for(lake))
        explained = explain(query, compiled, policies, context, ce.engine_for(lake), viewer)
    view = found.receipt.view(viewer)
    assert explained["question"] == view["question"]
    assert explained["plan"] == view["plan"]
    assert explained["evidence"] == view["evidence"]
    assert explained["query_fingerprint"] == view["query_fingerprint"]
    assert explained["model_fingerprint"] == found.result.envelope()["model_fingerprint"]


def test_the_shared_definitions_are_the_receipt_s() -> None:
    """The explanation's copies of the receipt's `$defs` stay equal; only `Evidence.status`
    adds `violated`."""
    for name, definition in SCHEMA["$defs"].items():
        if name in {"Materialization"}:
            continue
        expected = RECEIPT["$defs"][name]
        if name == "Evidence":
            expected = json.loads(json.dumps(expected))
            expected["properties"]["status"] = {"enum": ["declared", "verified", "violated"]}
            definition = {k: v for k, v in definition.items() if k != "description"}
            expected = {k: v for k, v in expected.items() if k != "description"}
        assert definition == expected, name
    packaged = ROOT / "packages/semantic/src/orvex/semantic/semantic-explain-v1.json"
    assert (
        packaged.read_bytes() == (ROOT / "contracts/semantic-explain/v1/schema.json").read_bytes()
    )


def engine_without_reads(lake: Lake) -> Engine:
    def refuse(request: Any) -> pa.RecordBatchReader:  # noqa: ANN401
        raise AssertionError("an explanation read data")

    return ce.engine_for(lake, read=refuse)


def test_an_unsupported_shape_is_explained_as_an_engine_refusal() -> None:
    document = windows.query("moving", group_by=("daily.day",))
    policies = no_policies(windows.DAILY)
    context = builder(ce.viewer_context())
    with windows.lake_of(windows.DAYS, windows.DAILY_CATALOG) as lake:
        explained = explain(
            json.dumps(document),
            windows.DAILY,
            policies,
            context,
            engine_without_reads(lake),
            CallerViewer(windows.DAILY, policies, context),
        )
    # Refused by the analysis itself: the last stage reached is the binding.
    assert (explained["outcome"], explained["stage"]) == ("refused", "bound")
    assert explained["refusal"]["code"] == "UNSUPPORTED_OPERATION"
    assert explained["refusal"]["category"] == "engine"
    assert explained["question"]["mode"] == "aggregate"
    for part in ("plan", "evidence", "materialization", "estimate"):
        assert part not in explained


def test_a_page_above_the_cap_is_explained_as_a_budget_refusal_at_reception() -> None:
    compiled, _, _ = corpus_case("CASE-001")
    policies = no_policies(compiled)
    context = builder(ce.viewer_context())
    sale = next(
        m.parsed.label for m in compiled.members.values() if m.parsed.label.endswith("sale_id")
    )
    query = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "detail",
        "fields": [{"member": sale}],
        "order": [{"by": sale, "direction": "asc"}],
        "page": {"size": "5000"},  # the engine's cap is 1000
    }
    with lake_for("CASE-001") as lake:
        explained = explain(
            json.dumps(query),
            compiled,
            policies,
            context,
            engine_without_reads(lake),
            CallerViewer(compiled, policies, context),
        )
    assert (explained["outcome"], explained["stage"]) == ("refused", "received")
    assert explained["refusal"]["code"] == "QUERY_BUDGET_EXCEEDED"
    assert "question" not in explained
    assert "query_fingerprint" not in explained


def test_a_hidden_member_is_never_explained() -> None:
    compiled, _, _ = corpus_case("CASE-001")
    policies = no_policies(
        compiled, column_policies=[{"member": "sales.region", "default": "hidden"}]
    )
    context = builder(ce.viewer_context())
    query = {**ce.QUERIES["CASE-001"]}
    with lake_for("CASE-001") as lake, pytest.raises(SemanticError) as caught:
        explain(
            json.dumps(query),
            compiled,
            policies,
            context,
            engine_without_reads(lake),
            CallerViewer(compiled, policies, context),
        )
    assert caught.value.code == "MEMBER_NOT_FOUND"


def test_a_viewer_that_sees_nothing_gets_no_builder_part() -> None:
    compiled, _, _ = corpus_case("CASE-001")
    policies = no_policies(compiled)
    context = ce.viewer_context()  # not a builder
    with lake_for("CASE-001") as lake:
        explained = explain(
            json.dumps(ce.QUERIES["CASE-001"]),
            compiled,
            policies,
            context,
            engine_without_reads(lake),
            NO_ONE,
        )
    assert list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(explained)) == []
    for part in ("plan", "evidence", "materialization", "estimate"):
        assert part not in explained
    assert explained["question"] == {"mode": "aggregate", "withheld": True}


def test_no_estimate_under_a_row_policy() -> None:
    compiled, _, _ = corpus_case("CASE-015")
    policies = case_policies("CASE-015", compiled)
    context = builder(ce.viewer_context(attributes=ce.CALLERS["CASE-015"]))
    with lake_for("CASE-015") as lake:
        explained = explain(
            json.dumps(ce.QUERIES["CASE-015"]),
            compiled,
            policies,
            context,
            engine_without_reads(lake),
            CallerViewer(compiled, policies, context),
        )
    assert explained["outcome"] == "planned"
    assert "estimate" not in explained


def test_no_explanation_path_names_a_read() -> None:
    """The boundary check (#364): neither `explain` nor `prepare` names anything that reads,
    checks zones or executes; every read is `_execute`'s, which only `answer` calls."""
    import inspect

    from orvex.semantic import execution, explanation

    for function in (explanation.explain, execution.prepare):
        source = inspect.getsource(function)
        for name in (".read(", "_read(", "_execute(", "_check_zones(", "_unregistered("):
            assert name not in source, (function.__name__, name)
    module = inspect.getsource(explanation)
    assert "_execute" not in module
    assert "answer(" not in module


def test_a_refusal_for_another_audience_shows_only_public_details() -> None:
    """A non-builder that sees nothing: the refusal in the `other` audience, public details
    only, the question withheld, and no builder part."""
    document = windows.query("moving", group_by=("daily.day",))
    policies = no_policies(windows.DAILY)
    context = ce.viewer_context()  # a viewer, not a builder
    with windows.lake_of(windows.DAYS, windows.DAILY_CATALOG) as lake:
        explained = explain(
            json.dumps(document),
            windows.DAILY,
            policies,
            context,
            engine_without_reads(lake),
            NO_ONE,
        )
    assert list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(explained)) == []
    assert explained["outcome"] == "refused"
    refusal = explained["refusal"]
    assert refusal["audience"] == "other"
    codes = json.loads((ROOT / "contracts/semantic-error/v1/codes.json").read_text("utf-8"))
    public = {k for k, v in codes[refusal["code"]]["details"].items() if v == "public"}
    assert set(refusal["details"]) <= public
    assert explained["question"] == {"mode": "aggregate", "withheld": True}
    for part in ("plan", "evidence", "materialization", "estimate"):
        assert part not in explained
