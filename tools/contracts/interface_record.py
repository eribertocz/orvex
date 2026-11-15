"""The record of the interface contracts (#486 G3): `packages/contracts/interface-record.json`,
each contract's description with its kit, as the compatibility check reads it at a release.

    python -m tools.contracts.interface_record           # write the record
    python -m tools.contracts.interface_record --check   # INTERFACE_RECORD_STALE when it differs

A contract without its kit (INTERFACE_KIT_MISSING) or a kit without its contract
(INTERFACE_KIT_ORPHANED) cannot be recorded.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from orvex.contracts.interface import InterfaceContract
from orvex.contracts.kit import Kit
from orvex.contracts.registry import interface_contracts, kits

from tools.repository import REPOSITORY_ROOT, identity

RECORD = REPOSITORY_ROOT / "packages" / "contracts" / "interface-record.json"


def build_record(
    contracts: Mapping[str, InterfaceContract], kits: Mapping[str, Kit]
) -> dict[str, Any]:
    missing = sorted(set(contracts) - set(kits))
    if missing:
        raise SystemExit(f"INTERFACE_KIT_MISSING: contracts {missing} ship no conformance kit")
    orphaned = sorted(set(kits) - set(contracts))
    if orphaned:
        raise SystemExit(f"INTERFACE_KIT_ORPHANED: kits {orphaned} have no registered contract")
    return {
        "contracts": {
            name: {**contract.describe(), "kit": kits[name].describe()}
            for name, contract in sorted(contracts.items())
        }
    }


def render(record: Mapping[str, Any]) -> str:
    return json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def current_record() -> dict[str, Any]:
    """The record of the contracts this checkout declares."""
    package = importlib.import_module(f"{identity(REPOSITORY_ROOT)['python_namespace']}.contracts")
    return build_record(interface_contracts(package), kits(package))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the record is stale")
    arguments = parser.parse_args(argv)
    text = render(current_record())
    if arguments.check:
        if not RECORD.is_file() or RECORD.read_text("utf-8") != text:
            print(
                f"INTERFACE_RECORD_STALE: {RECORD.relative_to(RECORD.parents[2])}; run "
                "python -m tools.contracts.interface_record"
            )
            return 1
        print("interface record: current")
        return 0
    RECORD.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
