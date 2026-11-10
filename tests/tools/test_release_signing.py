"""#242: only the publish job may sign, every build checks its SBOM, and the release notes link
the verification guide."""

from __future__ import annotations

from typing import Any

import yaml

from tools.repository import REPOSITORY_ROOT

RELEASE = REPOSITORY_ROOT / ".github/workflows/release.yml"
SIGNING = {"id-token", "attestations"}


def release() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(RELEASE.read_text("utf-8"))
    return loaded


def signers(workflow: dict[str, Any]) -> list[str]:
    found = [""] if SIGNING & set(workflow.get("permissions") or {}) else []
    found += [
        job
        for job, spec in workflow["jobs"].items()
        if SIGNING & set(spec.get("permissions") or {})
    ]
    return found


def test_only_publish_signs_and_the_rest_reads() -> None:
    workflow = release()
    assert signers(workflow) == ["publish"]
    assert workflow["permissions"] == {"contents": "read"}
    publish = workflow["jobs"]["publish"]["permissions"]
    assert publish == {"contents": "write", "id-token": "write", "attestations": "write"}
    workflow["jobs"]["build"]["permissions"] = {"id-token": "write"}
    assert signers(workflow) == ["build", "publish"]


def test_every_build_makes_and_checks_the_sbom_and_sums_it() -> None:
    steps = release()["jobs"]["build"]["steps"]
    runs = [str(step.get("run", "")) for step in steps]
    made = next(i for i, r in enumerate(runs) if "sbom_build.py dist" in r)
    checked = next(i for i, r in enumerate(runs) if "sbom_check.py dist" in r)
    summed = next(i for i, r in enumerate(runs) if "SHA256SUMS" in r)
    assert made < checked < summed
    assert "*.cdx.json" in runs[summed]
    assert not any("if" in steps[i] for i in (made, checked))  # pull requests and dry runs too


def test_publish_attests_the_wheels_and_the_sbom_and_links_the_guide() -> None:
    steps = release()["jobs"]["publish"]["steps"]
    uses = {str(s.get("uses", "")).split("@")[0]: s for s in steps}
    provenance = uses["actions/attest-build-provenance"]["with"]["subject-path"]
    assert "dist/*.whl" in provenance
    assert "dist/*.cdx.json" in provenance
    assert uses["actions/attest-sbom"]["with"]["subject-path"] == "dist/*.whl"
    notes = next(str(s["run"]) for s in steps if "gh release" in str(s.get("run", "")))
    assert "docs/user/verify-a-release.md" in notes
    assert '--notes "$verify" --generate-notes' in notes
    assert (REPOSITORY_ROOT / "docs/user/verify-a-release.md").is_file()
