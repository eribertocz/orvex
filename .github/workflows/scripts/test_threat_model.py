"""Tests of the threat-model check (#244): a valid model passes, and each rejection case R1-R7
fails with its code, file and line."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from threat_model import CATEGORIES, check

ROWS = "\n".join(
    f"| TM-API-{n} | {category} | A threat. | security.md §2 | tests/test_api.py |"
    for n, category in enumerate(CATEGORIES, start=1)
)
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


def repository(root: Path, model: str = MODEL) -> Path:
    """A minimal repository: two paths, two packages, two contracts, the rule documents."""
    (root / "contracts/http-api/v1").mkdir(parents=True)
    (root / "contracts/http-api/v1/openapi.yaml").write_text(
        'openapi: 3.1.0\npaths:\n  /a:\n    get: {}\n  "/b":\n    get: {}\ncomponents: {}\n'
    )
    for folder in (
        "packages/one",
        "packages/two",
        "contracts/alpha",
        "contracts/beta",
        "contracts/_example",
        "docs/adr",
        "docs/architecture",
        "docs/product",
        "tests",
    ):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "docs/adr/0027-two-data-routes.md").write_text("# ADR-0027\n")
    (root / "docs/architecture/security.md").write_text("# Security\n\n## 2. Identity\n")
    (root / "docs/product/laws.md").write_text("# Laws\n\n### Law 3 — Security by design\n")
    (root / "docs/roadmap.md").write_text("| P3-01 | Security scenarios | conformance |\n")
    (root / "tests/test_api.py").write_text("")
    (root / "docs/architecture/threat-model.md").write_text(model)
    return root


class ThreatModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def problems(self, model: str = MODEL) -> list[str]:
        return check(repository(self.root, model))

    def assert_refused(self, model: str, code: str, line: int, needle: str) -> None:
        found = self.problems(model)
        self.assertTrue(
            any(
                p.startswith(f"{code} docs/architecture/threat-model.md:{line}:") and needle in p
                for p in found
            ),
            found,
        )

    def test_a_valid_model_passes(self) -> None:
        self.assertEqual(self.problems(), [])

    def test_every_allowed_form_passes(self) -> None:
        model = MODEL.replace(
            "security.md §2 | tests/test_api.py |", "ADR-0027 | roadmap:P3-01 |", 1
        ).replace("| security.md §2 | tests/test_api.py |", "| laws.md Law 3 | gap: no test |", 1)
        self.assertEqual(self.problems(model), [])

    def test_r1_a_path_package_or_contract_missing_from_the_inventory(self) -> None:
        for item in ("http /b api\n", "package one api\n", "contract beta api\n"):
            with self.subTest(item=item):
                root = Path(tempfile.mkdtemp(dir=self.folder.name))
                found = check(repository(root, MODEL.replace(item, "")))
                name = item.split()[1]
                self.assertTrue(
                    any(p.startswith("THREAT_MODEL_STALE ") and name in p for p in found), found
                )

    def test_r2_an_inventory_item_that_no_longer_exists(self) -> None:
        model = MODEL.replace("contract beta api\n", "contract beta api\npackage gone api\n")
        self.assert_refused(model, "THREAT_MODEL_STALE", 10, "gone")

    def test_r3_an_inventory_item_naming_a_surface_without_a_section(self) -> None:
        self.assert_refused(
            MODEL.replace("http /b api", "http /b lake"), "THREAT_MODEL_INVALID", 5, "lake"
        )

    def test_r4_a_surface_missing_a_category(self) -> None:
        model = "\n".join(line for line in MODEL.splitlines() if "| Repudiation |" not in line)
        self.assert_refused(model + "\n", "THREAT_MODEL_INVALID", 13, "Repudiation")

    def test_r5_empty_rule_or_verification_unknown_category_or_duplicate_id(self) -> None:
        cases = {
            "empty rule": (
                MODEL.replace(
                    "| security.md §2 | tests/test_api.py |", "|  | tests/test_api.py |", 1
                ),
                "Rule",
            ),
            "empty verification": (
                MODEL.replace("| security.md §2 | tests/test_api.py |", "| security.md §2 |  |", 1),
                "Verification",
            ),
            "unknown category": (MODEL.replace("| Spoofing |", "| Phishing |", 1), "Phishing"),
            "duplicate id": (MODEL.replace("TM-API-2 ", "TM-API-1 ", 1), "TM-API-1"),
        }
        for name, (model, needle) in cases.items():
            with self.subTest(name):
                found = check(repository(Path(tempfile.mkdtemp(dir=self.folder.name)), model))
                self.assertTrue(
                    any(p.startswith("THREAT_MODEL_INVALID ") and needle in p for p in found), found
                )

    def test_r6_a_rule_naming_a_heading_or_adr_that_does_not_exist(self) -> None:
        for rule in ("security.md §9", "ADR-0099", "laws.md Law 42", "elsewhere"):
            with self.subTest(rule=rule):
                model = MODEL.replace("security.md §2", rule, 1)
                found = check(repository(Path(tempfile.mkdtemp(dir=self.folder.name)), model))
                self.assertTrue(
                    any(p.startswith("THREAT_MODEL_INVALID ") and rule in p for p in found), found
                )

    def test_r7_a_verification_path_that_does_not_exist_or_of_no_allowed_form(self) -> None:
        for value in ("tests/missing.py", "roadmap:P9-99", "gap:", "tested elsewhere"):
            with self.subTest(value=value):
                model = MODEL.replace("tests/test_api.py", value, 1)
                found = check(repository(Path(tempfile.mkdtemp(dir=self.folder.name)), model))
                self.assertTrue(
                    any(p.startswith("THREAT_MODEL_INVALID ") and value in p for p in found), found
                )

    def test_r7_n_a_only_on_a_category_with_no_other_row(self) -> None:
        alone = MODEL.replace(
            "| security.md §2 | tests/test_api.py |", "| security.md §2 | n/a: read only |", 1
        )
        self.assertEqual(self.problems(alone), [])
        beside = alone.replace(
            "| TM-API-6 |",
            "| TM-API-7 | Spoofing | Another. | security.md §2 | tests/test_api.py |\n| TM-API-6 |",
        )
        found = check(repository(Path(tempfile.mkdtemp(dir=self.folder.name)), beside))
        self.assertTrue(
            any(p.startswith("THREAT_MODEL_INVALID ") and "n/a" in p for p in found), found
        )

    def test_the_repository_model_passes_in_under_five_seconds(self) -> None:
        root = Path(__file__).resolve().parents[3]
        start = time.monotonic()
        self.assertEqual(check(root), [])
        self.assertLess(time.monotonic() - start, 5)


if __name__ == "__main__":
    unittest.main()
