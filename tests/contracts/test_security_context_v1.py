"""P2-09, #306, #486: the access package ships the `security-context` v1 contract's own schema,
and the access tests carry the contract's own examples."""

from __future__ import annotations

from tools.repository import REPOSITORY_ROOT

CONTRACT = REPOSITORY_ROOT / "contracts/security-context/v1"
ACCESS = REPOSITORY_ROOT / "packages/access"


def test_the_package_ships_the_contracts_own_schema() -> None:
    packaged = ACCESS / "src/orvex/access/security-context-v1.json"
    assert packaged.read_bytes() == (CONTRACT / "schema.json").read_bytes()


def test_the_access_tests_carry_the_contracts_own_examples() -> None:
    carried = ACCESS / "tests/fixtures/security-context/examples"
    expected = sorted(p.relative_to(CONTRACT / "examples") for p in CONTRACT.glob("examples/*/*"))
    assert sorted(p.relative_to(carried) for p in carried.glob("*/*")) == expected
    for relative in expected:
        assert (carried / relative).read_bytes() == (CONTRACT / "examples" / relative).read_bytes()
