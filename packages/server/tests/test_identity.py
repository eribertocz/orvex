from pathlib import Path

import pytest
from orvex.server.identity import IdentityError, load_identity, parse_identity

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PACKAGED = REPOSITORY_ROOT / "packages/server/src/orvex/server/identity.toml"

VALID = """
[product]
name = "Quadrant"
slug = "quadrant"
python_namespace = "quadrant"
ossie_vendor_name = "QUADRANT"
previous_vendor_names = ["ORVEX"]
"""


def test_packaged_copy_equals_the_repository_file() -> None:
    assert PACKAGED.read_bytes() == (REPOSITORY_ROOT / "identity.toml").read_bytes()


def test_load_identity_reads_the_packaged_file() -> None:
    identity = load_identity()
    assert identity == load_identity(REPOSITORY_ROOT / "identity.toml")
    assert identity.slug == identity.python_namespace


def test_parse_valid_identity() -> None:
    identity = parse_identity(VALID)
    assert identity.name == "Quadrant"
    assert identity.previous_vendor_names == ("ORVEX",)


@pytest.mark.parametrize(
    ("replacement", "field"),
    [
        (('name = "Quadrant"', ""), "name"),
        (('name = "Quadrant"', 'name = "  "'), "name"),
        (('slug = "quadrant"', 'slug = "Astro labe"'), "slug"),
        (('python_namespace = "quadrant"', 'python_namespace = "class"'), "python_namespace"),
        (('python_namespace = "quadrant"', 'python_namespace = "my-name"'), "python_namespace"),
        (('ossie_vendor_name = "QUADRANT"', 'ossie_vendor_name = "astro"'), "ossie_vendor_name"),
        (
            ('previous_vendor_names = ["ORVEX"]', "previous_vendor_names = [1]"),
            "previous_vendor_names",
        ),
    ],
)
def test_invalid_identity_names_the_field(replacement: tuple[str, str], field: str) -> None:
    with pytest.raises(IdentityError, match="IDENTITY_INVALID") as error:
        parse_identity(VALID.replace(*replacement))
    assert error.value.field == field


def test_missing_product_table() -> None:
    with pytest.raises(IdentityError) as error:
        parse_identity("name = 'x'\n")
    assert error.value.field == "product"
