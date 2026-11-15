"""The serializing codec (#486 G5): a value that crosses a contract, to bytes and back, exactly.

Every value becomes tagged JSON: exact types survive (a tuple stays a tuple, `bool` is never an
`int`), a `Decimal` keeps its scale, an integer of any width stays exact, a `datetime` keeps its
zone (an IANA zone by its key, or a fixed offset of `datetime.timezone` or pydantic's `TzInfo`,
each rebuilt as itself) and its `fold`; a naive datetime is refused. A pydantic model crosses
field by field through the same tree and is validated again when rebuilt, so nothing inside it
is weakened (its own JSON would turn an IANA zone into a fixed offset). An Arrow table crosses
as Arrow IPC, which keeps every type, NULL and time zone of its schema. Only the classes the
contract declares (`InterfaceContract.value_classes`) are built back, and only of exactly that
class. Anything else is refused with INTERFACE_VALUE_NOT_SERIALIZABLE: a live object never
crosses.
"""

from __future__ import annotations

import base64
import dataclasses
import json
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa
from pydantic import BaseModel, RootModel
from pydantic_core import TzInfo

from .aware import aware
from .interface_errors import InterfaceContractError

CODE = "INTERFACE_VALUE_NOT_SERIALIZABLE"
# The fixed-offset zones that cross, by the name their kind is recorded with.
OFFSET_KINDS: dict[type, str] = {timezone: "timezone", TzInfo: "pydantic"}


def qualified(kind: type) -> str:
    return f"{kind.__module__}.{kind.__qualname__}"


def _refused(text: str) -> InterfaceContractError:
    return InterfaceContractError(CODE, text)


class Codec:
    """Encodes and decodes the values of one contract."""

    def __init__(self, classes: frozenset[type]) -> None:
        self._classes = {qualified(kind): kind for kind in classes}

    def encode(self, value: object) -> bytes:
        return json.dumps(self._tree(value), ensure_ascii=False, separators=(",", ":")).encode()

    def decode(self, data: bytes) -> object:
        try:
            tree = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _refused(f"not an encoded value: {error}") from None
        return self._value(tree)

    def _tree(self, value: object) -> Any:  # noqa: ANN401 - a JSON tree
        kind = type(value)
        if value is None or kind in (bool, int, str):
            return value
        if kind is float:
            return {"$float": repr(value)}
        if kind is Decimal:
            return {"$decimal": str(value)}
        if kind is bytes and isinstance(value, bytes):
            return {"$bytes": base64.b64encode(value).decode()}
        if kind is datetime and isinstance(value, datetime):
            return {"$datetime": value.replace(tzinfo=None).isoformat(), **_zone(value)}
        if kind is date and isinstance(value, date):
            return {"$date": value.isoformat()}
        if kind is time and isinstance(value, time):
            if value.tzinfo is not None and type(value.tzinfo) is not timezone:
                raise _refused("a time of day carries a fixed offset or no zone")
            return {"$time": value.isoformat()}
        if kind is timedelta and isinstance(value, timedelta):
            return {"$timedelta": [value.days, value.seconds, value.microseconds]}
        if kind is pa.Table and isinstance(value, pa.Table):
            return {"$arrow": base64.b64encode(_ipc(value)).decode()}
        if kind in (tuple, list) and isinstance(value, (tuple, list)):
            return {f"${kind.__name__}": [self._tree(item) for item in value]}
        if kind is dict and isinstance(value, dict):
            if not all(type(key) is str for key in value):
                raise _refused("only `str` keys cross a contract")
            return {"$dict": {key: self._tree(item) for key, item in value.items()}}
        return self._object(value)

    def _object(self, value: object) -> Any:  # noqa: ANN401 - a JSON tree
        kind = type(value)
        if self._classes.get(qualified(kind)) is not kind:
            raise _refused(f"a {qualified(kind)} cannot cross this contract")
        name = qualified(kind)
        if isinstance(value, Enum):
            return {"$enum": name, "member": value.name}
        if isinstance(value, BaseModel):
            fields = {f: self._tree(getattr(value, f)) for f in type(value).model_fields}
            return {"$model": name, "fields": fields}
        if not dataclasses.is_dataclass(value):
            raise _refused(f"a {name} is not a value")
        fields = {f.name: self._tree(getattr(value, f.name)) for f in dataclasses.fields(value)}
        return {"$dataclass": name, "fields": fields}

    def _value(self, tree: Any) -> object:  # noqa: ANN401 - a JSON tree
        if tree is None or type(tree) in (bool, int, str):
            return tree
        if type(tree) is not dict or not tree:
            raise _refused("not an encoded value")
        tag = next(iter(tree))
        payload = tree[tag]
        try:
            return self._tagged(tag, payload, tree)
        except (ValueError, TypeError, KeyError, ArithmeticError) as error:
            raise _refused(f"not an encoded {tag}: {error}") from None

    def _tagged(self, tag: str, payload: Any, tree: dict[str, Any]) -> object:  # noqa: ANN401
        if tag == "$float":
            return float(payload)
        if tag == "$decimal":
            return Decimal(payload)
        if tag == "$bytes":
            return base64.b64decode(payload, validate=True)
        if tag == "$datetime":
            moment = datetime.fromisoformat(payload)
            return moment.replace(tzinfo=_tzinfo(tree), fold=tree.get("fold", 0))
        if tag == "$date":
            return date.fromisoformat(payload)
        if tag == "$time":
            return time.fromisoformat(payload)
        if tag == "$timedelta":
            days, seconds, microseconds = payload
            return timedelta(days=days, seconds=seconds, microseconds=microseconds)
        if tag == "$arrow":
            return pa.ipc.open_stream(base64.b64decode(payload, validate=True)).read_all()
        if tag == "$tuple":
            return tuple(self._value(item) for item in payload)
        if tag == "$list":
            return [self._value(item) for item in payload]
        if tag == "$dict":
            return {key: self._value(item) for key, item in payload.items()}
        kind = self._classes.get(payload) if type(payload) is str else None
        if kind is None:
            raise _refused(f"{tag} {payload!r} is not a class of this contract")
        if tag == "$enum" and issubclass(kind, Enum):
            return kind[tree["member"]]
        if tag == "$model" and issubclass(kind, BaseModel):
            fields = {name: self._value(item) for name, item in tree["fields"].items()}
            if issubclass(kind, RootModel):
                return kind.model_validate(fields["root"])
            return kind.model_validate(fields, by_alias=False, by_name=True)
        if tag == "$dataclass" and dataclasses.is_dataclass(kind):
            return kind(**{name: self._value(item) for name, item in tree["fields"].items()})
        raise _refused(f"{tag} does not match {payload}")


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue())


def _zone(value: datetime) -> dict[str, object]:
    """The zone of an aware datetime: an IANA zone by its key, a fixed offset with the kind of
    object that holds it (`datetime.timezone`, or the `TzInfo` pydantic parses an offset into).
    A naive datetime never crosses: time zones are explicit."""
    zone = value.tzinfo
    found: dict[str, object] = {"fold": value.fold} if value.fold else {}
    if zone is None or not aware(value):
        raise _refused("a naive datetime cannot cross a contract: its time zone is explicit")
    if type(zone) is ZoneInfo and zone.key is not None:
        return {**found, "zone": zone.key}
    if type(zone) in OFFSET_KINDS:
        offset = zone.utcoffset(None)
        if offset is None or (type(zone) is TzInfo and offset.microseconds):
            raise _refused(f"a {qualified(type(zone))} without a whole offset cannot cross")
        return {
            **found,
            "offset": [offset.days, offset.seconds, offset.microseconds],
            "kind": OFFSET_KINDS[type(zone)],
        }
    raise _refused(f"a {qualified(type(zone))} zone cannot cross a contract")


def _tzinfo(tree: dict[str, Any]) -> tzinfo:
    if "zone" in tree:
        return ZoneInfo(tree["zone"])
    days, seconds, microseconds = tree["offset"]
    offset = timedelta(days=days, seconds=seconds, microseconds=microseconds)
    if tree["kind"] == "pydantic":
        return TzInfo(int(offset.total_seconds()))
    if tree["kind"] == "timezone":
        return timezone(offset)
    raise ValueError(f"unknown offset kind {tree['kind']!r}")
