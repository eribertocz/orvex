"""Errors of the semantic engine (`contracts/semantic-error/v1`).

A `SemanticError` carries a stable code and its complete details. It is validated when it is
raised, so an unknown code or a missing or extra detail fails before anything is rendered.
`to_message` renders it for one locale and one audience; any audience but `builder` gets the
generic template and the public details only (fail closed).
"""

from __future__ import annotations

import json
import string
from collections.abc import Mapping
from functools import cache
from importlib.resources import files
from types import MappingProxyType
from typing import Any, Literal

from orvex.contracts.semantic_error.v1 import SemanticError as SemanticErrorMessage
from pydantic import ValidationError

Audience = Literal["other", "builder"]
LOCALES = ("es", "en")
DEFAULT_LOCALE = "es"


@cache
def _catalog() -> tuple[dict[str, Any], dict[str, dict[str, dict[str, str]]]]:
    """`codes.json` and the message templates by locale, as the contract ships them."""
    data = files("orvex.contracts.semantic_error").joinpath("v1_data")
    codes: dict[str, Any] = json.loads(data.joinpath("codes.json").read_text(encoding="utf-8"))
    messages = {
        locale: json.loads(data.joinpath("messages", f"{locale}.json").read_text(encoding="utf-8"))
        for locale in LOCALES
    }
    return codes, messages


def resolve_locale(locale: str | None) -> str:
    """The catalog language for a requested locale: its primary subtag if known, else `es`."""
    primary = (locale or "").split("-")[0].split("_")[0].lower()
    return primary if primary in LOCALES else DEFAULT_LOCALE


def _shown(value: object) -> str:
    if isinstance(value, list):
        if value and not all(isinstance(item, str) for item in value):
            return str(len(value))  # a list of objects (OSSIE_INVALID.problems): its count
        return ", ".join(str(item) for item in value)
    return str(value)


def _placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field is not None}


def _render(template: str, details: Mapping[str, object]) -> str:
    # Only the template is parsed: a value is inserted as text, never read as a template.
    parts = []
    for literal, field, _, _ in string.Formatter().parse(template):
        parts.append(literal)
        if field is not None:
            parts.append(_shown(details[field]))
    return "".join(parts)


class SemanticError(Exception):
    """A rejection, warning or notice with a stable code from `semantic-error/v1`."""

    def __init__(self, code: str, details: Mapping[str, object]) -> None:
        codes, _ = _catalog()
        entry = codes.get(code)
        if entry is None:
            raise ValueError(f"unknown semantic error code {code!r}")
        allowed = set(entry["details"])
        required = allowed - set(entry["optional"])
        given = set(details)
        if not required <= given <= allowed:
            raise ValueError(
                f"{code}: details must be {sorted(required)}, "
                f"optionally {sorted(entry['optional'])}; "
                f"missing {sorted(required - given)}, extra {sorted(given - allowed)}"
            )
        super().__init__(code)
        self.code = code
        self.severity: str = entry["severity"]
        self.category: str = entry["category"]
        self.details: Mapping[str, object] = MappingProxyType(dict(details))
        # The builder view holds every detail: validating it checks each value's type.
        try:
            self.to_message(DEFAULT_LOCALE, "builder")
        except ValidationError as error:
            # Only this code's builder variant explains the failure; the union lists them all.
            variant = "".join(part.capitalize() for part in code.split("_")) + "Error"
            fields = sorted(
                {
                    str(problem["loc"][problem["loc"].index("details") + 1])
                    for problem in error.errors()
                    if problem["loc"][:1] == (variant,)
                    and "details" in problem["loc"]
                    and len(problem["loc"]) > problem["loc"].index("details") + 1
                }
            )
            raise ValueError(f"{code}: invalid details {fields}") from None

    def __str__(self) -> str:
        return self.code

    def to_message(
        self, locale: str | None = None, audience: str | None = None
    ) -> SemanticErrorMessage:
        """The contract message for `locale` and `audience`. Any audience but `builder` gets
        the generic template and the public details only. Serialize it with `to_json`, or with
        `exclude_none=True`: an optional detail left out is `None` here and absent on the wire."""
        codes, messages = _catalog()
        language = resolve_locale(locale)
        # Exactly the text "builder"; anything else, a look-alike included, is the other view.
        view: Audience = "builder" if type(audience) is str and audience == "builder" else "other"
        disclosure: dict[str, str] = codes[self.code]["details"]
        shown = {
            name: value
            for name, value in self.details.items()
            if view == "builder" or disclosure[name] == "public"
        }
        templates = messages[language][self.code]
        template = templates["builder" if view == "builder" else "generic"]
        if not _placeholders(template) <= set(shown):
            template = templates["generic"]  # an optional detail was left out
        return SemanticErrorMessage.model_validate(
            {
                "kind": "semantic_error",
                "code": self.code,
                "severity": self.severity,
                "category": self.category,
                "audience": view,
                "message": _render(template, shown),
                "locale": language,
                "details": shown,
            }
        )

    def to_json(self, locale: str | None = None, audience: str | None = None) -> str:
        """The message as canonical JSON text: the same input always gives the same bytes."""
        # No contract field is nullable: an optional detail left out is absent, never `null`.
        message = self.to_message(locale, audience).model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        return json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
