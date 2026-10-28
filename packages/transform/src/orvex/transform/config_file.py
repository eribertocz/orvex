"""Read one hand-edited YAML configuration file safely (P1-05, P1-08).

Shared by every configuration file of a project (`project.yaml`, `connections.yaml`, model
sidecars). The reader refuses what would make a file ambiguous or dangerous: anything but a
regular UTF-8 file under 1 MiB, more than one document, duplicate keys, anchors, aliases and
merge keys, nesting deeper than 32 levels, null values, and a `version` other than the integer
the caller supports. Every problem names the file, a JSON pointer and its line, and never the
text of the file: errors are raised outside exception handlers, so the YAML or decoding error
that quoted the file is not kept as their context.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

MAX_FILE_BYTES = 1024 * 1024
MAX_DEPTH = 32
MERGE_TAG = "tag:yaml.org,2002:merge"
_FIELD = re.compile(r"[a-z_][a-z0-9_]*")
_TYPE_NAMES = frozenset({"int", "str", "float", "bool", "list", "dict", "root"})


class ConfigProblem(Exception):
    """A configuration file that cannot be used.

    `kind` is `unreadable`, `yaml`, `schema` or `version`; callers map it to their own error
    codes. `duplicate` is set for a duplicate key, with `path` pointing at it.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        file: str,
        path: str | None = None,
        line: int | None = None,
        *,
        duplicate: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.file = file
        self.path = path
        self.line = line
        self.duplicate = duplicate


def token(key: object) -> str:
    """A key as a JSON pointer reference token (RFC 6901)."""
    text = "null" if key is None else str(key).lower() if isinstance(key, bool) else str(key)
    return text.replace("~", "~0").replace("/", "~1")


def _untoken(text: str) -> str:
    return text.replace("~1", "/").replace("~0", "~")


def _first_null(value: object, pointer: str) -> str | None:
    """The JSON pointer of the first null value, or None (the root itself is left to callers)."""
    items: list[tuple[str, object]] = []
    if isinstance(value, dict):
        items = [(f"{pointer}/{token(key)}", child) for key, child in value.items()]
    elif isinstance(value, list):
        items = [(f"{pointer}/{index}", child) for index, child in enumerate(value)]
    for child_pointer, child in items:
        if child is None:
            return child_pointer
        found = _first_null(child, child_pointer)
        if found is not None:
            return found
    return None


def _data_path(data: object, location: tuple[object, ...]) -> list[object]:
    """The part of a validation location that names keys and indexes of the data itself.

    Pydantic also names the union member or type it tried (`Test1`, `int`, `root`); those are
    left out so the pointer leads to the value in the file. A missing or unknown key is named.
    """
    path: list[object] = []
    node = data
    skipped = False  # a union member was named: a key it misses is not the file's fault
    for part in location:
        in_dict = isinstance(node, dict) and part in node
        in_list = isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node)
        if in_dict or in_list:
            node = node[part]  # type: ignore[index]
            path.append(part)
        elif (
            not skipped
            and isinstance(node, dict)
            and isinstance(part, str)
            and _FIELD.fullmatch(part)
            and part not in _TYPE_NAMES
        ):
            path.append(part)
            break
        else:
            skipped = True
    return path


class ConfigFile:
    """One YAML file: its data, and where each value is in the text."""

    def __init__(self, path: Path, supported_version: int, name: str | None = None) -> None:
        self.file = name or path.name
        failure: ConfigProblem | None = None
        data = b""
        try:
            # Only a regular file: a pipe would block and a device could be endless. Opened
            # without blocking, checked on the open descriptor, and read with a bound, so the
            # limit never depends on the size the file system reports.
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as handle:
                regular = stat.S_ISREG(os.fstat(handle.fileno()).st_mode)
                if regular:
                    data = handle.read(MAX_FILE_BYTES + 1)
            if not regular:
                failure = ConfigProblem(
                    "unreadable", f"{self.file} is not a regular file.", self.file
                )
            elif len(data) > MAX_FILE_BYTES:
                failure = ConfigProblem(
                    "yaml", f"{self.file}: larger than {MAX_FILE_BYTES // 1024} KiB.", self.file
                )
        except OSError as error:
            failure = ConfigProblem(
                "unreadable",
                f"{self.file} cannot be read ({error.strerror or type(error).__name__}).",
                self.file,
            )
        if failure is not None:
            raise failure
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            # The decode error carries the whole file; only the position is kept.
            failure = ConfigProblem(
                "yaml", f"{self.file}: the file is not UTF-8 text (byte {error.start}).", self.file
            )
        if failure is not None:
            raise failure
        try:
            self._root = yaml.compose(text, Loader=yaml.SafeLoader)
        except yaml.YAMLError as error:
            failure = self._yaml_failure(error)
        except RecursionError:
            failure = self._too_deep(None)
        if failure is not None:
            raise failure
        self._check_nodes(self._root, "", set(), 0)
        loader = yaml.SafeLoader("")
        try:
            self.data: object = (
                None if self._root is None else loader.construct_document(self._root)
            )
        except yaml.YAMLError as error:
            failure = self._yaml_failure(error)
        except RecursionError:
            failure = self._too_deep(None)
        except (ValueError, OverflowError) as error:
            # A scalar Python cannot build: an integer over 4300 digits, an impossible date.
            failure = ConfigProblem(
                "yaml", f"{self.file}: a value cannot be read ({type(error).__name__}).", self.file
            )
        finally:
            loader.dispose()
        if failure is not None:
            raise failure
        null = _first_null(self.data, "")
        if null is not None:
            # No field of these files is nullable; generated models would admit null where a
            # field has a default.
            raise self.schema_error(f"{null or '/'}: a value is required, not null.", null)
        if not isinstance(self.data, dict):
            return
        if "version" not in self.data:
            raise self.schema_error("'version' is required.", "")
        version = self.data["version"]
        if type(version) is not int:  # a YAML `true` is a bool, which Python counts as 1
            raise self.schema_error("'version' must be an integer.", "/version")
        if version != supported_version:
            raise ConfigProblem(
                "version",
                f"{self.file}: version {version} is not supported; this release reads "
                f"version {supported_version}.",
                self.file,
                "/version",
                self.line("/version"),
            )

    def _yaml_failure(self, error: yaml.YAMLError) -> ConfigProblem:
        """The problem and its line, without the quoted text of the line."""
        problem: str | None = None
        line: int | None = None
        if isinstance(error, yaml.MarkedYAMLError):
            problem = error.problem or error.context
            mark = error.problem_mark or error.context_mark
            line = None if mark is None else mark.line + 1
        return ConfigProblem(
            "yaml", f"{self.file}: {problem or 'invalid YAML'}.", self.file, None, line
        )

    def _too_deep(self, line: int | None) -> ConfigProblem:
        return ConfigProblem(
            "yaml",
            f"{self.file}: values are nested more than {MAX_DEPTH} levels deep.",
            self.file,
            None,
            line,
        )

    def _check_nodes(
        self, node: yaml.Node | None, pointer: str, seen_nodes: set[int], depth: int
    ) -> None:
        """Refuse duplicate keys, anchors and aliases, merge keys and deep nesting.

        An alias reuses a node already seen; refusing it rules out self-references and the
        exponential expansion of nested aliases, and keeps every value at one place in the file.
        """
        if node is None:
            return
        if id(node) in seen_nodes:
            raise ConfigProblem(
                "yaml",
                f"{self.file}: anchors and aliases (&, *) are not supported; write each value "
                "where it is used.",
                self.file,
                pointer,
                node.start_mark.line + 1,
            )
        seen_nodes.add(id(node))
        if depth > MAX_DEPTH:
            raise self._too_deep(node.start_mark.line + 1)
        if isinstance(node, yaml.MappingNode):
            seen: set[object] = set()
            for key, value in node.value:
                if key.tag == MERGE_TAG:
                    # `<<` would copy keys in after this check, hiding duplicates.
                    raise ConfigProblem(
                        "yaml",
                        f"{self.file}: merge keys (<<) are not supported; write each key.",
                        self.file,
                        pointer,
                        key.start_mark.line + 1,
                    )
                name = key.value if isinstance(key, yaml.ScalarNode) else None
                if name in seen:
                    raise ConfigProblem(
                        "yaml",
                        f"{self.file}: duplicate key '{name}'.",
                        self.file,
                        f"{pointer}/{token(name)}",
                        key.start_mark.line + 1,
                        duplicate=True,
                    )
                seen.add(name)
                self._check_nodes(value, f"{pointer}/{token(name)}", seen_nodes, depth + 1)
        elif isinstance(node, yaml.SequenceNode):
            for index, item in enumerate(node.value):
                self._check_nodes(item, f"{pointer}/{index}", seen_nodes, depth + 1)

    def line(self, pointer: str) -> int | None:
        """The line of the value at a JSON pointer, or of its closest existing parent."""
        node = self._root
        if node is None:
            return None
        for part in [_untoken(part) for part in pointer.split("/")[1:]]:
            child = None
            if isinstance(node, yaml.MappingNode):
                child = next((v for k, v in node.value if k.value == part), None)
            elif isinstance(node, yaml.SequenceNode) and part.isdigit():
                index = int(part)
                child = node.value[index] if index < len(node.value) else None
            if child is None:
                break
            node = child
        line: int = node.start_mark.line
        return line + 1

    def schema_error(self, message: str, pointer: str) -> ConfigProblem:
        return ConfigProblem(
            "schema", f"{self.file}: {message}", self.file, pointer, self.line(pointer)
        )

    def model[M: BaseModel](
        self,
        model: type[M],
        data: object,
        pointer: str,
        secret_fields: frozenset[str] = frozenset(),
    ) -> M:
        """Validate strictly; the error names the location, never the value found there."""
        if not isinstance(data, dict):
            raise self.schema_error("Expected a mapping.", pointer)
        try:
            return model.model_validate(data, strict=True, by_alias=True)
        except ValidationError as error:
            first = error.errors(include_input=False, include_url=False)[0]
        # Raised outside the handler: the validation error quotes the input (a literal password,
        # for example), and it must not stay attached to ours as its context.
        location = "/".join(token(part) for part in _data_path(data, first["loc"]))
        where = f"{pointer}/{location}" if location else pointer
        if first["type"] == "extra_forbidden":
            message = f"Unknown key '{first['loc'][-1]}'."
        elif first["loc"] and first["loc"][0] in secret_fields:
            message = f"{where}: a password must be a reference ${{env:NAME}}, never a value."
        else:
            message = f"{where or '/'}: {first['msg']}."
        raise self.schema_error(message, where)
