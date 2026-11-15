"""Aware datetimes only (#486 §2, law of explicit time zones): a contract declares
`pydantic.AwareDatetime`, never a plain `datetime`, and a naive datetime value never crosses a
binding, in process or serialized.

`naive_problem` finds a naive datetime anywhere inside a value that crosses: in a tuple, list,
dict, frozen dataclass or pydantic model. The codec refuses the same value while encoding it.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

from pydantic import BaseModel


def aware(moment: datetime) -> bool:
    """A datetime with a zone that gives it an offset."""
    return moment.tzinfo is not None and moment.utcoffset() is not None


def naive_problem(value: object) -> str | None:
    """Where a naive datetime sits inside `value`, or None."""
    if isinstance(value, datetime):
        return None if aware(value) else f"a naive datetime {value.isoformat()}"
    if isinstance(value, (tuple, list)):
        items: list[object] = list(value)
    elif isinstance(value, dict):
        items = list(value.values())
    elif isinstance(value, BaseModel):
        items = [getattr(value, name) for name in type(value).model_fields]
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        items = [getattr(value, field.name) for field in dataclasses.fields(value)]
    else:
        return None
    return next((p for p in map(naive_problem, items) if p is not None), None)
