"""The standalone job (#486 G4): a module built, installed and tested on its own, outside the
repository, with only the contracts distribution beside it.

    python -m tools.ci.standalone <module>
    python -m tools.ci.standalone alternative-result-cache-ipc   # an alternative (#486 G1)

In a temporary directory outside the checkout: copy the module's package, build its sdist and,
from that sdist, its wheel (and the contracts wheel for any other module); create a clean
virtual environment; install the wheels and the module's test dependencies, every version
constrained to the lock file of this Python; run `tools/checks/module_dependencies.py` there;
and run the module's own tests with no repository configuration. A failed step, a failed test
or a skipped test (STANDALONE_TESTS_SKIPPED: a test that did not run never counts) fails the job.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import xml.etree.ElementTree as ElementTree
from collections.abc import Sequence
from pathlib import Path

from tools.checks.alternative_independence import ALTERNATIVES
from tools.ci.pins import pins
from tools.repository import MODULES, REPOSITORY_ROOT, identity

PIP = "pip==26.3"
PYTEST_CONFIG = """[pytest]
addopts = --import-mode=importlib --strict-markers --strict-config -p no:cacheprovider
xfail_strict = true
filterwarnings = error
"""
# Runs one PEP 517 hook of setuptools (the build backend every package declares) in place.
BUILD = (
    "import sys; from setuptools import build_meta as b; "
    "print(getattr(b, sys.argv[1])(sys.argv[2]))"
)


def outside_repository(directory: Path) -> Path:
    if directory.resolve().is_relative_to(REPOSITORY_ROOT.resolve()):
        raise SystemExit(f"STANDALONE_INSIDE_REPOSITORY: {directory} is inside the checkout")
    return directory


def lock_file() -> Path:
    name = "pylock.toml" if sys.version_info >= (3, 15) else "pylock.py314.toml"
    return REPOSITORY_ROOT / name


def constraints(lock: Path) -> list[str]:
    """`name==version` for every package of the lock file."""
    names = [package["name"] for package in tomllib.loads(lock.read_text("utf-8"))["packages"]]
    return pins(lock, names)


def _step(name: str, command: Sequence[str | Path], cwd: Path) -> str:
    print(f"standalone: {name}", flush=True)
    done = subprocess.run([str(part) for part in command], cwd=cwd, capture_output=True, text=True)
    if done.returncode != 0:
        print(done.stdout[-4000:], done.stderr[-4000:], sep="\n")
        raise SystemExit(f"STANDALONE_STEP_FAILED: {name} (exit {done.returncode})")
    return done.stdout


def build(package: Path, work: Path) -> Path:
    """The wheel of `package`, built from its sdist, itself built from a copy of the package."""
    source = work / "source" / package.name
    shutil.copytree(
        package, source, ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__")
    )
    dist = work / "dist"
    dist.mkdir(exist_ok=True)
    built = _step(
        f"sdist of {package.name}", [sys.executable, "-c", BUILD, "build_sdist", dist], source
    )
    sdist = dist / built.strip().splitlines()[-1]
    unpacked = work / "unpacked" / package.name
    with tarfile.open(sdist) as archive:
        archive.extractall(unpacked, filter="data")
    root = next(unpacked.iterdir())
    wheel = _step(
        f"wheel of {package.name} from its sdist",
        [sys.executable, "-c", BUILD, "build_wheel", dist],
        root,
    )
    return dist / wheel.strip().splitlines()[-1]


def located(module: str) -> tuple[Path, str]:
    """The folder of a module's distribution (or of an alternative's) and its package's name."""
    for alternative in ALTERNATIVES:
        if alternative.package.replace("_", "-") == module:
            return REPOSITORY_ROOT / alternative.folder, alternative.package
    if module not in MODULES or module == "server":
        raise SystemExit(f"STANDALONE_UNKNOWN_MODULE: {module}")
    return REPOSITORY_ROOT / "packages" / module, module


def run(module: str) -> int:
    package, name = located(module)
    namespace = str(identity(REPOSITORY_ROOT)["python_namespace"])
    slug = str(identity(REPOSITORY_ROOT)["slug"])
    distribution = f"{slug}-{module}"
    with tempfile.TemporaryDirectory(prefix="standalone-") as directory:
        work = outside_repository(Path(directory))
        wheels = [build(package, work)]
        if module != "contracts":
            wheels.append(build(REPOSITORY_ROOT / "packages" / "contracts", work))
        environment = work / "environment"
        _step("clean environment", [sys.executable, "-m", "venv", environment], work)
        python = environment / "bin" / "python"
        _step("pip", [python, "-m", "pip", "install", "--quiet", PIP], work)
        pinned = work / "constraints.txt"
        pinned.write_text("\n".join(constraints(lock_file())) + "\n", encoding="utf-8")
        manifest = (package / "src" / namespace / name / "module.toml").read_text("utf-8")
        tests = tomllib.loads(manifest)["dependencies"]["tests"]
        _step(
            f"install {distribution}"
            + ("" if module == "contracts" else f" with only {slug}-contracts"),
            [python, "-m", "pip", "install", "--quiet", "--constraint", pinned, *wheels, *tests],
            work,
        )
        suite = work / "suite"
        shutil.copytree(
            package / "tests", suite / "tests", ignore=shutil.ignore_patterns("__pycache__")
        )
        (suite / "pytest.ini").write_text(PYTEST_CONFIG, encoding="utf-8")
        checker = REPOSITORY_ROOT / "tools" / "checks" / "module_dependencies.py"
        print(
            _step(
                "declared dependencies",
                [python, "-I", checker, namespace, distribution, suite / "tests"],
                work,
            )
        )
        report = work / "junit.xml"
        _step(
            "own tests",
            [python, "-I", "-m", "pytest", "-q", f"--junitxml={report}", "tests"],
            suite,
        )
        totals = ElementTree.parse(report).getroot().find("testsuite")
        if totals is None:
            raise SystemExit("STANDALONE_STEP_FAILED: no test report")
        ran, skipped = int(totals.get("tests", "0")), int(totals.get("skipped", "0"))
        if skipped or not ran:
            raise SystemExit(f"STANDALONE_TESTS_SKIPPED: {skipped} of {ran} tests did not run")
    print(f"standalone: {distribution} installs alone and passes its {ran} tests")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        raise SystemExit("usage: python -m tools.ci.standalone <module>")
    return run(arguments[0])


if __name__ == "__main__":
    sys.exit(main())
