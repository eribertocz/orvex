"""Tests of the threat-model check (#244): a valid model passes, and each rejection case R1-R7
fails with its code, file, line and reason."""

from __future__ import annotations

import io
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from threat_model import CATEGORIES, OPENAPI, check, main
from threat_model import MODEL as MODEL_PATH

ROWS = "\n".join(
    f"| TM-API-{n} | {category} | A threat. | security.md §2 | tests/test_api.py |"
    for n, category in enumerate(CATEGORIES, start=1)
)
# Line numbers: the inventory fence is line 3, its items lines 4-10, the section line 13, and the
# rows TM-API-1..6 lines 17-22.
MODEL = f"""# Threat model

```inventory
http /a api
http /b api
package one api
package two internal: not reachable yet
contract alpha api
contract beta api
contract http-api api
```

## Surface: api

| ID | Category | Threat | Rule | Verification |
|---|---|---|---|---|
{ROWS}
"""
OPENAPI_TEXT = 'openapi: 3.1.0\npaths:\n  /a:\n    get: {}\n  "/b":\n    get: {}\ncomponents: {}\n'


def repository(root: Path, model: str = MODEL, openapi: str = OPENAPI_TEXT) -> Path:
    """A minimal repository: two paths, two packages, three contracts, the rule documents."""
    (root / "contracts/http-api/v1").mkdir(parents=True)
    (root / OPENAPI).write_text(openapi)
    for folder in (
        "packages/one/tests",
        "packages/two",
        "contracts/alpha",
        "contracts/beta",
        "contracts/_example",
        "docs/adr",
        "docs/architecture",
        "docs/product",
        "tests",
        "conformance/security/scenarios/rows",
    ):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "docs/adr/0027-two-data-routes.md").write_text("# ADR-0027\n")
    (root / "docs/architecture/security.md").write_text("# Security\n\n## 2. Identity\n")
    (root / "docs/product/laws.md").write_text("# Laws\n\n### Law 3 — Security by design\n")
    (root / "docs/roadmap.md").write_text("| P3-01 | Security scenarios | conformance |\n")
    (root / "tests/test_api.py").write_text("")
    (root / "tests/sub").mkdir()
    (root / "packages/one/tests/test_one.py").write_text("")
    (root / "README.md").write_text("")
    (root / MODEL_PATH).write_text(model)
    return root


def first_row(rule: str = "security.md §2", value: str = "tests/test_api.py") -> str:
    """MODEL with TM-API-1's Rule and Verification replaced."""
    return MODEL.replace("security.md §2 | tests/test_api.py |", f"{rule} | {value} |", 1)


class ThreatModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def problems(self, model: str = MODEL, openapi: str = OPENAPI_TEXT) -> list[str]:
        root = Path(tempfile.mkdtemp(dir=self.folder.name))
        return check(repository(root, model, openapi))

    def assert_only(self, found: list[str], expected: str) -> None:
        """`found` holds exactly one problem, `expected`."""
        self.assertEqual(found, [expected])

    def model_problem(self, code: str, line: int, why: str) -> str:
        return f"{code} {MODEL_PATH}:{line}: {why}"

    def test_a_valid_model_passes(self) -> None:
        self.assertEqual(self.problems(), [])

    def test_every_allowed_form_passes(self) -> None:
        values = (
            ("ADR-0027", "roadmap:P3-01"),
            ("laws.md Law 3", "gap: no test yet"),
            ("security.md §2", "packages/one/tests/test_one.py"),
            ("security.md §2", "conformance/security/scenarios/rows"),
            ("security.md §2", "n/a: does not apply"),
        )
        for rule, value in values:
            with self.subTest(value=value):
                self.assertEqual(self.problems(first_row(rule, value)), [])

    def test_r1_an_item_missing_from_the_inventory_is_reported_at_the_fence(self) -> None:
        for item in ("http /b api\n", "package one api\n", "contract beta api\n"):
            with self.subTest(item=item):
                kind, name = item.split()[:2]
                self.assert_only(
                    self.problems(MODEL.replace(item, "")),
                    self.model_problem(
                        "THREAT_MODEL_STALE", 3, f"{kind} {name} is missing from the inventory"
                    ),
                )

    def test_r2_an_inventory_item_that_no_longer_exists(self) -> None:
        model = MODEL.replace(
            "contract http-api api\n", "contract http-api api\npackage gone api\n"
        )
        self.assert_only(
            self.problems(model),
            self.model_problem("THREAT_MODEL_STALE", 11, "package gone no longer exists"),
        )

    def test_r3_an_inventory_item_naming_a_surface_without_a_section(self) -> None:
        self.assert_only(
            self.problems(MODEL.replace("http /b api", "http /b lake")),
            self.model_problem(
                "THREAT_MODEL_INVALID", 5, "http /b names surface 'lake', which has no section"
            ),
        )

    def test_r3_an_internal_item_needs_a_reason(self) -> None:
        self.assert_only(
            self.problems(MODEL.replace("internal: not reachable yet", "internal:")),
            self.model_problem("THREAT_MODEL_INVALID", 7, "package two is internal with no reason"),
        )

    def test_r4_a_surface_missing_a_category(self) -> None:
        model = (
            "\n".join(line for line in MODEL.splitlines() if "| Repudiation |" not in line) + "\n"
        )
        self.assert_only(
            self.problems(model),
            self.model_problem("THREAT_MODEL_INVALID", 13, "surface api has no Repudiation row"),
        )

    def test_r5_an_empty_rule_or_verification(self) -> None:
        self.assert_only(
            self.problems(first_row(rule="")),
            self.model_problem("THREAT_MODEL_INVALID", 17, "TM-API-1: empty Rule"),
        )
        self.assert_only(
            self.problems(first_row(value="")),
            self.model_problem("THREAT_MODEL_INVALID", 17, "TM-API-1: empty Verification"),
        )

    def test_r5_an_unknown_category_or_a_duplicate_id(self) -> None:
        found = self.problems(
            MODEL.replace("| TM-API-1 | Spoofing |", "| TM-API-1 | Phishing |", 1)
        )
        self.assertIn(
            self.model_problem("THREAT_MODEL_INVALID", 17, "TM-API-1: unknown category 'Phishing'"),
            found,
        )
        self.assert_only(
            self.problems(MODEL.replace("TM-API-2 ", "TM-API-1 ", 1)),
            self.model_problem("THREAT_MODEL_INVALID", 18, "TM-API-1 duplicates the ID of line 17"),
        )

    def test_r5_a_table_line_that_is_not_a_row_is_refused_not_skipped(self) -> None:
        for bad in ("| TM-Api-7 |", "|TM-API-7|", "| TM-API-7a |"):
            with self.subTest(bad=bad):
                line = f"{bad} Spoofing | A threat. | security.md §2 | tests/test_api.py |"
                found = self.problems(MODEL + line + "\n")
                self.assertEqual(len(found), 1, found)
                self.assertTrue(
                    found[0].startswith(f"THREAT_MODEL_INVALID {MODEL_PATH}:23: table line"), found
                )

    def test_r5_a_second_section_for_one_surface(self) -> None:
        model = MODEL + "\n## Surface: api\n"
        self.assert_only(
            self.problems(model),
            self.model_problem("THREAT_MODEL_INVALID", 24, "surface api has a second section"),
        )

    def test_r5_an_inventory_item_listed_twice(self) -> None:
        model = MODEL.replace("http /a api\n", "http /a api\nhttp /a api\n")
        self.assert_only(
            self.problems(model),
            self.model_problem(
                "THREAT_MODEL_INVALID", 5, "http /a is listed twice in the inventory"
            ),
        )

    def test_r5_a_malformed_or_unclosed_inventory_and_a_row_outside_a_surface(self) -> None:
        self.assertIn(
            self.model_problem(
                "THREAT_MODEL_INVALID",
                4,
                "inventory line 'path /a api' is not '<kind> <name> <surface>'",
            ),
            self.problems(MODEL.replace("http /a api", "path /a api")),
        )
        unclosed = MODEL.replace("contract http-api api\n```\n", "contract http-api api\n")
        self.assertIn(
            self.model_problem(
                "THREAT_MODEL_INVALID",
                len(unclosed.splitlines()),
                "the inventory block is not closed",
            ),
            self.problems(unclosed),
        )
        outside = MODEL.replace(
            "# Threat model\n", "# Threat model\n| TM-API-9 | Spoofing | x | y | z |\n", 1
        )
        self.assertIn(
            self.model_problem(
                "THREAT_MODEL_INVALID", 2, "row TM-API-9 is outside a surface section"
            ),
            self.problems(outside),
        )

    def test_a_model_without_an_inventory_block(self) -> None:
        model = MODEL[: MODEL.index("```inventory")] + MODEL[MODEL.index("## Surface: api") :]
        self.assertIn(
            self.model_problem("THREAT_MODEL_INVALID", 1, "the model has no inventory block"),
            self.problems(model),
        )

    def test_r6_a_rule_naming_nothing_that_exists(self) -> None:
        cases = {
            "security.md §9": "Rule 'security.md §9' names no security.md heading",
            "ADR-0099": "Rule 'ADR-0099' names no ADR file",
            "laws.md Law 42": "Rule 'laws.md Law 42' names no law",
            "elsewhere": "Rule 'elsewhere' is not a security.md section, an ADR or a law",
        }
        for rule, why in cases.items():
            with self.subTest(rule=rule):
                self.assert_only(
                    self.problems(first_row(rule=rule)),
                    self.model_problem("THREAT_MODEL_INVALID", 17, f"TM-API-1: {why}"),
                )

    def test_r7_a_verification_of_no_allowed_form(self) -> None:
        tail = "is not a test file, a scenario path, a roadmap ID, a gap or n/a"
        plain = "is not written as its plain repository path"
        cases = {
            "tests/missing.py": f"Verification 'tests/missing.py' {tail}",
            "README.md": f"Verification 'README.md' {tail}",
            "tests": f"Verification 'tests' {tail}",
            "../outside.py": "Verification '../outside.py' points outside the repository",
            "tests/../README.md": f"Verification 'tests/../README.md' {plain}",
            "conformance/security/scenarios/../../README.md": (
                f"Verification 'conformance/security/scenarios/../../README.md' {plain}"
            ),
            "tests/sub": f"Verification 'tests/sub' {tail}",
            "tested elsewhere": f"Verification 'tested elsewhere' {tail}",
            "roadmap:P9-99": "Verification 'roadmap:P9-99' names no roadmap ID",
            "gap:": "Verification 'gap:' has no reason or no valid ID",
        }
        for value, why in cases.items():
            with self.subTest(value=value):
                self.assert_only(
                    self.problems(first_row(value=value)),
                    self.model_problem("THREAT_MODEL_INVALID", 17, f"TM-API-1: {why}"),
                )

    def test_r7_n_a_only_on_a_category_with_no_other_row(self) -> None:
        beside = first_row(value="n/a: read only").replace(
            "| TM-API-6 |",
            "| TM-API-7 | Spoofing | Another. | security.md §2 | tests/test_api.py |\n| TM-API-6 |",
        )
        self.assert_only(
            self.problems(beside),
            self.model_problem(
                "THREAT_MODEL_INVALID",
                17,
                "TM-API-1: Verification 'n/a: read only': n/a only on a category with no other row",
            ),
        )

    def test_openapi_paths_are_read_through_comments_inline_values_and_colons(self) -> None:
        openapi = (
            "openapi: 3.1.0\n"
            "paths:  # the routes\n"
            "  /a:  # first\n"
            "    get: {}\n"
            "# a comment at column 0 inside the block\n"
            "\n"
            "  '/b': {$ref: b.yaml}\n"
            "  /v1/x:run:\n"
            "    post: {}\n"
            "components: {}\n"
        )
        found = self.problems(
            MODEL.replace("http /b api\n", "http /b api\nhttp /v1/x:run api\n"), openapi
        )
        self.assertEqual(found, [])

    def test_an_unreadable_path_key_fails_closed(self) -> None:
        openapi = "openapi: 3.1.0\npaths:\n  /a:\n    get: {}\n  not-a-path:\n    get: {}\n"
        found = self.problems(MODEL.replace("http /b api\n", ""), openapi)
        self.assertIn(f"THREAT_MODEL_INVALID {OPENAPI}:5: 'not-a-path:' is not a path key", found)

    def test_a_missing_or_empty_paths_mapping_fails_closed(self) -> None:
        expected = f"THREAT_MODEL_INVALID {OPENAPI}:1: no top-level `paths:` mapping with a path"
        for openapi in ("openapi: 3.1.0\ncomponents: {}\n", "openapi: 3.1.0\npaths: {}\n"):
            with self.subTest(openapi=openapi):
                self.assertIn(expected, self.problems(openapi=openapi))

    def test_main_exits_1_when_the_repository_cannot_be_read(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["threat_model.py", str(Path(self.folder.name) / "missing")])
        self.assertEqual(code, 1)
        self.assertTrue(
            output.getvalue().startswith(f"THREAT_MODEL_INVALID {MODEL_PATH}:1: cannot read")
        )

    def test_the_repository_model_passes_in_under_five_seconds(self) -> None:
        root = Path(__file__).resolve().parents[3]
        start = time.monotonic()
        self.assertEqual(check(root), [])
        self.assertLess(time.monotonic() - start, 5)


if __name__ == "__main__":
    unittest.main()
