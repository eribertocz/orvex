"""Strict YAML for `security.yaml` (P2-08, #304).

Only plain data: mappings with string keys, sequences and scalars. Refused:
- anchors, aliases and tags;
- duplicate keys and non-string keys;
- documents over the size or depth bound.

Scalars are typed without YAML 1.1 surprises:
- a quoted scalar is a string;
- a plain `true` or `false` is a boolean, a plain decimal integer is an integer, a plain `null`
  or `~` is null;
- anything else is a string, so `yes`, `on`, `010` and `1e3` stay text.

Each value's line is recorded under its JSON Pointer for error messages.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

MAX_BYTES = 1024 * 1024
MAX_DEPTH = 32
_INTEGER = re.compile(r"[-+]?(0|[1-9][0-9]*)")


class StrictYamlError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(message)
        self.line = line


def _escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _scalar(node: yaml.ScalarNode) -> object:
    if node.style in {"'", '"', "|", ">"}:
        return node.value
    value = node.value
    if value in {"true", "false"}:
        return value == "true"
    if value in {"null", "~", ""}:
        return None
    if _INTEGER.fullmatch(value):
        return int(value)
    return value


def _convert(node: yaml.Node, pointer: str, depth: int, lines: dict[str, int]) -> object:
    line = node.start_mark.line + 1
    if depth > MAX_DEPTH:
        raise StrictYamlError(line, "nested too deeply")
    lines[pointer] = line
    if isinstance(node, yaml.ScalarNode):
        return _scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return [
            _convert(item, f"{pointer}/{index}", depth + 1, lines)
            for index, item in enumerate(node.value)
        ]
    if isinstance(node, yaml.MappingNode):
        found: dict[str, Any] = {}
        for key, value in node.value:
            if not isinstance(key, yaml.ScalarNode) or not isinstance(_scalar(key), str):
                raise StrictYamlError(key.start_mark.line + 1, "a key is a string")
            if key.value in found:
                raise StrictYamlError(key.start_mark.line + 1, "a duplicate key")
            found[key.value] = _convert(value, f"{pointer}/{_escape(key.value)}", depth + 1, lines)
        return found
    raise StrictYamlError(line, "an unsupported node")


def _explicit_tag(event: yaml.Event) -> bool:
    """Whether the document wrote a tag (`!!str`, `!custom`) rather than YAML resolving one."""
    tag = getattr(event, "tag", None)
    if tag is None:
        return False
    implicit = getattr(event, "implicit", False)
    if isinstance(implicit, tuple):  # a scalar: (plain, quoted)
        return not any(implicit)
    return implicit is False


def load(text: str | bytes) -> tuple[object, dict[str, int]]:
    """The document and the line of each JSON Pointer, or `StrictYamlError`."""
    data = text.encode("utf-8") if isinstance(text, str) else text
    if len(data) > MAX_BYTES:
        raise StrictYamlError(1, "the file is too large")
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        raise StrictYamlError(1, "the file is not UTF-8") from None
    try:
        for event in yaml.parse(decoded, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
                raise StrictYamlError(event.start_mark.line + 1, "anchors and aliases are refused")
            if _explicit_tag(event):
                raise StrictYamlError(event.start_mark.line + 1, "tags are refused")
        documents = list(yaml.compose_all(decoded, Loader=yaml.SafeLoader))
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        raise StrictYamlError(mark.line + 1 if mark else 1, "not valid YAML") from None
    if len(documents) != 1 or documents[0] is None:
        raise StrictYamlError(1, "the file holds exactly one document")
    lines: dict[str, int] = {}
    return _convert(documents[0], "", 0, lines), lines
