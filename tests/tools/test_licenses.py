from pathlib import Path

import pytest

from tools.licenses.check_licenses import (
    Dependency,
    Policy,
    expression_allowed,
    load_policy,
    npm_dependencies,
    violations,
)

POLICY = Policy(
    runtime=frozenset({"MIT", "Apache-2.0", "BSD-2-Clause"}),
    development=frozenset({"MPL-2.0"}),
    exceptions={},
)


@pytest.mark.parametrize(
    ("expression", "allowed"),
    [
        ("MIT", True),
        ("GPL-3.0-only", False),
        ("Apache-2.0 OR BSD-2-Clause", True),
        ("GPL-3.0-only OR MIT", True),
        ("MIT AND GPL-3.0-only", False),
        ("(MIT AND Apache-2.0)", True),
    ],
)
def test_spdx_expressions(expression: str, allowed: bool) -> None:
    assert expression_allowed(expression, POLICY.runtime) is allowed


def test_gpl_dependency_is_rejected() -> None:
    gpl = Dependency("npm", "some-gpl", "GPL-3.0-only", runtime=False)
    assert violations([gpl], POLICY) == [gpl]


def test_dependency_without_license_is_rejected() -> None:
    unknown = Dependency("python", "mystery", None, runtime=False)
    assert violations([unknown], POLICY) == [unknown]


def test_mpl_is_allowed_for_development_only() -> None:
    development = Dependency("python", "hypothesis", "MPL-2.0", runtime=False)
    runtime = Dependency("python", "hypothesis", "MPL-2.0", runtime=True)
    assert violations([development, runtime], POLICY) == [runtime]


def test_exception_needs_a_reason(tmp_path: Path) -> None:
    allow_list = tmp_path / "allowed.toml"
    allow_list.write_text('runtime = ["MIT"]\ndevelopment = []\n[exceptions]\nthing = ""\n')
    with pytest.raises(ValueError, match="no reason"):
        load_policy(allow_list)


def test_npm_lock_classifies_runtime_and_development(tmp_path: Path) -> None:
    lock = tmp_path / "package-lock.json"
    lock.write_text(
        '{"packages": {"": {}, '
        '"node_modules/svelte": {"license": "MIT"}, '
        '"node_modules/vitest": {"license": "MIT", "dev": true}, '
        '"node_modules/odd": {"license": {"type": "MIT"}, "dev": true}}}'
    )
    found = {dependency.name: dependency for dependency in npm_dependencies(lock)}
    assert found["svelte"].runtime
    assert not found["vitest"].runtime
    assert found["odd"].license is None


def test_repository_policy_loads() -> None:
    policy = load_policy()
    assert "Apache-2.0" in policy.runtime
    assert "MPL-2.0" not in policy.runtime
