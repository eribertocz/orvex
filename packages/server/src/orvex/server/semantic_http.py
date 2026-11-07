"""The semantic engine over HTTP (P2-27a, #363): `query` and `catalog` for the project the
server opened, composed from `store`, `semantic` and `access`.

- **One snapshot per request.** A request reads the lake's current snapshot; the model, its
  policies, the caller's permissions and the data are all taken at that one snapshot. A next
  detail page instead uses the snapshot its cursor pins (Owner precision 2): the sealed cursor
  names it to the server alone (`SealedForm.data_version`), the model is compiled there and the
  page is read there. A snapshot no longer kept is `PAGE_CURSOR_INVALID`, reason `expired`;
  nothing falls back to the current one.
- **The caller** comes from a `ContextSource`: in local mode, `access`'s one local Builder,
  only for a request the HTTP layer authenticated. No CLI grant is used here.
- **JSON and Arrow carry the same answer** (Owner precision 3): the Arrow form is the result's
  own Arrow table, whose schema metadata also holds the JSON envelope (`semantic.envelope`)
  and the receipt view (`semantic.receipt`), byte for byte as in the JSON answer.
- **Errors** are the engine's `semantic-error` v1 messages for the builder audience, with an
  HTTP status chosen by code and category (`status_of`).
"""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any, Protocol

import pyarrow as pa
from orvex.access.local import LocalContextProvider, LocalRequest
from orvex.semantic.catalog.building import catalog_for_context
from orvex.semantic.errors import SemanticError
from orvex.semantic.execution import Answer, Engine, answer
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.semantic.query.binding import Limits as QueryLimits
from orvex.semantic.receipt import Key
from orvex.semantic.receipt.viewer import CallerViewer
from orvex.semantic.result import SemanticResult
from orvex.semantic.result.cursors import SealedForm
from orvex.server import evidence_records, lake_place, semantic_model
from orvex.server.identity import Identity
from orvex.server.project import Project, ProjectLoadError, load_project
from orvex.store.json_encoding import arrow_to_json
from orvex.store.lake import Lake

ARROW = "application/vnd.apache.arrow.stream"
ENVELOPE_KEY = b"semantic.envelope"
RECEIPT_KEY = b"semantic.receipt"
LOCALE = "es"
TURN_SECONDS = 30  # how long a request waits for the one before it in this process
NO_POLICIES = b"version: 1\n"  # a model without `security.yaml`: no policy, every member visible


class Refused(Exception):
    """A request the API refuses with `status` and `code`, outside the engine's own errors."""

    def __init__(
        self, status: int, code: str, message: str, details: Sequence[tuple[str, str]] = ()
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.details = tuple(details)


class ContextSource(Protocol):
    """The caller's `security-context` v1 document for one request, policies and snapshot."""

    def __call__(
        self, request: LocalRequest, policies: PolicyModel, snapshot: int
    ) -> Mapping[str, Any]: ...


class LakeSource(Protocol):
    """Where the model and its data come from: a lake for one request, and the model compiled
    on one of its snapshots."""

    def session(self) -> AbstractContextManager[Lake]: ...

    def model(self, lake: Lake, snapshot: int) -> semantic_model.Loaded: ...


def local_context(request: LocalRequest, policies: PolicyModel, snapshot: int) -> dict[str, Any]:
    """Local mode: the one local Builder, for an authenticated request only. Every managed table
    the policies may read is read on `snapshot`, which the permission fingerprint records."""
    provider = LocalContextProvider(request.project, policies.fingerprint, {"lake": str(snapshot)})
    return provider.context_for(request).document()


@dataclass(frozen=True)
class Settings:
    """What the server fixed for every question of this serve session."""

    result_key: Key = field(repr=False)
    seal_key: Key = field(repr=False)
    cursors: SealedForm = field(repr=False)
    limits: QueryLimits
    timeout_ms: int | None
    engine_version: str = field(default_factory=lambda: version("duckdb"))
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True)
class Semantic:
    """The semantic routes' composition: the source, the caller and the settings."""

    source: LakeSource
    context: ContextSource
    settings: Settings


DETAIL_PAGE_CAP = 10_000  # rows of one detail page at most
QUERY_TIMEOUT_MS = 120_000


def session_settings() -> Settings:
    """Keys of this serve session only: a receipt sealed or a cursor issued before a restart
    no longer verifies, and a cursor of another session is `invalid`."""
    return Settings(
        result_key=Key("session-result", secrets.token_bytes(32)),
        seal_key=Key("session-seal", secrets.token_bytes(32)),
        cursors=SealedForm(secrets.token_bytes(32)),
        limits=QueryLimits(detail_page_size=DETAIL_PAGE_CAP),
        timeout_ms=QUERY_TIMEOUT_MS,
    )


# --- statuses -----------------------------------------------------------------------------------

_BY_CODE = {
    "MEMBER_NOT_FOUND": 404,
    "EVIDENCE_VIOLATED": 409,
    "MATRIX_TOO_WIDE": 413,
    "ENGINE_VERSION_UNTESTED": 503,
}
_BY_CATEGORY = {
    "query": 400,
    "model": 422,
    "correctness": 422,
    "security": 403,
    "engine": 422,
}


def status_of(error: SemanticError) -> int:
    """The HTTP status of an engine error: a budget is 429 for time and 413 for size; then by
    code, then by category."""
    if error.code == "QUERY_BUDGET_EXCEEDED":
        return 429 if error.details.get("budget") == "time_ms" else 413
    return _BY_CODE.get(error.code, _BY_CATEGORY.get(error.category, 500))


def semantic_message(error: SemanticError) -> dict[str, Any]:
    """The `semantic-error` v1 message, builder audience (local mode)."""
    document: dict[str, Any] = json.loads(error.to_json(LOCALE, "builder"))
    return document


# --- one request --------------------------------------------------------------------------------


def _policies(model: semantic_model.SemanticModel) -> PolicyModel:
    if model.policies is not None:
        return model.policies
    return read_policies(NO_POLICIES, model.compiled, groups=frozenset(), managed_tables={})


def _snapshot_document(lake: Lake, snapshot_id: int) -> dict[str, object] | None:
    found = lake.snapshot(snapshot_id)
    if found is None:
        return None
    return {"id": found.id, "committed_at": found.committed_at.isoformat()}


def _after(body: bytes) -> object:
    """The query's `page.after`, when the body says one; binding checks everything else."""
    try:
        document = json.loads(body)
    except ValueError, UnicodeDecodeError:
        return None
    page = document.get("page") if isinstance(document, dict) else None
    return page.get("after") if isinstance(page, dict) else None


def _read_snapshot(lake: Lake, settings: Settings, after: object) -> int:
    """The snapshot this request reads: the current one, or the one a next page's cursor pins
    while the lake keeps it."""
    current = lake.current_snapshot().id
    if after is None:
        return current
    pinned = settings.cursors.data_version(after)
    if pinned is None or not pinned.isdigit():
        raise SemanticError("PAGE_CURSOR_INVALID", {"reason": "invalid"})
    number = int(pinned)
    if lake.snapshot(number) is None:
        reason = "expired" if number <= current else "invalid"
        raise SemanticError("PAGE_CURSOR_INVALID", {"reason": reason})
    return number


def _loaded(semantic: Semantic, lake: Lake, snapshot: int) -> semantic_model.SemanticModel:
    loaded = semantic.source.model(lake, snapshot)
    if loaded.model is not None:
        return loaded.model
    if not loaded.problems:
        raise Refused(404, "SEMANTIC_MODEL_NOT_FOUND", "The project has no semantic model")
    raise Refused(
        422,
        "SEMANTIC_MODEL_INVALID",
        "The project's semantic model has problems",
        [(problem.file, str(problem)) for problem in loaded.problems],
    )


@dataclass(frozen=True)
class Asked:
    """One answer: the result, its receipt view, and the result in the JSON form."""

    found: Answer
    receipt: dict[str, Any]

    def json_document(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.found.result.wire(arrow_to_json))
        return {"result": result, "receipt": self.receipt}

    def arrow_table(self) -> pa.Table:
        return arrow_answer(self.found.result, self.receipt)


def arrow_answer(result: SemanticResult, receipt: Mapping[str, Any]) -> pa.Table:
    """The result's Arrow form, with the JSON envelope and the receipt view added to the schema
    metadata: the same bytes the JSON answer holds, rows apart."""
    table = result.arrow()
    metadata = dict(table.schema.metadata or {})
    metadata[ENVELOPE_KEY] = _canonical(result.envelope())
    metadata[RECEIPT_KEY] = _canonical(receipt)
    return table.replace_schema_metadata(metadata)


def _canonical(document: Mapping[str, Any]) -> bytes:
    from orvex.semantic.canonical import canonicalize

    return canonicalize(dict(document))


def ask(semantic: Semantic, body: bytes, request: LocalRequest) -> Asked:
    """The answer to the `semantic-query` v1 document `body` for the caller of `request`."""
    settings = semantic.settings
    with semantic.source.session() as lake:
        snapshot = _read_snapshot(lake, settings, _after(body))
        model = _loaded(semantic, lake, snapshot)
        policies = _policies(model)
        context = dict(semantic.context(request, policies, snapshot))
        pinned = _snapshot_document(lake, snapshot)
        if pinned is None:  # expired between the two calls: never another snapshot
            raise SemanticError("PAGE_CURSOR_INVALID", {"reason": "expired"})
        evidence, unchanged = evidence_records.engine_evidence(lake)

        def kept(data_version: str) -> Mapping[str, object] | None:
            # Only the snapshot this request compiled at: a cursor of any other is refused.
            if data_version != str(snapshot):
                return None
            return _snapshot_document(lake, snapshot)

        engine = Engine(
            read=lake.read,
            encoder=arrow_to_json,
            snapshot=pinned,
            engine_version=settings.engine_version,
            result_key=settings.result_key,
            seal_key=settings.seal_key,
            instant=settings.now(),
            limits=settings.limits,
            timeout_ms=settings.timeout_ms,
            cursors=settings.cursors,
            pinned=kept,
            evidence=evidence,
            unchanged=unchanged,
            statistics=lake.statistics,
        )
        found = answer(body, model.compiled, policies, context, engine)
        viewer = CallerViewer(model.compiled, policies, context)
        return Asked(found, found.receipt.view(viewer))


def catalog(semantic: Semantic, request: LocalRequest) -> dict[str, Any]:
    """The caller's `catalog` v1 document, on the lake's current snapshot."""
    with semantic.source.session() as lake:
        snapshot = lake.current_snapshot().id
        model = _loaded(semantic, lake, snapshot)
        policies = _policies(model)
        context = semantic.context(request, policies, snapshot)
        return catalog_for_context(model.compiled, policies, context)


# --- the project's lake -------------------------------------------------------------------------


class ProjectBusy(Exception):
    """A run holds the project's lake."""


@dataclass
class ProjectSource:
    """The project the server opened: its lake under the project lock for each request, and
    its semantic model compiled at the snapshot asked (#358). Requests of this process take
    turns; a run of another process makes a request wait for nothing: it is refused busy."""

    folder: Path
    home: Path
    open_lake: Callable[[], Lake]
    lock: Callable[[], AbstractContextManager[None]]
    locked: type[Exception]  # what `lock` raises while a run holds the lake
    vendor_names: tuple[str, ...]
    turn: threading.Lock = field(default_factory=threading.Lock)

    @contextmanager
    def session(self) -> Iterator[Lake]:
        if not self.turn.acquire(timeout=TURN_SECONDS):
            raise ProjectBusy
        try:
            with ExitStack() as stack:
                try:
                    stack.enter_context(self.lock())
                except self.locked:
                    raise ProjectBusy from None
                lake = self.open_lake()
                stack.callback(lake.close)
                yield lake
        finally:
            self.turn.release()

    def model(self, lake: Lake, snapshot: int) -> semantic_model.Loaded:
        return semantic_model.load_semantic(
            self.folder, lake, snapshot, self.vendor_names, audience="builder"
        )


class Opener(Protocol):
    """Opens the project's existing lake at `home` with the settings of `project`."""

    def __call__(self, project: Project, home: Path) -> Lake: ...


def project_source(
    folder: Path,
    identity: Identity,
    opener: Opener,
    lock: Callable[[Path], AbstractContextManager[None]],
    locked: type[Exception],
) -> ProjectSource:
    """The project folder the server opened. Its settings are read again for every request, so
    the lake is always the one `project.yaml` names now. `opener`, `lock` and `locked` are the
    lake's own opening and the project lock, which `cli` passes in: the HTTP code imports no
    command module."""
    home = folder / f".{identity.slug}"

    def lake() -> Lake:
        try:
            project = load_project(folder)
        except ProjectLoadError as error:
            raise Refused(422, "PROJECT_INVALID", "The project's settings have problems") from error
        return opener(project, home)

    def held() -> AbstractContextManager[None]:
        if not lake_place.exists(home):  # before the lock, which would create the folder
            raise Refused(404, "LAKE_NOT_FOUND", "The project's lake has no data yet; run it first")
        return lock(home)

    return ProjectSource(
        folder=folder,
        home=home,
        open_lake=lake,
        lock=held,
        locked=locked,
        vendor_names=(identity.ossie_vendor_name, *identity.previous_vendor_names),
    )
