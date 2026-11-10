"""#241: every contract error code the product raises has a test."""

from __future__ import annotations

import json
from pathlib import Path

from tools.checks import error_codes
from tools.repository import REPOSITORY_ROOT


def _repository(root: Path, raised: str, tested: str) -> Path:
    schema = root / "contracts" / "store" / "v1" / "schema.json"
    schema.parent.mkdir(parents=True)
    enum = ["STORE_A", "STORE_B", "STORE_RESERVED"]
    schema.write_text(json.dumps({"$defs": {"ErrorCode": {"enum": enum}}}), "utf-8")
    source = root / "packages" / "store" / "src" / "pkg" / "store" / "x.py"
    source.parent.mkdir(parents=True)
    source.write_text(raised, "utf-8")
    generated = root / "packages" / "contracts" / "src" / "v1.py"
    generated.parent.mkdir(parents=True)
    generated.write_text(f"CODES = {enum!r}\n", "utf-8")  # lists every code, raises none
    test = root / "packages" / "store" / "tests" / "test_x.py"
    test.parent.mkdir(parents=True)
    test.write_text(tested, "utf-8")
    reserved = root / error_codes.RESERVED
    reserved.parent.mkdir(parents=True)
    reserved.write_text('[reserved]\nSTORE_RESERVED = { phase = "P9" }\n', "utf-8")
    return root


def untested(root: Path) -> list[str]:
    return [
        f.split()[1] for f in error_codes.problems(root)[0] if f.startswith("ERROR_CODE_UNTESTED")
    ]


def test_a_raised_code_without_a_test_fails_and_a_tested_one_passes(tmp_path: Path) -> None:
    root = _repository(
        tmp_path, 'raise E("STORE_A")\nraise E("STORE_B")\n', 'assert code == "STORE_A"\n'
    )
    failures, notes = error_codes.problems(root)
    assert [f.split()[1] for f in failures] == ["STORE_B"]
    assert notes == ["note: STORE_RESERVED (store) is reserved for P9"]


def test_a_code_in_a_conformance_case_counts_as_tested(tmp_path: Path) -> None:
    root = _repository(tmp_path, 'raise E("STORE_B")\n', "")
    case = root / "conformance" / "cases" / "c.yaml"
    case.parent.mkdir(parents=True)
    case.write_text("expect: {error: STORE_B}\n", "utf-8")
    assert untested(root) == []


def test_a_code_only_inside_a_longer_name_is_not_tested(tmp_path: Path) -> None:
    root = _repository(tmp_path, 'raise E("STORE_A")\n', 'NAME = "STORE_A_LIKE"\n')
    assert untested(root) == ["STORE_A"]


def test_a_code_only_in_a_comment_is_not_tested(tmp_path: Path) -> None:
    root = _repository(tmp_path, 'raise E("STORE_A")\n', "# STORE_A is covered elsewhere\n")
    assert untested(root) == ["STORE_A"]


def test_the_repository_has_no_untested_code() -> None:
    assert error_codes.problems(REPOSITORY_ROOT)[0] == []


def test_a_code_neither_raised_nor_reserved_fails(tmp_path: Path) -> None:
    root = _repository(tmp_path, 'raise E("STORE_A")\n', 'assert c == "STORE_A"\n')
    failures = error_codes.problems(root)[0]
    assert [f.split()[:2] for f in failures] == [["ERROR_CODE_UNRESERVED", "STORE_B"]]


def test_a_reserved_code_that_is_raised_or_unknown_fails(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        'raise E("STORE_A")\nraise E("STORE_B")\nraise E("STORE_RESERVED")\n',
        'assert c in ("STORE_A", "STORE_B", "STORE_RESERVED")\n',
    )
    (root / error_codes.RESERVED).write_text(
        '[reserved]\nSTORE_RESERVED = { phase = "P9" }\nGONE = { phase = "P9" }\n', "utf-8"
    )
    codes = [f.split()[:2] for f in error_codes.problems(root)[0]]
    assert codes == [
        ["ERROR_CODE_RESERVED_BUT_RAISED", "STORE_RESERVED:"],
        ["ERROR_CODE_RESERVED_UNKNOWN", "GONE:"],
    ]


def test_the_http_registry_of_an_openapi_contract_is_read(tmp_path: Path) -> None:
    root = _repository(tmp_path, 'raise E("STORE_A")\nraise E("HTTP_X")\n', 'x = "STORE_A"\n')
    api = root / "contracts" / "http-api" / "v1" / "openapi.yaml"
    api.parent.mkdir(parents=True)
    api.write_text("components:\n  x-error-codes:\n    HTTP_X: An HTTP code.\n", "utf-8")
    assert error_codes.contract_codes(root)["HTTP_X"] == "http-api"
    untested = [
        f.split()[1] for f in error_codes.problems(root)[0] if f.startswith("ERROR_CODE_UNTESTED")
    ]
    assert untested == ["HTTP_X"]
