"""The weekly read-only watch of Apache Ossie upstream (#285, ADR-0030 §7).

    python -m tools.ossie.watch [--watch WATCH.toml] [--pin PIN.toml] [--json REPORT.json]

It compares upstream `main` with the active registry entry (`vendor/apache-ossie/PIN.toml`, the
byte-identical copy of the packaged entry) on the paths and proposals of
`vendor/apache-ossie/watch.toml`, and reports:

1. each vendored file whose upstream SHA-256 differs from the entry;
2. each commit since the pinned one that touches a watched path;
3. each watched proposal whose state differs from the one recorded.

Each changed path is classified as informational, compatible, potentially incompatible or a
critical review. A class is a warning for a person, never a verdict: the watch changes no file,
version or contract, and writes nothing upstream or here. It exits 0 whatever it finds, and 1
(with an `::error::` annotation) only when it cannot run: the network, Git, or a malformed watch
list or entry. Standard library and the `git` command only.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tools.contracts.compat import incompatible
from tools.repository import REPOSITORY_ROOT

FOLDER = REPOSITORY_ROOT / "vendor" / "apache-ossie"

INFORMATIONAL = "informational"
COMPATIBLE = "compatible"
POTENTIALLY_INCOMPATIBLE = "potentially incompatible"
CRITICAL_REVIEW = "critical review"
# Lowest to highest: a commit takes the highest class of its files.
KINDS = (INFORMATIONAL, COMPATIBLE, POTENTIALLY_INCOMPATIBLE, CRITICAL_REVIEW)

PROPOSAL_STATES = ("open", "closed", "merged", "unrecorded")
WATCH_KEYS = frozenset({"repository", "api", "branch", "paths", "proposals"})

CRITICAL_FILES = frozenset(
    {"core-spec/expression_language.md", "core-spec/relational_semantics.md"}
)
SPECIFICATION = "core-spec/spec.md"
# Sections of the specification whose change needs a critical review, and those that are not
# normative, matched in the heading of a changed line and its parents.
CRITICAL_SECTIONS = ("metric", "relationship", "null", "aggregat", "extension")
NON_NORMATIVE_SECTIONS = ("example", "version history")
NON_NORMATIVE_FOLDERS = frozenset({"examples", "docs"})
CATALOGS = frozenset({"extensions/dialects.md", "extensions/vendors.md"})
# A heading's optional closing sequence: hashes after a space, then only spaces.
_CLOSING_HASHES = re.compile(r"(^|[ \t]+)#+[ \t]*$")
# Git's empty tree: the base of a commit without parents.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

Fetch = Callable[[int], Mapping[str, Any]]


class WatchError(Exception):
    """The watch cannot run: it fails instead of reporting nothing."""


@dataclass(frozen=True)
class WatchList:
    repository: str
    api: str
    branch: str
    paths: tuple[str, ...]
    proposals: Mapping[int, str]


@dataclass(frozen=True)
class Pin:
    commit: str
    files: Mapping[str, str]
    sha256: Mapping[str, str]


@dataclass(frozen=True)
class Classification:
    kind: str
    reason: str


@dataclass(frozen=True)
class Change:
    path: str
    kind: str
    reason: str


@dataclass(frozen=True)
class VendoredChange:
    file: str
    upstream: str
    pinned_sha256: str
    upstream_sha256: str | None


@dataclass(frozen=True)
class Commit:
    sha: str
    date: str
    subject: str
    paths: list[str]
    kind: str


@dataclass(frozen=True)
class ProposalChange:
    number: int
    recorded: str
    state: str

    def line(self) -> str:
        return f"#{self.number}: {self.recorded} -> {self.state}"


@dataclass(frozen=True)
class Report:
    upstream: str
    pinned: str
    vendored: list[VendoredChange]
    changes: list[Change]
    commits: list[Commit]
    proposals: list[ProposalChange]

    def to_json(self) -> dict[str, Any]:
        classes: dict[str, list[str]] = {}
        for change in self.changes:
            classes.setdefault(change.kind, []).append(change.path)
        return {
            "upstream": self.upstream,
            "pinned": self.pinned,
            "classes": {kind: classes[kind] for kind in KINDS if kind in classes},
            "vendored": [asdict(item) for item in self.vendored],
            "changes": [asdict(item) for item in self.changes],
            "commits": [asdict(item) for item in self.commits],
            "proposals": [asdict(item) for item in self.proposals],
        }

    def markdown(self) -> str:
        lines = [
            "# Apache Ossie upstream watch",
            "",
            f"Upstream `main` at `{self.upstream[:12]}`; pinned at `{self.pinned[:12]}`. "
            "Each class is a warning for a person to review, never a verdict.",
            "",
            "## Changes since the pin",
            "",
        ]
        lines += _table(
            ("Class", "Path", "Reason"),
            [(change.kind, f"`{change.path}`", change.reason) for change in self.changes],
        )
        lines += ["", "## Vendored files changed upstream", ""]
        lines += _table(
            ("File", "Upstream path", "Pinned SHA-256", "Upstream SHA-256"),
            [
                (
                    f"`{item.file}`",
                    f"`{item.upstream}`",
                    f"`{item.pinned_sha256}`",
                    f"`{item.upstream_sha256}`" if item.upstream_sha256 else "removed",
                )
                for item in self.vendored
            ],
        )
        lines += ["", "## Commits touching watched paths", ""]
        lines += _table(
            ("Commit", "Date", "Class", "Subject", "Paths"),
            [
                (
                    f"`{commit.sha[:12]}`",
                    commit.date,
                    commit.kind,
                    commit.subject,
                    ", ".join(f"`{path}`" for path in commit.paths),
                )
                for commit in self.commits
            ],
        )
        lines += ["", "## Proposals whose state changed", ""]
        lines += [f"- {item.line()}" for item in self.proposals] or ["None."]
        return "\n".join(lines) + "\n"

    def annotations(self) -> list[str]:
        """One `::warning::` workflow command per critical-review item."""
        return [
            f"::warning title=Ossie critical review::{_escape(f'{change.path}: {change.reason}')}"
            for change in self.changes
            if change.kind == CRITICAL_REVIEW
        ]


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    if not rows:
        return ["None."]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for row in rows:
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |")
    return lines


def _escape(message: str) -> str:
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


# --- Inputs ------------------------------------------------------------------------------------


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise WatchError(f"{path}: {error}") from error


def _text(data: Mapping[str, Any], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise WatchError(f"{path}: `{key}` must be a non-empty string")
    return value


def load_watch_list(path: Path) -> WatchList:
    data = _read_toml(path)
    unknown = set(data) - WATCH_KEYS
    if unknown:
        raise WatchError(f"{path}: unknown keys {sorted(unknown)}")
    paths = data.get("paths")
    if not isinstance(paths, list) or not paths:
        raise WatchError(f"{path}: `paths` must be a non-empty list")
    if not all(isinstance(item, str) and item for item in paths):
        raise WatchError(f"{path}: every watched path must be a non-empty string")
    table = data.get("proposals")
    if not isinstance(table, dict):
        raise WatchError(f"{path}: `[proposals]` is missing")
    proposals: dict[int, str] = {}
    for number, state in table.items():
        if not number.isdigit():
            raise WatchError(f"{path}: proposal {number!r} is not a number")
        if state not in PROPOSAL_STATES:
            raise WatchError(
                f"{path}: proposal {number}: state {state!r} is not one of {PROPOSAL_STATES}"
            )
        proposals[int(number)] = state
    return WatchList(
        repository=_text(data, "repository", path),
        api=_text(data, "api", path).rstrip("/"),
        branch=_text(data, "branch", path),
        paths=tuple(paths),
        proposals=proposals,
    )


def load_pin(path: Path) -> Pin:
    data = _read_toml(path)
    commit = _text(data, "commit", path)
    files, hashes = data.get("files"), data.get("sha256")
    if not isinstance(files, dict) or not isinstance(hashes, dict) or set(files) != set(hashes):
        raise WatchError(f"{path}: `[files]` and `[sha256]` must name the same files")
    return Pin(commit=commit, files=dict(files), sha256=dict(hashes))


# --- Git ---------------------------------------------------------------------------------------


def _git(git_dir: Path | None, *arguments: str) -> bytes:
    command = ["git", *([f"--git-dir={git_dir}"] if git_dir else []), *arguments]
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(command, capture_output=True, check=False, env=environment)
    except OSError as error:
        raise WatchError(f"git cannot run: {error}") from error
    if done.returncode != 0:
        message = done.stderr.decode("utf-8", errors="replace").strip()
        raise WatchError(f"git {arguments[0]} failed: {message}")
    return done.stdout


def _read(git_dir: Path, revision: str, path: str) -> bytes | None:
    """A file's bytes at `revision`, or None when it does not exist there."""
    if not _git(git_dir, "ls-tree", revision, "--", path).strip():
        return None
    return _git(git_dir, "cat-file", "blob", f"{revision}:{path}")


def _names(output: bytes) -> list[str]:
    # A name that is not UTF-8 keeps its bytes, so it can be passed back to Git unchanged.
    return [name.decode("utf-8", errors="surrogateescape") for name in output.split(b"\0") if name]


# --- Classification ----------------------------------------------------------------------------


def classify(path: str, before: bytes | None, after: bytes | None) -> Classification:
    """The class of one upstream path's change from `before` to `after` (None: absent)."""
    name = path.rsplit("/", 1)[-1]
    if path in CRITICAL_FILES:
        return Classification(CRITICAL_REVIEW, "expression language or relational semantics")
    if path.startswith("compliance/"):
        # Every compliance file is an expectation, whatever folder holds it; a README is not.
        if name.upper().startswith("README"):
            return Classification(INFORMATIONAL, "non-normative text")
        return Classification(CRITICAL_REVIEW, "a compliance expectation")
    if (
        name.upper().startswith("README")
        or path == "ROADMAP.md"
        or NON_NORMATIVE_FOLDERS & set(path.split("/")[:-1])
    ):
        return Classification(INFORMATIONAL, "non-normative text")
    if path == SPECIFICATION:
        return _specification(before, after)
    if before is None or after is None:
        added = "added" if before is None else "removed"
        return Classification(POTENTIALLY_INCOMPATIBLE, f"a normative file {added} upstream")
    if name.endswith(".json") and "schema" in name:
        return _schema(before, after)
    if path in CATALOGS:
        return _catalog(before, after)
    if path.startswith("validation/"):
        return Classification(POTENTIALLY_INCOMPATIBLE, "a validator rule changed")
    return Classification(POTENTIALLY_INCOMPATIBLE, "a normative file changed")


def _lines(content: bytes) -> list[str]:
    return content.decode("utf-8", errors="replace").splitlines()


def _schema_version(schema: object) -> object:
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise WatchError("a schema's `properties` is not an object")
    version = properties.get("version", {})
    if not isinstance(version, dict):
        raise WatchError("a schema's `properties.version` is not an object")
    return version.get("const")


def _schema(before: bytes, after: bytes) -> Classification:
    try:
        old, new = json.loads(before), json.loads(after)
    except ValueError:
        return Classification(POTENTIALLY_INCOMPATIBLE, "the schema is not readable JSON")
    if _schema_version(old) != _schema_version(new):
        return Classification(
            POTENTIALLY_INCOMPATIBLE,
            f"the version const changed from {_schema_version(old)!r} to {_schema_version(new)!r}",
        )
    findings = incompatible(old, new)
    if findings:
        more = f" and {len(findings) - 3} more" if len(findings) > 3 else ""
        return Classification(POTENTIALLY_INCOMPATIBLE, "; ".join(findings[:3]) + more)
    return Classification(COMPATIBLE, "no finding from the compatibility checker")


def _catalog(before: bytes, after: bytes) -> Classification:
    opcodes = difflib.SequenceMatcher(None, _lines(before), _lines(after), autojunk=False)
    if any(tag in ("replace", "delete") for tag, *_ in opcodes.get_opcodes()):
        return Classification(POTENTIALLY_INCOMPATIBLE, "catalog entries removed or changed")
    return Classification(COMPATIBLE, "catalog additions")


def _sections(lines: Sequence[str]) -> list[tuple[str, ...]]:
    """For each line, the chain of Markdown headings it is under, as CommonMark reads them: a
    heading or a fence is indented by at most three spaces (four or more is an indented code
    block), and a fence closes only with its own character, at least as long as it opened."""
    stack: list[tuple[int, str]] = []
    fence: tuple[str, int] | None = None
    chains: list[tuple[str, ...]] = []
    for line in lines:
        expanded = line.expandtabs(4)
        body = expanded.lstrip(" ")
        indented = len(expanded) - len(body) > 3
        if not indented and body[:3] in ("```", "~~~"):
            mark = body[0]
            length = len(body) - len(body.lstrip(mark))
            if fence is None:
                fence = (mark, length)
            elif mark == fence[0] and length >= fence[1] and not body[length:].strip():
                fence = None
        elif fence is None and not indented and body.startswith("#"):
            marks = len(body) - len(body.lstrip("#"))
            if marks <= 6 and body[marks : marks + 1] in ("", " ", "\t"):
                title = _CLOSING_HASHES.sub("", body[marks:]).strip()
                while stack and stack[-1][0] >= marks:
                    stack.pop()
                stack.append((marks, title))
        chains.append(tuple(title for _, title in stack))
    return chains


def _matches(chain: tuple[str, ...], words: Sequence[str]) -> bool:
    return any(word in title.lower() for title in chain for word in words)


def _specification(before: bytes | None, after: bytes | None) -> Classification:
    if before is None or after is None:
        return Classification(CRITICAL_REVIEW, "the specification was added or removed")
    old, new = _lines(before), _lines(after)
    old_sections, new_sections = _sections(old), _sections(new)
    changed: set[tuple[str, ...]] = set()
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, first, last, new_first, new_last in matcher.get_opcodes():
        if tag != "equal":
            changed.update(old_sections[first:last], new_sections[new_first:new_last])
    named = ", ".join(sorted(" > ".join(chain) or "(preamble)" for chain in changed))
    if any(_matches(chain, CRITICAL_SECTIONS) for chain in changed):
        return Classification(CRITICAL_REVIEW, f"specification sections: {named}")
    if changed and all(_matches(chain, NON_NORMATIVE_SECTIONS) for chain in changed):
        return Classification(INFORMATIONAL, f"non-normative sections: {named}")
    return Classification(POTENTIALLY_INCOMPATIBLE, f"normative sections: {named}")


def _highest(kinds: Sequence[str]) -> str:
    return max(kinds, key=KINDS.index)


# --- The watch ---------------------------------------------------------------------------------


def _state(number: int, answer: Mapping[str, Any]) -> str:
    state = answer.get("state") if isinstance(answer, Mapping) else None
    if state not in ("open", "closed"):
        raise WatchError(f"proposal #{number}: the API answered an unknown state {state!r}")
    pull = answer.get("pull_request")
    return "merged" if isinstance(pull, Mapping) and pull.get("merged_at") else str(state)


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect, but never carry the token to another host."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,  # noqa: ANN401
        code: int,
        msg: str,
        headers: Any,  # noqa: ANN401
        newurl: str,
    ) -> urllib.request.Request | None:
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            before, after = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
            if (before.scheme, before.hostname, before.port) != (
                after.scheme,
                after.hostname,
                after.port,
            ):
                new.remove_header("Authorization")
        return new


OPENER = urllib.request.build_opener(SameHostRedirect())


def api_reader(api: str) -> Fetch:
    """Read one issue or pull request from the public API: GET only, with the default token."""

    def fetch(number: int) -> Mapping[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ossie-upstream-watch",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(f"{api}/issues/{number}", headers=headers, method="GET")
        try:
            with OPENER.open(request, timeout=30) as answer:
                data = json.loads(answer.read())
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as error:
            raise WatchError(f"proposal #{number} cannot be read: {error}") from error
        if not isinstance(data, dict):
            raise WatchError(f"proposal #{number}: the API answer is not an object")
        return data

    return fetch


def run(watch_list: WatchList, pin: Pin, fetch: Fetch) -> Report:
    with tempfile.TemporaryDirectory(prefix="ossie-upstream-") as temporary:
        git_dir = Path(temporary) / "upstream.git"
        _git(
            None,
            "clone",
            "--quiet",
            "--bare",
            "--no-tags",
            "--single-branch",
            f"--branch={watch_list.branch}",
            "--",
            watch_list.repository,
            str(git_dir),
        )
        return _compare(git_dir, watch_list, pin, fetch)


def _compare(git_dir: Path, watch_list: WatchList, pin: Pin, fetch: Fetch) -> Report:
    head = _git(git_dir, "rev-parse", "--verify", f"refs/heads/{watch_list.branch}^{{commit}}")
    upstream = head.decode().strip()
    try:
        _git(git_dir, "cat-file", "-e", f"{pin.commit}^{{commit}}")
    except WatchError as error:
        raise WatchError(f"the pinned commit {pin.commit} is not upstream") from error
    vendored: list[VendoredChange] = []
    for file, upstream_path in sorted(pin.files.items()):
        content = _read(git_dir, upstream, upstream_path)
        digest = hashlib.sha256(content).hexdigest() if content is not None else None
        if digest != pin.sha256[file]:
            vendored.append(VendoredChange(file, upstream_path, pin.sha256[file], digest))
    paths = list(watch_list.paths)
    changes: list[Change] = []
    names = _git(
        git_dir, "diff", "--no-renames", "--name-only", "-z", pin.commit, upstream, "--", *paths
    )
    for path in _names(names):
        found = classify(path, _read(git_dir, pin.commit, path), _read(git_dir, upstream, path))
        changes.append(Change(path, found.kind, found.reason))
    commits: list[Commit] = []
    listing = _git(
        git_dir,
        "log",
        "--reverse",
        "-z",
        "--format=%H%x1f%P%x1f%cs%x1f%s",
        f"{pin.commit}..{upstream}",
        "--",
        *paths,
    )
    # One NUL-separated record per commit; the subject, which may hold any other character, is
    # the last field.
    for record in listing.decode("utf-8", errors="replace").split("\0"):
        if not record:
            continue
        sha, parents, date, subject = record.split("\x1f", 3)
        base = parents.split()[0] if parents else EMPTY_TREE
        touched = _names(
            _git(git_dir, "diff", "--no-renames", "--name-only", "-z", base, sha, "--", *paths)
        )
        kinds = [
            classify(path, _read(git_dir, base, path), _read(git_dir, sha, path)).kind
            for path in touched
        ]
        commits.append(Commit(sha, date, subject, touched, _highest(kinds)))
    proposals: list[ProposalChange] = []
    for number, recorded in sorted(watch_list.proposals.items()):
        state = _state(number, fetch(number))
        if state != recorded:
            proposals.append(ProposalChange(number, recorded, state))
    return Report(upstream, pin.commit, vendored, changes, commits, proposals)


def main(argv: Sequence[str] | None = None, fetch: Fetch | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.ossie.watch", description=__doc__)
    parser.add_argument("--watch", type=Path, default=FOLDER / "watch.toml")
    parser.add_argument("--pin", type=Path, default=FOLDER / "PIN.toml")
    parser.add_argument("--json", type=Path, help="where to write the JSON report")
    arguments = parser.parse_args(argv)
    try:
        watch_list = load_watch_list(arguments.watch)
        pin = load_pin(arguments.pin)
        report = run(watch_list, pin, fetch or api_reader(watch_list.api))
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with Path(summary).open("a", encoding="utf-8") as output:
                output.write(_printable(report.markdown()))
        else:
            print(_printable(report.markdown()))
        if arguments.json:
            arguments.json.parent.mkdir(parents=True, exist_ok=True)
            arguments.json.write_text(
                json.dumps(report.to_json(), indent=2) + "\n", encoding="utf-8"
            )
        for annotation in report.annotations():
            print(_printable(annotation))
    except Exception as error:  # noqa: BLE001 - a WatchError, or any unexpected error, fails visibly
        return _cannot_run(error)
    return 0


def _cannot_run(error: BaseException) -> int:
    message = f"the Ossie upstream watch cannot run: {type(error).__name__}: {error}"
    print(f"::error::{_printable(_escape(message))}")
    return 1


def _printable(text: str) -> str:
    """`text` with any byte kept from a non-UTF-8 name shown as an escape."""
    return text.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="backslashreplace")


if __name__ == "__main__":
    sys.exit(main())
