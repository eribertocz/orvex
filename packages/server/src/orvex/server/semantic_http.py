"""The semantic engine over HTTP (P2-27a, #363): `query` and `catalog` for the project the
server opened, composed from `store`, `semantic` and `access`.

- **One snapshot per request.** A request reads the lake's current snapshot; the model, its
  policies, the caller's permissions and the data are all taken at that one snapshot. A next
  detail page instead uses the snapshot its cursor pins (Owner precision 2): the sealed cursor
  names it to the server alone (`SealedForm.data_version`), the model is compiled there and the
  page is read there. A snapshot no longer kept is `PAGE_CURSOR_INVALID`, reason `expired`;
  nothing falls back to the current one. Its permissions, though, are those in force now
  (`permission_state`, #440): a policy or mapping changed since refuses the cursor.
- **The caller** comes from a `ContextSource`: in local mode, `access`'s one local Builder,
  only for a request the HTTP layer authenticated; in server mode, the session's user as the
  directory knows them at that request (`server_context`, #440). No CLI grant is used here.
- **Audit (server mode, #443).** An answer whose policies withheld every row records why
  (`semantic_audit`) before it is returned; nothing of the events reaches the response.
- **JSON and Arrow carry the same answer** (Owner precision 3): the Arrow form is the result's
  own Arrow table, whose schema metadata also holds the JSON envelope (`semantic.envelope`)
  and the receipt view (`semantic.receipt`), byte for byte as in the JSON answer.
- **Errors** are the engine's `semantic-error` v1 messages for the caller's audience, with an
  HTTP status chosen by code and category (`status_of`).
"""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any, Protocol

import pyarrow as pa
from orvex.access.audit.log import AuditLog
from orvex.access.authentication import query_limits
from orvex.access.authentication.query_limits import DEFAULT_LIMITS, RoleLimit
from orvex.access.context import InvalidContext
from orvex.access.errors import ACCESS_FORBIDDEN, AccessError
from orvex.access.local import LocalContextProvider, LocalRequest
from orvex.access.server_context import (
    Audience,
    ServerContextProvider,
    ServerRequest,
    role_of,
)
from orvex.access.storage.store import Store
from orvex.semantic.catalog.building import catalog_for_context
from orvex.semantic.errors import SemanticError
from orvex.semantic.execution import Answer, Engine, answer
from orvex.semantic.explanation import explain
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.semantic.query.binding import Limits as QueryLimits
from orvex.semantic.receipt import Key
from orvex.semantic.receipt.viewer import CallerViewer
from orvex.semantic.result import SemanticResult
from orvex.semantic.result.cursors import SealedForm
from orvex.semantic.result_cache import ResultCache
from orvex.server import evidence_records, lake_place, semantic_model
from orvex.server.identity import Identity
from orvex.server.permission_state import permission_state
from orvex.server.project import Project, ProjectLoadError, load_project
from orvex.server.query_cancel import Cancel
from orvex.server.query_slots import QuerySlots, Slot
from orvex.server.semantic_model import ExternalTables
from orvex.store.errors import StoreError
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
    """The caller's `security-context` v1 document for one request, on the policy model's
    fingerprint and the snapshot of the mapping state in force (`permission_state`)."""

    def __call__(
        self, request: object, policy_fingerprint: str, mapping_snapshot: int
    ) -> Mapping[str, Any]: ...


class LakeSource(Protocol):
    """Where the model and its data come from: a lake for one request, and the model compiled
    on one of its snapshots."""

    def session(self) -> AbstractContextManager[Lake]: ...

    def model(self, lake: Lake, snapshot: int) -> semantic_model.Loaded: ...


def local_context(
    request: object, policy_fingerprint: str, mapping_snapshot: int
) -> dict[str, Any]:
    """Local mode: the one local Builder, for an authenticated request only. The permission
    fingerprint records the mapping state the policies read (`permission_state`)."""
    if not isinstance(request, LocalRequest):
        raise InvalidContext("the request is not an authenticated local request")
    provider = LocalContextProvider(
        request.project, policy_fingerprint, {"lake": str(mapping_snapshot)}
    )
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
    # This process's result cache (#444): one per serve session, never shared or kept.
    cache: ResultCache | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Semantic:
    """The semantic routes' composition: the source, the caller and the settings; in server
    mode, the audit log the answers' events go to (#443). Local mode has none."""

    source: LakeSource
    context: ContextSource
    settings: Settings
    audit: AuditLog | None = None
    # Per-role limits (#445): a role's limits now (server mode: the instance settings; local
    # mode's one Builder: the defaults), and this process's running questions.
    limits: Callable[[str], RoleLimit] = lambda role: DEFAULT_LIMITS[role]
    slots: QuerySlots = field(default_factory=QuerySlots)
    # Who asks, before any turn or planning (#445): the slot's user and project, and the role
    # whose limit it counts against. Server mode reads the directory now.
    identity: Callable[[object], tuple[str, str, str]] = lambda request: local_identity(request)


def local_identity(request: object) -> tuple[str, str, str]:
    """Local mode's one Builder."""
    if not isinstance(request, LocalRequest):
        raise InvalidContext("the request is not an authenticated local request")
    return "local", request.project, "builder"


def server_identity(store: Store) -> Callable[[object], tuple[str, str, str]]:
    """Server mode: the session's user, the project, and their role there now."""

    def identity(request: object) -> tuple[str, str, str]:
        if not isinstance(request, ServerRequest):
            raise InvalidContext("the request is not an authenticated server request")
        role = role_of(store, request)
        if role is None:
            raise AccessError(ACCESS_FORBIDDEN, "The operation is not allowed.")
        return request.user, request.project, role

    return identity


def take_slot(semantic: Semantic, request: object) -> Slot:
    """The caller's concurrency slot, taken before the question waits for its turn or is
    planned; `QUERY_BUDGET_EXCEEDED` (`concurrent_queries`) at the role's limit, never queued."""
    user, project, role = semantic.identity(request)
    return semantic.slots.take(user, project, semantic.limits(role).max_concurrent)


DETAIL_PAGE_CAP = 10_000  # rows of one detail page at most
RESULT_CACHE_BYTES = 256 * 2**20  # this process's result cache at most (#444)
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
        cache=ResultCache(RESULT_CACHE_BYTES),
    )


# --- statuses -----------------------------------------------------------------------------------

_BY_CODE = {
    "MEMBER_NOT_FOUND": 404,
    "EVIDENCE_VIOLATED": 409,
    "MATRIX_TOO_WIDE": 413,
    "QUERY_RESULT_WITHDRAWN": 409,
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
    """The HTTP status of an engine error: a budget is 429 for time and concurrency and 413 for
    size; then by code, then by category."""
    if error.code == "QUERY_BUDGET_EXCEEDED":
        return 429 if error.details.get("budget") in {"time_ms", "concurrent_queries"} else 413
    return _BY_CODE.get(error.code, _BY_CATEGORY.get(error.category, 500))


def server_context(store: Store, audit: AuditLog) -> ContextSource:
    """Server mode (#440): the session's user as the directory knows them now, on the policy
    model and the mapping state in force now (`permission_state`), which the permission
    fingerprint records."""

    def build(request: object, policy_fingerprint: str, mapping_snapshot: int) -> dict[str, Any]:
        provider = ServerContextProvider(
            store, audit, policy_fingerprint, {"lake": str(mapping_snapshot)}
        )
        return provider.context_for(request).document()

    return build


def server_limits(store: Store, audit: AuditLog) -> Callable[[str], RoleLimit]:
    """Server mode (#445): a role's limits as the instance settings hold them now."""
    limits = query_limits.QueryLimits(store, audit)
    return lambda role: limits.current()[role]


def semantic_message(error: SemanticError, audience: Audience) -> dict[str, Any]:
    """The `semantic-error` v1 message for the caller's audience: `builder` for a Builder,
    the generic view for anyone else (#440; never fixed)."""
    document: dict[str, Any] = json.loads(error.to_json(LOCALE, audience))
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


def _policies_at(semantic: Semantic, lake: Lake, snapshot: int) -> PolicyModel | None:
    """The policies in force at `snapshot`, or None when its model is invalid or missing."""
    loaded = semantic.source.model(lake, snapshot)
    return None if loaded.model is None else _policies(loaded.model)


@dataclass(frozen=True)
class Asked:
    """One answer: the result, its receipt view, and the result in the JSON form; the snapshot
    and the context it was computed under, which its delivery checks again (#445)."""

    found: Answer
    receipt: dict[str, Any]
    snapshot: int = 0
    context: Mapping[str, Any] = field(default_factory=dict)

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


def _no_read(request: object) -> pa.RecordBatchReader:
    """An explanation's read: never called, and failing if it ever were (#364)."""
    raise ExplanationRead


class ExplanationRead(RuntimeError):
    """An explanation tried to read data: a defect, answered as an internal error."""


@dataclass(frozen=True)
class _Question:
    """One request's lake, snapshot, model, policies, caller and engine."""

    model: semantic_model.SemanticModel
    policies: PolicyModel
    context: dict[str, Any]
    engine: Engine


def _question(
    semantic: Semantic,
    lake: Lake,
    body: bytes,
    request: object,
    read: Callable[[Any], pa.RecordBatchReader],
) -> _Question:
    """Everything a question needs on the one snapshot it uses: the current one, or the one
    a next page's cursor pins (Owner precision 2)."""
    settings = semantic.settings
    snapshot = _read_snapshot(lake, settings, _after(body))
    model = _loaded(semantic, lake, snapshot)
    policies = _policies(model)
    state = permission_state(lake, snapshot, policies, lambda n: _policies_at(semantic, lake, n))
    context = dict(semantic.context(request, state.policy_fingerprint, state.mapping_snapshot))
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
        read=read,
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
        routing=model.routing,
        # The existing lakes the model was compiled on, read at those same snapshots (#399).
        adapters=model.adapters,
        cache=settings.cache,  # keyed by this request's identity, at `pinned` (#444)
    )
    return _Question(model, policies, context, engine)


def ask(semantic: Semantic, body: bytes, request: object, cancel: Cancel | None = None) -> Asked:
    """The answer to the `semantic-query` v1 document `body` for the caller of `request`, run
    within the role's run time (#445); `cancel` interrupts its managed-lake reads. The caller's
    slot is taken before (`take_slot`)."""
    with semantic.source.session() as lake:
        read = lake.read if cancel is None else cancel.reading(lake)
        question = _question(semantic, lake, body, request, read)
        compiled = question.model.compiled
        limit = semantic.limits(str(question.context["role"])).max_run_ms
        engine = question.engine
        timeout = limit if engine.timeout_ms is None else min(engine.timeout_ms, limit)
        engine = replace(engine, timeout_ms=timeout)  # the role's run time (#445)
        found = answer(body, compiled, question.policies, question.context, engine)
        viewer = CallerViewer(compiled, question.policies, question.context)
        snapshot = int(str(question.engine.snapshot["id"]))
        return Asked(found, found.receipt.view(viewer), snapshot, question.context)


def current_context(semantic: Semantic, request: object, snapshot: int) -> Mapping[str, Any]:
    """The caller's context as it is now, for an answer computed at `snapshot`: what the
    delivery step compares with the context the answer was computed under (#445). Raises as
    building it at the request does: a membership gone, a deactivated user."""
    with semantic.source.session() as lake:
        policies = _policies(_loaded(semantic, lake, snapshot))
        state = permission_state(
            lake, snapshot, policies, lambda n: _policies_at(semantic, lake, n)
        )
        return dict(semantic.context(request, state.policy_fingerprint, state.mapping_snapshot))


def explanation(semantic: Semantic, body: bytes, request: object) -> dict[str, Any]:
    """The `semantic-explain` v1 document of `body` for the caller of `request` (#364): the
    same snapshot, model, policies and caller as `ask`, and a read that is never called."""
    with semantic.source.session() as lake:
        question = _question(semantic, lake, body, request, _no_read)
        compiled = question.model.compiled
        viewer = CallerViewer(compiled, question.policies, question.context)
        return explain(body, compiled, question.policies, question.context, question.engine, viewer)


def catalog(semantic: Semantic, request: object) -> dict[str, Any]:
    """The caller's `catalog` v1 document, on the lake's current snapshot."""
    with semantic.source.session() as lake:
        snapshot = lake.current_snapshot().id
        model = _loaded(semantic, lake, snapshot)
        policies = _policies(model)
        context = semantic.context(request, policies.fingerprint, snapshot)
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
    # The enabled existing lakes, each at its served snapshot (`existing_lakes`, #399); none
    # when the development switch is off.
    existing: Callable[[], Mapping[str, ExternalTables | StoreError]] | None = None

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

    def model(
        self,
        lake: Lake,
        snapshot: int,
        external: Mapping[str, ExternalTables | StoreError] | None = None,
    ) -> semantic_model.Loaded:
        """The model at `snapshot`, each existing lake as `external` gives it (a check of a new
        snapshot) or as served now."""
        if external is None and self.existing is not None:
            external = self.existing()
        try:
            connections = load_project(self.folder).connections
        except ProjectLoadError as error:
            raise Refused(422, "PROJECT_INVALID", "The project's settings have problems") from error
        return semantic_model.load_semantic(
            self.folder,
            lake,
            snapshot,
            self.vendor_names,
            audience="builder",
            connections=connections,
            external=external,
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
    existing: Callable[[], Mapping[str, ExternalTables | StoreError]] | None = None,
) -> ProjectSource:
    """The project folder the server opened. Its settings are read again for every request, so
    the lake is always the one `project.yaml` names now. `opener`, `lock` and `locked` are the
    lake's own opening and the project lock, which `cli` passes in: the HTTP code imports no
    command module. `existing`: the enabled existing lakes at their served snapshots."""
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
        existing=existing,
    )
