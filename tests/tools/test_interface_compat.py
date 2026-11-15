"""Interface-contract compatibility with the last release (#486 G3), and the record it compares."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any, Protocol

import orvex.contracts
import pytest
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.interface import InterfaceContract, Operation
from orvex.contracts.kit import Idempotent, Kit, KitCase
from orvex.contracts.kit_client import KitClient
from orvex.contracts.registry import interface_contracts, kits
from orvex.contracts.version import Version

from tools.contracts import interface_compat, interface_record
from tools.contracts.interface_compat import incompatibilities, last_release, released_record
from tools.contracts.interface_record import RECORD, build_record, render
from tools.repository import REPOSITORY_ROOT


class MeterError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, str]) -> None:
        super().__init__(f"{code}: {message}")


class Meter(Protocol):
    def read(self, envelope: CallEnvelope, meter: str) -> int: ...

    def reset(self, envelope: CallEnvelope, meter: str) -> None: ...


def check(meter: KitClient) -> None:
    meter.read("m")


def sent(meter: KitClient, envelope: CallEnvelope) -> object:
    return meter.send("reset", envelope, "m")


def contract(**changes: Any) -> InterfaceContract:  # noqa: ANN401
    fields: dict[str, Any] = {
        "name": "meter",
        "version": Version(1, 0),
        "protocol": Meter,
        "error_type": MeterError,
        "capabilities": ("read", "write"),
        "errors": ("METER_UNKNOWN", "METER_LOCKED"),
        "operations": (
            Operation("read", "read", ("METER_UNKNOWN",)),
            Operation("reset", "write", ("METER_UNKNOWN", "METER_LOCKED")),
        ),
    }
    fields.update(changes)
    return InterfaceContract(**fields)


def kit(of: InterfaceContract, *cases: KitCase) -> Kit:
    chosen = cases or (
        KitCase("read.known", "read", "a known meter reads its count", check),
        KitCase("reset.zero", "reset", "a reset meter reads zero", check),
    )
    return Kit(of, recorded=len(chosen), cases=chosen)


def recorded(of: InterfaceContract, the_kit: Kit | None = None) -> dict[str, Any]:
    return build_record({of.name: of}, {of.name: the_kit or kit(of)})


RELEASED = recorded(contract())


def codes(found: list[str]) -> list[str]:
    return [problem.split(":", 1)[0] for problem in found]


class ReadOnly(Protocol):
    def read(self, envelope: CallEnvelope, meter: str) -> int: ...


class Renamed(Protocol):
    def read(self, envelope: CallEnvelope, name: str) -> int: ...

    def reset(self, envelope: CallEnvelope, meter: str) -> None: ...


class Retyped(Protocol):
    def read(self, envelope: CallEnvelope, meter: str) -> str: ...

    def reset(self, envelope: CallEnvelope, meter: str) -> None: ...


class MoreRequired(Protocol):
    def read(self, envelope: CallEnvelope, meter: str, at: int) -> int: ...

    def reset(self, envelope: CallEnvelope, meter: str) -> None: ...


class MoreOptional(Protocol):
    def read(self, envelope: CallEnvelope, meter: str, at: int | None = None) -> int: ...

    def reset(self, envelope: CallEnvelope, meter: str) -> None: ...

    def total(self, envelope: CallEnvelope) -> int: ...


def removed_operation() -> dict[str, Any]:
    reduced = contract(
        protocol=ReadOnly,
        capabilities=("read",),
        operations=(Operation("read", "read", ("METER_UNKNOWN",)),),
    )
    return recorded(
        reduced, kit(reduced, KitCase("read.known", "read", "a known meter reads its count", check))
    )


def renamed_error() -> dict[str, Any]:
    return recorded(
        contract(
            errors=("METER_NOT_FOUND", "METER_LOCKED"),
            operations=(
                Operation("read", "read", ("METER_NOT_FOUND",)),
                Operation("reset", "write", ("METER_NOT_FOUND", "METER_LOCKED")),
            ),
        )
    )


def changed_expectation() -> dict[str, Any]:
    of = contract()
    return recorded(
        of,
        kit(
            of,
            KitCase("read.known", "read", "a known meter reads its count, or zero", check),
            KitCase("reset.zero", "reset", "a reset meter reads zero", check),
        ),
    )


class TestPlantedViolations:
    def test_a_removed_operation_fails(self) -> None:
        found = codes(incompatibilities(RELEASED, removed_operation()))
        assert "INTERFACE_OPERATION_REMOVED" in found
        assert "INTERFACE_CAPABILITY_REMOVED" in found
        assert "INTERFACE_KIT_CASE_REMOVED" in found

    def test_a_renamed_error_code_fails(self) -> None:
        assert codes(incompatibilities(RELEASED, renamed_error())) == [
            "INTERFACE_ERROR_CODE_REMOVED"
        ]

    def test_a_changed_kit_expectation_fails(self) -> None:
        assert codes(incompatibilities(RELEASED, changed_expectation())) == [
            "INTERFACE_KIT_EXPECTATION_CHANGED"
        ]

    @pytest.mark.parametrize("protocol", [Renamed, Retyped, MoreRequired], ids=lambda p: p.__name__)
    def test_a_changed_signature_fails(self, protocol: type) -> None:
        assert codes(incompatibilities(RELEASED, recorded(contract(protocol=protocol)))) == [
            "INTERFACE_SIGNATURE_CHANGED"
        ]

    def test_a_moved_operation_fails(self) -> None:
        moved = contract(
            operations=(
                Operation("read", "write", ("METER_UNKNOWN",)),
                Operation("reset", "write", ("METER_UNKNOWN", "METER_LOCKED")),
            ),
            capabilities=("write",),
        )
        found = codes(incompatibilities(RELEASED, recorded(moved)))
        assert "INTERFACE_SIGNATURE_CHANGED" in found

    def test_a_removed_contract_and_a_lowered_version_fail(self) -> None:
        assert codes(incompatibilities(RELEASED, {"contracts": {}})) == [
            "INTERFACE_CONTRACT_REMOVED"
        ]
        newer = recorded(contract(version=Version(1, 3)))
        assert codes(incompatibilities(newer, RELEASED)) == ["INTERFACE_VERSION_LOWERED"]


class TestCompatibleChanges:
    def test_the_same_contract_is_compatible(self) -> None:
        assert incompatibilities(RELEASED, copy.deepcopy(RELEASED)) == []

    def test_appending_is_compatible(self) -> None:
        grown = contract(
            version=Version(1, 1),
            protocol=MoreOptional,
            capabilities=("read", "write", "sum"),
            errors=("METER_UNKNOWN", "METER_LOCKED", "METER_OVERFLOW"),
            operations=(
                Operation("read", "read", ("METER_UNKNOWN",)),
                Operation("reset", "write", ("METER_UNKNOWN", "METER_LOCKED")),
                Operation("total", "sum", ("METER_OVERFLOW",), since=Version(1, 1)),
            ),
        )
        cases = (
            KitCase("read.known", "read", "a known meter reads its count", check),
            KitCase("reset.zero", "reset", "a reset meter reads zero", check),
            KitCase("total.sums", "total", "the total sums every meter", check, Version(1, 1)),
            KitCase("read.after-reset", "read", "a read after a reset", check, Version(1, 1)),
        )
        assert incompatibilities(RELEASED, recorded(grown, kit(grown, *cases))) == []
        assert incompatibilities({"contracts": {}}, RELEASED) == []

    def test_an_addition_claiming_the_released_version_fails(self) -> None:
        grown = contract(
            version=Version(1, 1),
            protocol=MoreOptional,
            operations=(
                Operation("read", "read", ("METER_UNKNOWN",)),
                Operation("reset", "write", ("METER_UNKNOWN", "METER_LOCKED")),
                Operation("total", "read"),
            ),
        )
        cases = (
            KitCase("read.known", "read", "a known meter reads its count", check),
            KitCase("reset.zero", "reset", "a reset meter reads zero", check),
            KitCase("total.sums", "total", "the total sums every meter", check),
        )
        found = incompatibilities(RELEASED, recorded(grown, kit(grown, *cases)))
        assert codes(found) == ["INTERFACE_ADDITION_UNVERSIONED"] * 2
        assert "meter.total is new since 1.0 and claims 1.0" in found[0]

    def test_an_operation_that_starts_changing_state_fails(self) -> None:
        changed = contract(
            operations=(
                Operation("read", "read", ("METER_UNKNOWN",)),
                Operation("reset", "write", ("METER_UNKNOWN", "METER_LOCKED"), changes_state=True),
            )
        )
        cases = (
            KitCase("read.known", "read", "a known meter reads its count", check),
            KitCase("reset.zero", "reset", "a reset meter reads zero", Idempotent(sent, check)),
        )
        found = incompatibilities(RELEASED, recorded(changed, kit(changed, *cases)))
        assert "INTERFACE_SIGNATURE_CHANGED" in codes(found)
        assert any("reset changes_state is True, was False" in problem for problem in found)

    @pytest.mark.parametrize("build", [removed_operation, renamed_error, changed_expectation])
    def test_a_new_major_version_may_break(self, build: Any) -> None:  # noqa: ANN401
        broken = build()
        broken["contracts"]["meter"]["version"] = "2.0"
        assert incompatibilities(RELEASED, broken) == []


class TestRecord:
    def test_the_record_holds_each_contract_with_its_kit(self) -> None:
        entry = RELEASED["contracts"]["meter"]
        assert entry["version"] == "1.0"
        assert sorted(entry["operations"]) == ["read", "reset"]
        assert entry["kit"]["recorded"] == 2

    def test_a_contract_without_a_kit_and_a_kit_without_a_contract_are_refused(self) -> None:
        with pytest.raises(SystemExit, match="INTERFACE_KIT_MISSING"):
            build_record({"meter": contract()}, {})
        with pytest.raises(SystemExit, match="INTERFACE_KIT_ORPHANED"):
            build_record({}, {"meter": kit(contract())})

    def test_the_committed_record_is_current(self) -> None:
        current = build_record(interface_contracts(orvex.contracts), kits(orvex.contracts))
        assert RECORD.read_text("utf-8") == render(current)
        assert interface_record.main(["--check"]) == 0

    def test_a_stale_record_fails_the_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = tmp_path / "record.json"
        stale.write_text(render(RELEASED))
        monkeypatch.setattr(interface_record, "RECORD", stale)
        assert interface_record.main(["--check"]) == 1
        assert interface_record.main([]) == 0
        assert stale.read_text("utf-8") == RECORD.read_text("utf-8")


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


class TestRelease:
    def write(self, root: Path, record: dict[str, Any] | None) -> None:
        path = root / RECORD.relative_to(REPOSITORY_ROOT)
        path.parent.mkdir(parents=True, exist_ok=True)
        if record is not None:
            path.write_text(render(record))
        (root / "README.md").write_text(json.dumps(record))

    def repository(self, tmp_path: Path, released: dict[str, Any] | None) -> Path:
        git(tmp_path, "init", "-q", "-b", "main")
        git(tmp_path, "config", "user.email", "t@example.invalid")
        git(tmp_path, "config", "user.name", "t")
        self.write(tmp_path, released)
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-qm", "release")
        git(tmp_path, "tag", "-a", "v0.4.0-alpha.1", "-m", "release")
        git(tmp_path, "tag", "-a", "store-v0.4.0", "-m", "module tag")
        git(tmp_path, "tag", "v0.10.0a1")
        self.write(tmp_path, {"contracts": {}})
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-qm", "later")
        return tmp_path

    def test_the_record_of_the_last_product_release_is_read(self, tmp_path: Path) -> None:
        root = self.repository(tmp_path, RELEASED)
        assert last_release(root) == "v0.4.0-alpha.1"
        assert released_record(root, "v0.4.0-alpha.1") == RELEASED

    def test_a_release_without_a_record_released_no_interface_contract(
        self, tmp_path: Path
    ) -> None:
        root = self.repository(tmp_path, None)
        assert released_record(root, "v0.4.0-alpha.1") is None

    def test_no_release_tag_fails_closed(self, tmp_path: Path) -> None:
        git(tmp_path, "init", "-q", "-b", "main")
        git(tmp_path, "config", "user.email", "t@example.invalid")
        git(tmp_path, "config", "user.name", "t")
        (tmp_path / "a").write_text("a")
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-qm", "first")
        with pytest.raises(SystemExit, match="INTERFACE_RELEASE_UNKNOWN"):
            last_release(tmp_path)

    def test_the_check_fails_on_a_break_since_the_release(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = self.repository(tmp_path, RELEASED)
        monkeypatch.setattr(interface_compat, "current_record", renamed_error)
        assert interface_compat.run(root) == 1
        assert "INTERFACE_ERROR_CODE_REMOVED" in capsys.readouterr().out
        monkeypatch.setattr(interface_compat, "current_record", lambda: RELEASED)
        assert interface_compat.run(root) == 0
