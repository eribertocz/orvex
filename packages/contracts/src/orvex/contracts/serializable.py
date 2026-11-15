"""Which annotated types may cross an interface contract (#486 G5): plain values, never live
objects, so every operation can later run in another process (#156).

A value type is one of `SCALARS` (an aware datetime and an Arrow table among them), a
`Literal` of booleans, integers, strings or `None`, a union of value types, a value type with
constraints (`Annotated`), a parameterized `tuple`, `list` or `dict` with `str` keys, an `Enum`,
or a pydantic model (the generated contract types) or frozen dataclass whose fields are all
value types. Anything else (`Any`, `object`, callables, iterators, locks, connections, a
mutable dataclass, a bare `tuple`) is refused.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum

import pyarrow as pa
from pydantic import AwareDatetime, BaseModel

SCALARS: tuple[type, ...] = (
    type(None),
    bool,
    int,
    float,
    str,
    bytes,
    Decimal,
    datetime,
    AwareDatetime,
    date,
    time,
    timedelta,
    pa.Table,
)
LITERAL_TYPES = (bool, int, str, type(None))
SEQUENCES = (tuple, list)


def type_name(annotation: object) -> str:
    """A stable text for an annotation: `int`, `tuple[str, ...]`, `a.b.Reading | None`."""
    if annotation is type(None) or annotation is None:
        return "None"
    if annotation is Ellipsis:
        return "..."
    origin = typing.get_origin(annotation)
    arguments = typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        return " | ".join(type_name(argument) for argument in arguments)
    if origin is typing.Literal:
        return f"Literal[{', '.join(repr(argument) for argument in arguments)}]"
    if origin is not None:
        inner = ", ".join(type_name(argument) for argument in arguments)
        return f"{type_name(origin)}[{inner}]"
    if isinstance(annotation, type):
        if annotation.__module__ == "builtins":
            return annotation.__qualname__
        return f"{annotation.__module__}.{annotation.__qualname__}"
    return repr(annotation)


def crossing_problem(annotation: object, classes: set[type]) -> str | None:
    """Why `annotation` may not cross a contract, or None; the classes it reaches (enums,
    models, dataclasses) are added to `classes`."""
    if annotation in SCALARS or annotation is None:
        return None
    origin = typing.get_origin(annotation)
    arguments = typing.get_args(annotation)
    if origin is typing.Annotated:  # constraints narrow a value type; they never widen it
        return crossing_problem(arguments[0], classes)
    if origin in (typing.Union, types.UnionType):
        return _first(crossing_problem(argument, classes) for argument in arguments)
    if origin is typing.Literal:
        if all(type(argument) in LITERAL_TYPES for argument in arguments):
            return None
        return f"{type_name(annotation)} holds a value that is not a plain literal"
    if origin in SEQUENCES:
        items = [argument for argument in arguments if argument is not Ellipsis]
        if not items:
            return f"{type_name(annotation)} does not say what it holds"
        return _first(crossing_problem(item, classes) for item in items)
    if origin is dict:
        key = arguments[0] if arguments else None
        if typing.get_origin(key) is typing.Annotated:
            key = typing.get_args(key)[0]
        if key is not str:
            return f"{type_name(annotation)}: only `str` keys may cross"
        return crossing_problem(arguments[1], classes)
    if origin is None and isinstance(annotation, type):
        return _class_problem(annotation, classes)
    return f"{type_name(annotation)} is not a value type: a live object cannot cross a contract"


def _class_problem(annotation: type, classes: set[type]) -> str | None:
    if annotation in classes:
        return None
    if issubclass(annotation, Enum):
        classes.add(annotation)
        return None
    if issubclass(annotation, BaseModel):
        classes.add(annotation)
        fields = (field.annotation for field in annotation.model_fields.values())
        return _first(crossing_problem(field, classes) for field in fields)
    if dataclasses.is_dataclass(annotation):
        if not annotation.__dataclass_params__.frozen:  # type: ignore[attr-defined]
            return f"{type_name(annotation)} is a mutable dataclass"
        classes.add(annotation)
        hints = typing.get_type_hints(annotation)
        fields = (hints[field.name] for field in dataclasses.fields(annotation))
        return _first(crossing_problem(field, classes) for field in fields)
    return f"{type_name(annotation)} is not a value type: a live object cannot cross a contract"


def _first(problems: typing.Iterable[str | None]) -> str | None:
    return next((problem for problem in problems if problem is not None), None)
