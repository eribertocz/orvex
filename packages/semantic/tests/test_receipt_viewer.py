"""P2-27a (#363): the receipt viewer of a `security-context` v1 caller."""

from __future__ import annotations

import sys
from pathlib import Path

from orvex.semantic.policies import read_policies
from orvex.semantic.receipt.viewer import CallerViewer

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from conformance.runners import corpus_execution as ce  # noqa: E402

MODEL = ROOT / "conformance/semantic-corpus/bi-platform/models/CASE-001.yaml"


def context(role: str, groups: tuple[str, ...] = ()) -> dict[str, object]:
    return {**ce.viewer_context(groups), "role": role}


def test_only_visible_members_and_their_datasets_are_seen_and_policies_only_by_a_builder() -> None:
    compiled = ce.compiled_case(MODEL)
    labels = sorted(m.parsed.label for m in compiled.members.values())
    hidden, masked = "sales.region", "sales.sale_id"  # neither is read by `revenue`
    rules = (
        "version: 1\ncolumn_policies:\n"
        f"  - member: {hidden}\n    default: hidden\n"
        f"  - member: {masked}\n    default: masked\n    mask: '*'\n"
    )
    policies = read_policies(rules.encode(), compiled, groups=frozenset(), managed_tables={})
    viewer = CallerViewer(compiled, policies, context("viewer"))
    assert not viewer.builder
    assert not viewer.sees_member(hidden)
    assert not viewer.sees_member(masked)
    assert not viewer.sees_value(masked, {"type": "text", "value": "x"})
    assert not viewer.knows_policy("anything")
    assert not viewer.sees_member("no_such_member")
    assert not viewer.sees_dataset("no_such_dataset")
    visible = [label for label in labels if label not in {hidden, masked}]
    for label in visible:
        assert viewer.sees_member(label)
    builder = CallerViewer(compiled, policies, context("builder"))
    assert builder.builder
    assert builder.knows_policy("anything")
    assert not builder.sees_member(hidden)  # a builder is bound by column policies too
