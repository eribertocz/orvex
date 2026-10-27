"""Product identity: the display name, slug and namespaces read from `identity.toml`.

`server` reads the identity once and passes the values to the other modules at composition;
no other module reads the file (docs/process/engineering.md §1).
"""

from __future__ import annotations

import keyword
import re
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

_SLUG = re.compile(r"[a-z][a-z0-9-]*")
_VENDOR = re.compile(r"[A-Z][A-Z0-9_]*")


class IdentityError(ValueError):
    """`identity.toml` is missing a field or holds an invalid value."""

    code = "IDENTITY_INVALID"

    def __init__(self, field: str, problem: str) -> None:
        super().__init__(f"{self.code}: identity field '{field}' {problem}")
        self.field = field


@dataclass(frozen=True, slots=True)
class Identity:
    name: str
    slug: str
    python_namespace: str
    ossie_vendor_name: str
    previous_vendor_names: tuple[str, ...]


def load_identity(path: Path | None = None) -> Identity:
    """Read the identity from `path`, or from the copy packaged with the server by default."""
    text = (
        path.read_text(encoding="utf-8")
        if path is not None
        else files("sextant.server").joinpath("identity.toml").read_text(encoding="utf-8")
    )
    return parse_identity(text)


def parse_identity(text: str) -> Identity:
    try:
        product = tomllib.loads(text)["product"]
    except (tomllib.TOMLDecodeError, KeyError) as error:
        raise IdentityError("product", "table is missing or the file is not valid TOML") from error

    name = _string(product, "name")
    if not name.strip():
        raise IdentityError("name", "is empty")
    slug = _string(product, "slug")
    if not _SLUG.fullmatch(slug):
        raise IdentityError("slug", f"'{slug}' does not match [a-z][a-z0-9-]*")
    namespace = _string(product, "python_namespace")
    if (
        not namespace.isidentifier()
        or keyword.iskeyword(namespace)
        or namespace != namespace.lower()
    ):
        raise IdentityError("python_namespace", f"'{namespace}' is not a lowercase Python name")
    vendor = _string(product, "ossie_vendor_name")
    if not _VENDOR.fullmatch(vendor):
        raise IdentityError("ossie_vendor_name", f"'{vendor}' does not match [A-Z][A-Z0-9_]*")
    previous = product.get("previous_vendor_names")
    if not isinstance(previous, list) or not all(
        isinstance(item, str) and _VENDOR.fullmatch(item) for item in previous
    ):
        raise IdentityError("previous_vendor_names", "must be a list of vendor names")
    return Identity(name, slug, namespace, vendor, tuple(previous))


def _string(product: dict[str, Any], field: str) -> str:
    value = product.get(field)
    if not isinstance(value, str):
        raise IdentityError(field, "is missing or not a string")
    return value
