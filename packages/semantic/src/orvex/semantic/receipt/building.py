"""The receipt of a semantic result (`receipt` v1, P2-07).

The server keeps one immutable `ReceiptRecord` per result. Only views leave it:
- `summary`, for every caller;
- `builder`, for a caller with builder permission.

Each view is built item by item, by allow-list, and every item passes the viewer's own
visibility. A view is never the record with fields removed afterwards. Nothing the record is
given holds a predicate, a bound parameter, a mapping value or a value-set size, and a policy's
mapping table is kept only in the record, never in a view.

- `receipt_id` is the record's fingerprint: its identity, which anyone can recompute.
- The seal, an HMAC-SHA-256 under an instance key, is its authentication.
- The result's MAC, under an instance key, lets a replay be checked without storing values.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from orvex.semantic.canonical import canonicalize, fingerprint
from orvex.semantic.ossie.extensions import full_match_validator

SCHEMA_FILE = "receipt-v1.json"
FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")
KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
POLICY_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
INSTANT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")
ROUTES = frozenset({"managed", "existing_ducklake", "live"})
APPLIED = frozenset({"restricted", "exempt", "no_rows"})
FIGURES = frozenset({"rows_scanned", "groups", "cells"})
SEAL_DOMAIN = b"receipt-seal/v1"
RESULT_DOMAIN = b"result-mac/v1"


class InvalidReceipt(ValueError):
    """A receipt that cannot be built as asked: an internal error, never a user's."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


@cache
def schema_validator() -> Draft202012Validator:
    schema = json.loads(files("orvex.semantic.receipt").joinpath(SCHEMA_FILE).read_bytes())
    return full_match_validator(schema)


def _require(condition: bool, reason: str, message: str) -> None:
    if not condition:
        raise InvalidReceipt(reason, message)


@dataclass(frozen=True, slots=True)
class Key:
    """An instance key, supplied by `server`; never read from the environment here."""

    key_id: str
    secret: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _require(
            isinstance(self.key_id, str) and bool(KEY_ID.fullmatch(self.key_id)),
            "key",
            "a key has an id",
        )
        _require(
            isinstance(self.secret, bytes) and len(self.secret) >= 32,
            "key",
            "a key has 32 bytes or more",
        )

    def mac(self, domain: bytes, data: bytes) -> dict[str, str]:
        """HMAC-SHA-256 of `data` under this key, separated by `domain`, so one key given for
        two purposes never makes a seal and a result MAC interchangeable."""
        return {
            "key_id": self.key_id,
            "mac": hmac.new(self.secret, domain + b"\n" + data, hashlib.sha256).hexdigest(),
        }


@dataclass(frozen=True, slots=True)
class ModelUse:
    model_fingerprint: str
    reader: str
    experimental: bool = False
    commit: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyUse:
    """A policy that applied: its name, kind and how. Nothing else about it is ever given."""

    name: str
    kind: str
    applied: str


@dataclass(frozen=True, slots=True)
class DataRead:
    """A data version read for a dataset of the question, or for a policy (`for_policy`), whose
    mapping `table` stays in the record."""

    name: str
    route: str
    retained: bool
    for_policy: bool = False
    snapshot_id: int | None = None
    committed_at: str | None = None
    project: str | None = None
    connection: str | None = None
    read_at: str | None = None
    table: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceUse:
    dataset: str
    kind: str
    status: str
    verified_by_run: str | None = None
    verified_at: str | None = None


@dataclass(frozen=True, slots=True)
class Obligation:
    """`members` and `datasets` are every member and dataset the obligation names (P2-11,
    #310 §3). A view shows it only when they are declared and the viewer sees each one; its
    evidence only when the viewer sees every dataset. They never reach a view themselves."""

    rule: str
    obligation: str
    evidence: str | None = None
    members: tuple[str, ...] | None = None
    datasets: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class ExecutionUse:
    engine: str
    engine_version: str
    adapter: str
    adapter_version: str
    started_at: str
    duration_ms: int
    cache: str = "miss"
    engine_available: bool = True
    preaggregation: str | None = None
    estimate: Mapping[str, int] | None = None
    actual: Mapping[str, int] | None = None


@dataclass(frozen=True, slots=True)
class FreshnessUse:
    dataset: str
    as_of: str


class Viewer(Protocol):
    """Who reads a view: whether they hold builder permission, and what they may know."""

    @property
    def builder(self) -> bool: ...

    def sees_member(self, member: str) -> bool: ...

    def sees_value(self, member: str, value: Mapping[str, object]) -> bool: ...

    def sees_dataset(self, dataset: str) -> bool: ...

    def knows_policy(self, name: str) -> bool: ...


class _NoOne:
    """The viewer for anything that is not a well-formed viewer: no builder detail, nothing
    visible."""

    builder = False

    def sees_member(self, member: str) -> bool:
        return False

    def sees_value(self, member: str, value: Mapping[str, object]) -> bool:
        return False

    def sees_dataset(self, dataset: str) -> bool:
        return False

    def knows_policy(self, name: str) -> bool:
        return False


NO_ONE = _NoOne()


def _asks(viewer: object, method: str, *arguments: object) -> bool:
    """A viewer's answer; anything but exactly `True`, or a failure to answer, is no."""
    try:
        answer = getattr(viewer, method)(*arguments)
    except Exception:  # noqa: BLE001 - a broken viewer sees nothing
        return False
    return answer is True


def _is_builder(viewer: object) -> bool:
    try:
        return getattr(viewer, "builder", False) is True
    except Exception:  # noqa: BLE001
        return False


# --- the question, restricted to the viewer ------------------------------------------------


PREDICATE_PLAIN = (
    "op",
    "include_null",
    "exclude_null",
    "unit",
    "offset",
    "count",
    "to_date",
    "segment",
    "hierarchy",
)
PREDICATE_ONE_VALUE = ("gte", "gt", "lte", "lt")
PREDICATE_VALUES = ("values", "nodes")


def _named(entry: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    """`entry`'s members named in `keys`, and only those: the allow-list of a view."""
    return {key: entry[key] for key in keys if key in entry}


def _predicate(member: str, predicate: Mapping[str, Any], viewer: object) -> dict[str, Any] | None:
    """The predicate rebuilt from its allowed members, or None when it holds a value the
    viewer may not see or a member this view does not know."""
    known = {*PREDICATE_PLAIN, *PREDICATE_ONE_VALUE, *PREDICATE_VALUES}
    if not set(predicate) <= known:
        return None
    values = [predicate[key] for key in PREDICATE_ONE_VALUE if key in predicate]
    values += [value for key in PREDICATE_VALUES for value in predicate.get(key, ())]
    if not all(_asks(viewer, "sees_value", member, value) for value in values):
        return None
    return _named(predicate, [*PREDICATE_PLAIN, *PREDICATE_ONE_VALUE, *PREDICATE_VALUES])


def _filters(
    entries: Sequence[Mapping[str, Any]], viewer: object, cut: list[bool]
) -> list[dict[str, Any]]:
    kept = []
    for entry in entries:
        member = entry["member"]
        if not _asks(viewer, "sees_member", member):
            cut.append(True)
            continue
        shown = _named(entry, ("scope", "member", "role"))
        predicate = _predicate(member, entry["predicate"], viewer)
        if predicate is None:
            shown["withheld"] = True
            cut.append(True)
        else:
            shown["predicate"] = predicate
        kept.append(shown)
    return kept


def question(canonical: Mapping[str, Any], viewer: object) -> dict[str, Any]:
    """The canonical query restricted to what `viewer` may know (README, "Views"). Every entry
    is rebuilt from the members this view allows; parameters never appear."""

    def sees(member: str) -> bool:
        return _asks(viewer, "sees_member", member)

    cut: list[bool] = []

    def keep(
        entries: Sequence[Mapping[str, Any]],
        keys: Sequence[str],
        members: Callable[[Mapping[str, Any]], list[str]],
    ) -> list[dict[str, Any]]:
        kept = []
        for entry in entries:
            if all(sees(member) for member in members(entry)):
                kept.append(_named(entry, keys))
            else:
                cut.append(True)
        return kept

    shown: dict[str, Any] = {"mode": canonical["mode"]}
    shown.update(_named(canonical, ("coverage", "limit", "as_known_at")))
    if "page" in canonical:
        shown["page"] = {"size": canonical["page"]["size"]}
    if "parameters" in canonical:
        cut.append(True)  # parameter values are never shown (Owner decision of 2026-11-02)
    group_by = keep(canonical.get("group_by", ()), ("member", "role"), lambda e: [e["member"]])
    for entry, source in zip(
        group_by, [e for e in canonical.get("group_by", ()) if sees(e["member"])], strict=True
    ):
        if "bucket" in source:
            entry["bucket"] = _named(source["bucket"], ("unit", "calendar"))
    metric_filters = keep(
        canonical.get("metric_filters", ()),
        ("scope", "metric", "dimension", "grain"),
        lambda e: [e["metric"], e["dimension"], *e.get("grain", ())],
    )
    for entry, source in zip(
        metric_filters,
        [
            e
            for e in canonical.get("metric_filters", ())
            if all(sees(m) for m in [e["metric"], e["dimension"], *e.get("grain", ())])
        ],
        strict=True,
    ):
        entry["condition"] = _named(source["condition"], ("op", "value", "n"))
    lists: dict[str, list[dict[str, Any]]] = {
        "metrics": keep(
            canonical.get("metrics", ()), ("metric", "context", "apply"), lambda e: [e["metric"]]
        ),
        "group_by": group_by,
        "fields": keep(canonical.get("fields", ()), ("member", "role"), lambda e: [e["member"]]),
        "order": keep(
            canonical.get("order", ()), ("by", "direction", "nulls"), lambda e: [e["by"]]
        ),
        "metric_filters": metric_filters,
        "filters": _filters(canonical.get("filters", ()), viewer, cut),
    }
    contexts = []
    for context in canonical.get("contexts", ()):
        filters = _filters(context["filters"], viewer, cut)
        if filters:
            contexts.append({"name": context["name"], "filters": filters})
        else:
            cut.append(True)
    lists["contexts"] = contexts
    modifiers = []
    for modifier in canonical.get("context_modifiers", ()):
        members = [member for member in modifier["members"] if sees(member)]
        if len(members) != len(modifier["members"]):
            cut.append(True)
        if members:
            modifiers.append({**_named(modifier, ("op", "scope")), "members": members})
    lists["context_modifiers"] = modifiers
    for key, entries in lists.items():
        if entries:
            shown[key] = entries
    if "top_n" in canonical:
        top = canonical["top_n"]
        if all(sees(member) for member in [top["by"], *top.get("per", ())]):
            shown["top_n"] = _named(top, ("by", "n", "per", "others"))
        else:
            cut.append(True)
    if cut:
        shown["withheld"] = True
    return shown


# --- the record ----------------------------------------------------------------------------


def _check_inputs(
    query_canonical: Mapping[str, Any],
    query_fingerprint: str,
    model: ModelUse,
    permission_fingerprint: str,
    policies: Sequence[PolicyUse],
    data: Sequence[DataRead],
    execution: ExecutionUse,
) -> None:
    _require(
        fingerprint("semantic_query", 1, query_canonical) == query_fingerprint,
        "fingerprint",
        "the query fingerprint is not the canonical query's",
    )
    for name, value in (
        ("model_fingerprint", model.model_fingerprint),
        ("permission_fingerprint", permission_fingerprint),
    ):
        _require(isinstance(value, str) and bool(FINGERPRINT.fullmatch(value)), "fingerprint", name)
    for policy in policies:
        _require(
            isinstance(policy.name, str) and bool(POLICY_NAME.fullmatch(policy.name)),
            "policy",
            "a policy is given by its name only",
        )
        _require(
            policy.kind in {"row", "column"} and policy.applied in APPLIED, "policy", policy.name
        )
    _require(bool(data), "data", "a result reads at least one data version")
    for read in data:
        _require(read.route in ROUTES, "data_route", f"{read.name}: route {read.route!r}")
        if read.route == "live":
            _require(read.read_at is not None and read.snapshot_id is None, "data_route", read.name)
            _require(
                read.retained is False, "data_route", f"{read.name}: a live read is never retained"
            )
        else:
            _require(read.snapshot_id is not None, "data_route", f"{read.name}: no snapshot")
        if read.route == "existing_ducklake":
            _require(
                None not in (read.project, read.connection, read.committed_at),
                "data_route",
                f"{read.name}: an external snapshot needs its project, connection and commit time",
            )
        if read.for_policy:
            _require(
                bool(POLICY_NAME.fullmatch(read.name)),
                "data_route",
                "a policy read is named by its policy",
            )
    for figures in (execution.estimate, execution.actual):
        _require(figures is None or set(figures) <= FIGURES, "execution", "unknown figures")
    _require(execution.cache in {"hit", "miss", "bypass"}, "execution", "cache")


def _reproducibility(
    model: ModelUse, data: Sequence[DataRead], execution: ExecutionUse
) -> dict[str, Any]:
    reasons = []
    if any(read.route != "live" and not read.retained for read in data):
        reasons.append("snapshot_expired")
    if any(read.route == "live" for read in data):
        reasons.append("live_read")
    if model.experimental:
        reasons.append("experimental_reader")
    if not execution.engine_available:
        reasons.append("engine_unavailable")
    if reasons:
        return {"class": "explainable", "reasons": reasons, "replay": "not_checked"}
    return {"class": "exact", "replay": "not_checked"}


def _version(read: DataRead) -> dict[str, Any]:
    version: dict[str, Any] = {
        "for": "policy" if read.for_policy else "dataset",
        "name": read.name,
        "route": read.route,
    }
    if read.route == "live":
        version["read_at"] = read.read_at
    else:
        snapshot: dict[str, Any] = {"id": read.snapshot_id}
        for key in ("committed_at", "project", "connection"):
            if getattr(read, key) is not None:
                snapshot[key] = getattr(read, key)
        version["snapshot"] = snapshot
    return version


def execution_fingerprint(
    model_fingerprint: str,
    query_fingerprint: str,
    permission_fingerprint: str,
    data: Sequence[DataRead],
    execution: ExecutionUse,
) -> str:
    """The execution identity (ADR-0030 §6): never the cache origin, timings or the result."""
    versions = sorted((_version(read) for read in data), key=canonicalize)
    return fingerprint(
        "execution",
        1,
        {
            "model": model_fingerprint,
            "query": query_fingerprint,
            "permissions": permission_fingerprint,
            "data": versions,
            "engine": [execution.engine, execution.engine_version],
            "adapter": [execution.adapter, execution.adapter_version],
        },
    )


def _unordered(items: list[Any]) -> list[Any]:
    """An unordered collection in one fixed order, so the record's identity never depends on
    the order it was given in."""
    return sorted(items, key=canonicalize)


def _plain(value: object) -> Any:  # noqa: ANN401 - a JSON document
    """A dataclass input as a JSON document, `None` members left out."""
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _plain(getattr(value, name))
            for name in value.__dataclass_fields__
            if getattr(value, name) is not None
        }
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


class ReceiptRecord:
    """The server's record of one result. It never leaves the server: only views do."""

    __slots__ = ("_document", "_seal", "receipt_id")

    def __init__(self, document: dict[str, Any], seal_key: Key) -> None:
        canonical = canonicalize(document)
        self._document = canonical
        self.receipt_id = fingerprint("receipt", 1, document)
        self._seal = seal_key.mac(SEAL_DOMAIN, canonical)

    def document(self) -> dict[str, Any]:
        found: dict[str, Any] = json.loads(self._document)
        return found

    @property
    def reproducibility(self) -> dict[str, Any]:
        found: dict[str, Any] = self.document()["reproducibility"]
        return found

    def canonical(self) -> bytes:
        return self._document

    def view(self, viewer: object = NO_ONE) -> dict[str, Any]:
        """The view for `viewer`: `summary`, or `builder` when the viewer holds builder
        permission. Every item is chosen by allow-list and passes the viewer's visibility."""
        record = self.document()

        def datasets(name: str) -> bool:
            return _asks(viewer, "sees_dataset", name)

        def knows(name: str) -> bool:
            return _asks(viewer, "knows_policy", name)

        builder = _is_builder(viewer)
        policies = [p for p in record["security"]["policies"] if knows(p["name"])]
        view: dict[str, Any] = {
            "kind": "receipt",
            "version": 1,
            "view": "builder" if builder else "summary",
            "receipt_id": self.receipt_id,
            "seal": dict(self._seal),
            "query_fingerprint": record["query"]["fingerprint"],
            "question": question(record["query"]["canonical"], viewer),
            "security": {"restricted": bool(record["security"]["policies"]), "policies": policies},
            "freshness": [f for f in record["freshness"] if datasets(f["dataset"])],
            "reproducibility": record["reproducibility"],
            "cache": record["execution"]["cache"],
        }
        if not builder:
            return view
        execution = {
            key: record["execution"][key]
            for key in (
                "engine",
                "engine_version",
                "adapter",
                "adapter_version",
                "preaggregation",
                "started_at",
                "duration_ms",
                "actual",
            )
            if key in record["execution"]
        }
        # An exact estimate on a dataset with row policies would reveal its size (#238 rule 12).
        if "estimate" in record["execution"] and not any(
            p["kind"] == "row" for p in record["security"]["policies"]
        ):
            execution["estimate"] = record["execution"]["estimate"]
        if any(p["kind"] == "row" for p in record["security"]["policies"]):
            execution.pop("actual", None)
        data = []
        for read in record["data"]:
            visible = knows(read["name"]) if read["for_policy"] else datasets(read["name"])
            if visible:
                shown = _version(DataRead(**{k: v for k, v in read.items() if k != "table"}))
                shown["retained"] = read["retained"]
                data.append(shown)
        view.update(
            model={
                k: record["model"][k]
                for k in ("model_fingerprint", "reader", "experimental", "commit")
                if k in record["model"]
            },
            plan=_plan(record["plan"], viewer),
            evidence=[e for e in record["evidence"] if datasets(e["dataset"])],
            data=data,
            execution=execution,
            execution_fingerprint=record["execution_fingerprint"],
            result=record["result"],
        )
        return view


OBLIGATION_FIELDS = ("rule", "obligation")  # what a view shows of an obligation; evidence aside


def _plan(plan: Mapping[str, Any], viewer: object) -> dict[str, Any]:
    """The plan as `viewer` may know it: each obligation rebuilt from `OBLIGATION_FIELDS`, and
    left out when it does not declare what it names or names anything the viewer cannot see."""
    shown: dict[str, Any] = {}
    if "plan_fingerprint" in plan:
        shown["plan_fingerprint"] = plan["plan_fingerprint"]
    obligations = []
    for obligation in plan["obligations"]:
        members, datasets = obligation.get("members"), obligation.get("datasets")
        if members is None or datasets is None:
            continue
        if not all(_asks(viewer, "sees_member", m) for m in members):
            continue
        if not all(_asks(viewer, "sees_dataset", d) for d in datasets):
            continue
        entry = {k: obligation[k] for k in OBLIGATION_FIELDS}
        if "evidence" in obligation and datasets:
            entry["evidence"] = obligation["evidence"]
        obligations.append(entry)
    shown["obligations"] = obligations
    return shown


def build_receipt(
    *,
    query_canonical: Mapping[str, Any],
    query_fingerprint: str,
    model: ModelUse,
    permission_fingerprint: str,
    data: Sequence[DataRead],
    execution: ExecutionUse,
    result_canonical: bytes,
    result_key: Key,
    seal_key: Key,
    policies: Sequence[PolicyUse] = (),
    evidence: Sequence[EvidenceUse] = (),
    obligations: Sequence[Obligation] = (),
    plan_fingerprint: str | None = None,
    freshness: Sequence[FreshnessUse] = (),
) -> ReceiptRecord:
    """The record of one result, or `InvalidReceipt`. Its reproducibility is derived from the
    data versions, the reader and the engine, never given."""
    _check_inputs(
        query_canonical, query_fingerprint, model, permission_fingerprint, policies, data, execution
    )
    _require(
        isinstance(result_canonical, bytes) and bool(result_canonical),
        "result",
        "no canonical result",
    )
    for instant in [execution.started_at, *(f.as_of for f in freshness)]:
        _require(bool(INSTANT.fullmatch(instant)), "instant", f"{instant!r} is not a UTC instant")
    document: dict[str, Any] = {
        "query": {"canonical": dict(query_canonical), "fingerprint": query_fingerprint},
        "model": _plain(model),
        "plan": {"obligations": _unordered(_plain(list(obligations)))}
        | ({"plan_fingerprint": plan_fingerprint} if plan_fingerprint else {}),
        "security": {
            "permission_fingerprint": permission_fingerprint,
            "policies": _unordered(_plain(list(policies))),
        },
        "evidence": _unordered(_plain(list(evidence))),
        "data": _unordered(
            [
                {**_plain(read), "for_policy": read.for_policy, "retained": read.retained}
                for read in data
            ]
        ),
        "freshness": _unordered(_plain(list(freshness))),
        "execution": _plain(execution),
        "execution_fingerprint": execution_fingerprint(
            model.model_fingerprint, query_fingerprint, permission_fingerprint, data, execution
        ),
        "result": result_key.mac(RESULT_DOMAIN, result_canonical),
        "reproducibility": _reproducibility(model, data, execution),
    }
    record = ReceiptRecord(document, seal_key)
    for viewer in (NO_ONE, _Everything()):
        problems = list(schema_validator().iter_errors(record.view(viewer)))
        if problems:
            where = "/".join(str(part) for part in problems[0].absolute_path)
            raise InvalidReceipt("schema", f"/{where}: {problems[0].message}")
    return record


class _Everything:
    """A builder who sees everything: used only to check that every view fits the schema."""

    builder = True

    def sees_member(self, member: str) -> bool:
        return True

    def sees_value(self, member: str, value: Mapping[str, object]) -> bool:
        return True

    def sees_dataset(self, dataset: str) -> bool:
        return True

    def knows_policy(self, name: str) -> bool:
        return True


def verify_receipt(
    view: Mapping[str, Any], record: ReceiptRecord, seal_key: Key, viewer: object
) -> bool:
    """Whether `view` is genuine: the record's seal holds under `seal_key`, and `view` is
    exactly the record's view for `viewer`."""
    expected = seal_key.mac(SEAL_DOMAIN, record.canonical())
    seal = view.get("seal")
    if not isinstance(seal, Mapping) or seal.get("key_id") != expected["key_id"]:
        return False
    if not hmac.compare_digest(str(seal.get("mac", "")), expected["mac"]):
        return False
    return canonicalize(dict(view)) == canonicalize(record.view(viewer))


def verify_replay(
    record: ReceiptRecord, replayed_canonical: bytes, result_key: Key, seal_key: Key
) -> ReceiptRecord:
    """A new record whose replay state is `verified` when the replayed result's canonical form
    gives the record's result MAC under `result_key`, and `failed` otherwise. Only an `exact`
    record can be replayed."""
    document = record.document()
    _require(
        document["reproducibility"]["class"] == "exact", "replay", "only an exact record replays"
    )
    expected = document["result"]
    given = result_key.mac(RESULT_DOMAIN, replayed_canonical)
    same = given["key_id"] == expected["key_id"] and hmac.compare_digest(
        given["mac"], expected["mac"]
    )
    document["reproducibility"] = {
        **document["reproducibility"],
        "replay": "verified" if same else "failed",
    }
    return ReceiptRecord(document, seal_key)


__all__ = [
    "NO_ONE",
    "DataRead",
    "EvidenceUse",
    "ExecutionUse",
    "FreshnessUse",
    "InvalidReceipt",
    "Key",
    "ModelUse",
    "Obligation",
    "PolicyUse",
    "ReceiptRecord",
    "Viewer",
    "build_receipt",
    "execution_fingerprint",
    "question",
    "verify_receipt",
    "verify_replay",
]
