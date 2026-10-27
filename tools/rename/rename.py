"""Rename the product in one step (docs/process/engineering.md §1).

    python -m tools.rename.rename "New Name" new-slug [--dry-run]

Rewrites `identity.toml` (and its packaged copy), the namespace directories, imports,
distribution names, CLI entry points and every exempt location that names the product, and
records the old Ossie vendor name in `previous_vendor_names` so existing models keep loading.
Review the result with `git diff`, then run every check.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from tools.repository import REPOSITORY_ROOT, identity, read_text, tracked_files

_SLUG = re.compile(r"[a-z][a-z0-9-]*")


@dataclass(frozen=True, slots=True)
class Names:
    name: str
    slug: str
    namespace: str
    vendor: str

    @classmethod
    def from_slug(cls, name: str, slug: str) -> Names:
        if not name.strip() or not _SLUG.fullmatch(slug):
            raise ValueError("RENAME_INVALID: the name is empty or the slug is not [a-z][a-z0-9-]*")
        namespace = slug.replace("-", "_")
        return cls(name, slug, namespace, namespace.upper())


def identity_text(new: Names, previous_vendors: list[str]) -> str:
    vendors = ", ".join(f'"{vendor}"' for vendor in previous_vendors)
    return f"""# Product identity — the ONLY place where the product name is written.
# Every package, CLI, Docker image, Ossie vendor name and UI title is derived from this file.
# To rename the product: run `python -m tools.rename.rename "<Name>" <slug>`; it rewrites this
# file and adds the previous vendor name to `previous_vendor_names` so existing semantic models
# keep loading.

[product]
name = "{new.name}"
slug = "{new.slug}"
python_namespace = "{new.namespace}"
ossie_vendor_name = "{new.vendor}"
previous_vendor_names = [{vendors}]
"""


def rewrite(text: str, old: Names, new: Names) -> str:
    """Replace whole-word occurrences: display name, vendor name, namespace paths, then slug."""

    def words(word: str) -> str:
        return rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])"

    text = re.sub(words(old.name), new.name, text)
    text = re.sub(words(old.vendor), new.vendor, text)
    text = re.sub(words(old.namespace) + r"(?=[./][a-z_])", new.namespace, text)
    return re.sub(words(old.slug), new.slug, text)


def plan(root: Path, new: Names) -> tuple[dict[Path, str], list[tuple[Path, Path]]]:
    current = identity(root)
    old = Names(
        str(current["name"]),
        str(current["slug"]),
        str(current["python_namespace"]),
        str(current["ossie_vendor_name"]),
    )
    previous = [str(vendor) for vendor in current["previous_vendor_names"]]
    if old.vendor not in previous and old.vendor != new.vendor:
        previous.append(old.vendor)
    new_identity = identity_text(new, [vendor for vendor in previous if vendor != new.vendor])

    edits: dict[Path, str] = {}
    for path in tracked_files(root):
        if path.name == "identity.toml":
            edits[path] = new_identity
            continue
        text = read_text(path)
        if text is None:
            continue
        changed = rewrite(text, old, new)
        if changed != text:
            edits[path] = changed

    moves = [
        (source, source.with_name(new.namespace))
        for source in sorted((root / "packages").glob(f"*/src/{old.namespace}"))
        if old.namespace != new.namespace
    ]
    return edits, moves


def apply(edits: dict[Path, str], moves: list[tuple[Path, Path]]) -> None:
    for path, text in edits.items():
        path.write_text(text, encoding="utf-8")
    for source, target in moves:
        source.rename(target)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("name", help="new display name, for example 'Polaris'")
    parser.add_argument("slug", help="new slug, for example 'polaris'")
    parser.add_argument("--dry-run", action="store_true", help="show what would change")
    arguments = parser.parse_args()
    try:
        new = Names.from_slug(arguments.name, arguments.slug)
    except ValueError as error:
        print(error)
        return 1
    edits, moves = plan(REPOSITORY_ROOT, new)
    for path in sorted(edits):
        print(f"edit  {path.relative_to(REPOSITORY_ROOT)}")
    for source, target in moves:
        print(
            f"move  {source.relative_to(REPOSITORY_ROOT)} -> {target.relative_to(REPOSITORY_ROOT)}"
        )
    if not arguments.dry_run:
        apply(edits, moves)
    print(f"rename: {len(edits)} file(s), {len(moves)} director(ies)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
