"""The lake-side state a caller's permissions depend on, for one request (#440): the policy
model's fingerprint and the snapshot at which the managed-lake tables the policies read are
in force. Both go into the permission fingerprint beside what `access` reads.

- **A first page or any other question** reads the lake's current snapshot: its policies and
  that snapshot.
- **A next page** reads its data at the snapshot its cursor pins (Owner precision 2), but its
  permissions are those in force now (Q6 A). The policies are the current ones. The mapping
  state is the cursor's snapshot only while no table any of those policies reads, or the pinned
  ones read, has changed since; otherwise it is the current snapshot. So a revoked mapping
  changes the fingerprint, and the cursor is refused before any row is read.
- **Fail closed.** When the current permissions cannot be read (the model is now invalid or
  gone, or the lake cannot say what changed), the next page is `PAGE_CURSOR_INVALID`: it is
  never served on the permissions of its first page.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from orvex.contracts.store.v1 import ChangesRequest
from orvex.semantic.errors import SemanticError
from orvex.semantic.policies import PolicyModel
from orvex.store.errors import StoreError
from orvex.store.lake import Lake


@dataclass(frozen=True, slots=True)
class PermissionState:
    policy_fingerprint: str
    mapping_snapshot: int


def _refused() -> SemanticError:
    return SemanticError("PAGE_CURSOR_INVALID", {"reason": "invalid"})


def permission_state(
    lake: Lake,
    snapshot: int,
    policies: PolicyModel,
    current_policies: Callable[[int], PolicyModel | None],
) -> PermissionState:
    """The state for a request reading `snapshot` under `policies` (those at `snapshot`).
    `current_policies(n)` gives the policies at snapshot `n`, or None when there are none."""
    current = lake.current_snapshot().id
    if snapshot == current:
        return PermissionState(policies.fingerprint, snapshot)
    now = current_policies(current)
    if now is None:
        raise _refused()
    reads = sorted(policies.managed_reads | now.managed_reads)
    if now.fingerprint != policies.fingerprint:
        return PermissionState(now.fingerprint, current)
    if not reads:
        return PermissionState(now.fingerprint, snapshot)
    try:
        found = lake.changes(
            ChangesRequest(
                kind="changes",
                tables=reads,  # type: ignore[arg-type]
                since=snapshot,
                snapshot=current,
                count_removed=False,
            )
        )
    except StoreError as error:
        raise _refused() from error
    changed = any(table.changed for table in found.tables)
    return PermissionState(now.fingerprint, current if changed else snapshot)
