import subprocess
import tempfile
import unittest
from pathlib import Path

from aggregate import Verdict, decide
from changes import relevant_files

PATHS = ["Dockerfile", ".dockerignore", "tools/benchmarks/**"]


class RelevantFilesTest(unittest.TestCase):
    """Which changed files make a scoped check apply (#267)."""

    def test_exact_names_and_folders_match(self) -> None:
        changed = ["Dockerfile", "tools/benchmarks/a/b.py", "docs/x.md", "tools/benchmarksx.py"]
        self.assertEqual(relevant_files(changed, PATHS), ["Dockerfile", "tools/benchmarks/a/b.py"])

    def test_nothing_relevant_is_an_empty_list(self) -> None:
        self.assertEqual(relevant_files(["docs/x.md", "Dockerfile.old"], PATHS), [])


class ChangesScriptTest(unittest.TestCase):
    """The `changes` job: a non-PR event always applies; a failure fails the job."""

    def run_script(self, env: dict[str, str], cwd: Path) -> tuple[int, str]:
        output = cwd / "out.txt"
        script = Path(__file__).with_name("changes.py")
        process = subprocess.run(
            ["python3", str(script)],
            cwd=cwd,
            env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(output), **env},
            capture_output=True,
            text=True,
        )
        return process.returncode, output.read_text() if output.exists() else ""

    def repository(self, folder: Path) -> tuple[str, str]:
        def git(*arguments: str) -> str:
            return subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
                cwd=folder,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        git("init", "--quiet")
        (folder / "README.md").write_text("a")
        git("add", "-A")
        git("commit", "--quiet", "-m", "base")
        base = git("rev-parse", "HEAD")
        (folder / "Dockerfile").write_text("FROM x")
        git("add", "-A")
        git("commit", "--quiet", "-m", "head")
        return base, git("rev-parse", "HEAD")

    def test_a_relevant_change_applies(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            base, head = self.repository(folder)
            code, out = self.run_script(
                {
                    "EVENT": "pull_request",
                    "BASE": base,
                    "HEAD": head,
                    "RELEVANT_PATHS": "Dockerfile",
                },
                folder,
            )
            self.assertEqual((code, out.strip()), (0, "relevant=true"))

    def test_no_relevant_change_is_proven_not_applicable(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            base, head = self.repository(folder)
            code, out = self.run_script(
                {"EVENT": "pull_request", "BASE": base, "HEAD": head, "RELEVANT_PATHS": "web/**"},
                folder,
            )
            self.assertEqual((code, out.strip()), (0, "relevant=false"))

    def changed_by(self, folder: Path, change: str) -> tuple[int, str]:
        def git(*arguments: str) -> str:
            return subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
                cwd=folder,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        _, head = self.repository(folder)
        if change == "rename":
            git("mv", "Dockerfile", "docker.old")
        else:
            (folder / "tools" / "benchmarks").mkdir(parents=True)
            (folder / "tools" / "benchmarks" / "café.py").write_text("x")
        git("add", "-A")
        git("commit", "--quiet", "-m", change)
        return self.run_script(
            {
                "EVENT": "pull_request",
                "BASE": head,
                "HEAD": git("rev-parse", "HEAD"),
                "RELEVANT_PATHS": "Dockerfile\ntools/benchmarks/**",
            },
            folder,
        )

    def test_a_file_moved_out_of_a_relevant_path_applies(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            self.assertEqual(self.changed_by(Path(name), "rename")[1].strip(), "relevant=true")

    def test_a_name_with_non_ascii_characters_applies(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            self.assertEqual(self.changed_by(Path(name), "non-ascii")[1].strip(), "relevant=true")

    def test_an_unreadable_base_fails_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            _, head = self.repository(folder)
            code, out = self.run_script(
                {"EVENT": "pull_request", "BASE": "0" * 40, "HEAD": head, "RELEVANT_PATHS": "x"},
                folder,
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(out, "")

    def test_missing_inputs_fail(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            code, out = self.run_script(
                {"EVENT": "pull_request", "RELEVANT_PATHS": "x"}, Path(name)
            )
            self.assertNotEqual(code, 0)
            self.assertEqual(out, "")
            code, out = self.run_script(
                {"EVENT": "pull_request", "BASE": "a", "HEAD": "b"}, Path(name)
            )
            self.assertNotEqual(code, 0)

    def test_other_events_always_apply(self) -> None:
        for event in ("push", "workflow_dispatch", "schedule"):
            with tempfile.TemporaryDirectory() as name:
                code, out = self.run_script({"EVENT": event, "RELEVANT_PATHS": "x"}, Path(name))
                self.assertEqual((code, out.strip()), (0, "relevant=true"), event)


class AggregateTest(unittest.TestCase):
    """The scoped check's required aggregator: fails closed; `Not applicable` only when proven."""

    def test_the_table(self) -> None:
        cases = [
            # changes, relevant, result -> verdict
            ("failure", "", "skipped", Verdict.FAILED),
            ("cancelled", "", "skipped", Verdict.FAILED),
            ("skipped", "", "skipped", Verdict.FAILED),
            ("success", "", "skipped", Verdict.FAILED),
            ("success", "maybe", "success", Verdict.FAILED),
            ("success", "true", "success", Verdict.PASSED),
            ("success", "true", "failure", Verdict.FAILED),
            ("success", "true", "skipped", Verdict.FAILED),
            ("success", "true", "cancelled", Verdict.FAILED),
            ("success", "false", "skipped", Verdict.NOT_APPLICABLE),
            ("success", "false", "success", Verdict.FAILED),
            ("success", "false", "failure", Verdict.FAILED),
        ]
        for changes, relevant, result, expected in cases:
            with self.subTest(changes=changes, relevant=relevant, result=result):
                self.assertEqual(decide(changes, relevant, result), expected)

    def run_script(self, env: dict[str, str]) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as name:
            summary = Path(name) / "summary.md"
            script = Path(__file__).with_name("aggregate.py")
            process = subprocess.run(
                ["python3", str(script)],
                env={"PATH": "/usr/bin:/bin", "GITHUB_STEP_SUMMARY": str(summary), **env},
                capture_output=True,
                text=True,
            )
            text = summary.read_text() if summary.exists() else ""
            return process.returncode, process.stdout, text

    def test_not_applicable_is_stated_explicitly(self) -> None:
        code, out, summary = self.run_script(
            {
                "CHECK": "docker",
                "CHANGES": "success",
                "RELEVANT": "false",
                "RESULT": "skipped",
                "RELEVANT_PATHS": "Dockerfile\n.dockerignore\n",
            }
        )
        self.assertEqual(code, 0)
        self.assertIn("::notice title=Not applicable::", out)
        self.assertIn("Not applicable: no change under Dockerfile, .dockerignore", summary)

    def test_a_failure_exits_non_zero_and_says_why(self) -> None:
        code, out, summary = self.run_script(
            {"CHECK": "docker", "CHANGES": "success", "RELEVANT": "true", "RESULT": "failure"}
        )
        self.assertEqual(code, 1)
        self.assertIn("::error title=docker::", out)
        self.assertIn("failure", summary)


if __name__ == "__main__":
    unittest.main()
