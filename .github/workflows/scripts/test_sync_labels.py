import unittest
from pathlib import Path

from sync_labels import Label, LabelFileError, parse_label_file, plan_sync

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class ParseTests(unittest.TestCase):
    def test_repository_label_file_parses(self) -> None:
        labels = parse_label_file(
            (REPOSITORY_ROOT / ".github" / "labels.yml").read_text(encoding="utf-8")
        )
        names = {label.name for label in labels}
        self.assertIn("owner-merge", names)
        self.assertIn("status:spec-ready", names)

    def test_parses_one_label(self) -> None:
        text = (
            '# comment\n- { name: "type:feat", color: "1F6FEB", description: "New capability" }\n'
        )
        self.assertEqual(parse_label_file(text), [Label("type:feat", "1f6feb", "New capability")])

    def test_rejects_unknown_line(self) -> None:
        with self.assertRaises(LabelFileError):
            parse_label_file("- name: type:feat\n")

    def test_rejects_duplicates(self) -> None:
        line = '- { name: "a", color: "000000", description: "" }\n'
        with self.assertRaises(LabelFileError):
            parse_label_file(line + line)


class PlanTests(unittest.TestCase):
    def test_plan_creates_updates_and_deletes(self) -> None:
        existing = [Label("bug", "d73a4a", "Something isn't working"), Label("a", "000000", "old")]
        desired = [Label("a", "000000", "new"), Label("b", "ffffff", "")]
        plan = plan_sync(existing, desired)
        self.assertEqual(plan.create, (Label("b", "ffffff", ""),))
        self.assertEqual(plan.update, (Label("a", "000000", "new"),))
        self.assertEqual(plan.delete, ("bug",))

    def test_plan_is_empty_when_equal(self) -> None:
        labels = [Label("a", "000000", "x")]
        plan = plan_sync(labels, labels)
        self.assertEqual((plan.create, plan.update, plan.delete), ((), (), ()))


if __name__ == "__main__":
    unittest.main()
