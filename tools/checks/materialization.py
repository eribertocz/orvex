"""MATERIALIZATION_IN_PYTHON: product code never pulls rows into Python objects (ADR-0025).

A use is allowed when the (file, name) is listed in `materialization-allowlist.toml` with a
reason that states its bound, or with the issue (`tracked`) that removes an unbounded use.
Reading a result once into Arrow (`to_arrow_table`, `to_arrow_reader`, `fetch_arrow_table`,
`fetch_record_batch`, `read_all`, `read_next_batch`) is allowed by ADR-0025 rule 3 and not
reported.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

from tools.checks.architecture import AllowListError, Finding, product_files

# Every way of turning query rows into Python objects (ADR-0025 rules 1 and 6).
MATERIALIZING_NAMES = frozenset(
    {
        # DuckDB results and relations
        "fetchall",
        "fetchone",
        "fetchmany",
        "fetchdf",
        "fetch_df",
        "fetch_df_chunk",
        "fetchnumpy",
        "df",
        "pl",
        "to_df",
        "torch",
        "tf",
        # DataFrame and array conversions
        "to_pandas",
        "to_numpy",
        "tolist",
        # Arrow to Python
        "to_pylist",
        "to_pydict",
        "as_py",
        # DataFrame rows walked one by one
        "iterrows",
        "itertuples",
    }
)
TRACKED = re.compile(r"#[1-9][0-9]*")
ALLOW_LIST = Path(__file__).with_name("materialization-allowlist.toml")


def load_allow_list(path: Path = ALLOW_LIST) -> set[tuple[str, str]]:
    entries = tomllib.loads(path.read_text(encoding="utf-8")).get("allow", [])
    allowed: set[tuple[str, str]] = set()
    for entry in entries:
        if not all(
            isinstance(entry.get(key), str) and entry[key].strip()
            for key in ("file", "call", "reason")
        ):
            raise AllowListError(
                f"{AllowListError.code}: entry {entry!r} needs file, call and reason"
            )
        tracked = entry.get("tracked")
        if tracked is not None and not (isinstance(tracked, str) and TRACKED.fullmatch(tracked)):
            raise AllowListError(
                f"{AllowListError.code}: entry {entry!r}: tracked must be an issue, #<number>"
            )
        allowed.add((entry["file"], entry["call"]))
    return allowed


def materialization_findings(
    tree: ast.AST, relative: str, allowed: set[tuple[str, str]]
) -> list[Finding]:
    """Every read of a materializing name, called or not (`rows = result.to_pylist` too);
    assigning an attribute of that name (`obj.df = …`) is not a read."""
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and node.attr in MATERIALIZING_NAMES
            and (relative, node.attr) not in allowed
        ):
            findings.append(
                Finding(
                    "MATERIALIZATION_IN_PYTHON",
                    relative,
                    node.lineno,
                    f".{node.attr} reads rows into Python (ADR-0025)",
                )
            )
    return findings


def stale_entries(root: Path, allowed: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Allow-list entries that no longer match any use, so permissions cannot outlive the code."""
    used: set[tuple[str, str]] = set()
    for path, relative in product_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        used |= {
            (relative, finding.detail.split()[0].removeprefix("."))
            for finding in materialization_findings(tree, relative, set())
        }
    return sorted(allowed - used)
