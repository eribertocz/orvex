"""Where a user's attributes come from (application, #440): the `AttributeSource` interface, the
project's own attributes behind it, and the merge the context provider reads.

- **Reading back.** `stored_attributes` returns each stored attribute through
  `attribute_values.typed`. A value that fails the checks raises `UnreadableAttribute`: it is
  never dropped or repaired, and the caller builds no context (fail closed).
- **Every source is checked.** `merged` puts each name and value from every source through the
  same checks as a value set by hand, so a later source (an identity provider's claims, say)
  can never bring in a reserved name, a value outside its type or a 65th attribute.
- **Precedence.** The first source's value of a name wins. The project's own attributes come
  first, so a value set by hand is never overwritten.
"""

from __future__ import annotations

from typing import Any, Protocol

from orvex.access.directory import attribute_values
from orvex.access.errors import AccessError
from orvex.access.storage.store import Transaction


class UnreadableAttribute(Exception):
    """An attribute that fails the contract's checks: no context is built from it."""


class AttributeSource(Protocol):
    """A user's typed attributes in a project, read inside the caller's transaction."""

    def attributes_for(
        self, transaction: Transaction, user: str, project: str
    ) -> dict[str, dict[str, Any]]: ...


def stored_attributes(
    transaction: Transaction, user: str, project: str
) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for name, kind, value in transaction.rows(
        "SELECT name, type, value FROM attributes WHERE project = :project AND user_id = :user",
        project=project,
        user=user,
    ):
        try:
            found[attribute_values.checked_name(str(name))] = attribute_values.typed(
                str(kind), str(value)
            )
        except AccessError as error:
            raise UnreadableAttribute(str(name)) from error
    return found


class ProjectAttributes:
    """The attributes a Builder set in the project: the one source in v1."""

    def attributes_for(
        self, transaction: Transaction, user: str, project: str
    ) -> dict[str, dict[str, Any]]:
        return stored_attributes(transaction, user, project)


def _checked(name: object, attribute: object) -> tuple[str, dict[str, Any]]:
    try:
        if not isinstance(attribute, dict):
            raise attribute_values.invalid("value")
        kind = attribute.get("type")
        value = attribute.get("values" if kind == "list" else "value")
        expected = {"type", "values" if kind == "list" else "value"}
        found = attribute_values.checked(kind, value)
        if set(attribute) != expected or found != attribute:  # not in its contract form
            raise attribute_values.invalid("value")
        return attribute_values.checked_name(name), found
    except AccessError as error:
        raise UnreadableAttribute(str(name)) from error


def merged(
    sources: tuple[AttributeSource, ...], transaction: Transaction, user: str, project: str
) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for source in sources:
        for name, attribute in source.attributes_for(transaction, user, project).items():
            checked_name, checked = _checked(name, attribute)
            found.setdefault(checked_name, checked)
    if len(found) > attribute_values.MAXIMUM_PER_USER:
        raise UnreadableAttribute("too many attributes")
    return found
