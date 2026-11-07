"""Conformance of the page-cursor contract (P2-06b, #293).

The suite runs unchanged against every cursor form in `FORMS`: here, a sealed form and a
server-side form written for this test only, with the standard library; P2-12 adds the form it
ships. Positions, fingerprints and snapshots are synthetic.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import replace
from functools import partial

import pytest
from orvex.semantic import SemanticError
from orvex.semantic.result import (
    CursorBinding,
    CursorForm,
    CursorState,
    SnapshotStatus,
    issue_cursor,
    verify_cursor,
)
from orvex.semantic.result.cursors import SealedForm


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _bound(binding: CursorBinding) -> bytes:
    return json.dumps(
        [
            binding.query_fingerprint,
            binding.model_fingerprint,
            binding.permission_fingerprint,
            binding.page_size,
        ]
    ).encode()


class SealedTestForm:
    """Encrypt-then-MAC (HMAC-SHA256 keystream, HMAC-SHA256 tag) under a key held only here, a
    fresh 16-byte nonce per cursor, and the bindings as authenticated data. A test stand-in for
    the AEAD that P2-12 selects under the dependency policy."""

    def __init__(self) -> None:
        self._encryption = secrets.token_bytes(32)
        self._authentication = secrets.token_bytes(32)

    def _stream(self, nonce: bytes, size: int) -> bytes:
        blocks = (size + 31) // 32
        return b"".join(
            hmac.digest(self._encryption, nonce + n.to_bytes(4, "big"), "sha256")
            for n in range(blocks)
        )[:size]

    def _tag(self, binding: CursorBinding, nonce: bytes, sealed: bytes) -> bytes:
        return hmac.digest(self._authentication, _bound(binding) + nonce + sealed, "sha256")

    def issue(self, binding: CursorBinding, state: CursorState) -> str:
        plain = json.dumps([state.data_version, list(state.position)]).encode()
        nonce = secrets.token_bytes(16)
        sealed = bytes(a ^ b for a, b in zip(plain, self._stream(nonce, len(plain)), strict=True))
        return _b64(nonce + sealed + self._tag(binding, nonce, sealed))

    def open(self, cursor: str, binding: CursorBinding) -> CursorState | None:
        try:
            raw = _unb64(cursor)
        except ValueError:
            return None
        if len(raw) < 48 or _b64(raw) != cursor:
            return None
        nonce, sealed, tag = raw[:16], raw[16:-32], raw[-32:]
        if not hmac.compare_digest(tag, self._tag(binding, nonce, sealed)):
            return None
        plain = bytes(a ^ b for a, b in zip(sealed, self._stream(nonce, len(sealed)), strict=True))
        version, position = json.loads(plain)
        return CursorState(version, tuple(position))


class ServerSideTestForm:
    """A random 256-bit handle pointing to state kept here."""

    def __init__(self) -> None:
        self._states: dict[str, tuple[CursorBinding, CursorState]] = {}

    def issue(self, binding: CursorBinding, state: CursorState) -> str:
        handle = _b64(secrets.token_bytes(32))
        self._states[handle] = (binding, state)
        return handle

    def open(self, cursor: str, binding: CursorBinding) -> CursorState | None:
        kept = self._states.get(cursor)
        if kept is None or kept[0] != binding:
            return None
        return kept[1]


class ProductForm(SealedForm):
    """The form `server` uses, with a fresh key each time it is made."""

    def __init__(self) -> None:
        super().__init__(secrets.token_bytes(32))


FORMS: dict[str, Callable[[], CursorForm]] = {
    "sealed": SealedTestForm,
    "server-side": ServerSideTestForm,
    "product": ProductForm,
}

CANARY = "c4n4ry"
BINDING = CursorBinding(
    query_fingerprint="sha256:" + hashlib.sha256(b"q").hexdigest(),
    model_fingerprint="sha256:" + hashlib.sha256(b"m").hexdigest(),
    permission_fingerprint="sha256:" + hashlib.sha256(b"analyst").hexdigest(),
    page_size=50,
)
STATE = CursorState("snapshot-41", ("2026-10-31", "1250.00", 7))
AVAILABLE: Callable[[str], SnapshotStatus] = lambda version: SnapshotStatus.AVAILABLE  # noqa: E731


@pytest.fixture(params=sorted(FORMS))
def form(request: pytest.FixtureRequest) -> CursorForm:
    return FORMS[request.param]()


def refusal(call: Callable[[], object]) -> dict[str, object]:
    with pytest.raises(SemanticError) as caught:
        call()
    assert caught.value.code == "PAGE_CURSOR_INVALID"
    return dict(caught.value.details)


def test_a_genuine_cursor_gives_back_its_position(form: CursorForm) -> None:
    cursor = issue_cursor(form, BINDING, STATE)
    assert verify_cursor(form, cursor, BINDING, AVAILABLE) == STATE


def test_a_changed_character_is_refused(form: CursorForm) -> None:
    cursor = issue_cursor(form, BINDING, STATE)
    for index in (0, len(cursor) // 2, len(cursor) - 1):
        changed = cursor[:index] + ("A" if cursor[index] != "A" else "B") + cursor[index + 1 :]
        assert refusal(partial(verify_cursor, form, changed, BINDING, AVAILABLE)) == {
            "reason": "invalid"
        }


@pytest.mark.parametrize(
    "malformed",
    [lambda c: c[:-1], lambda c: c[: len(c) // 2], lambda c: c + "A", lambda c: "A" * 1025,
     lambda c: "", lambda c: c[:-1] + "=", lambda c: c[:-1] + "+", lambda c: None, lambda c: 7],
)  # fmt: skip
def test_truncated_long_and_malformed_cursors_are_refused(
    form: CursorForm, malformed: Callable[[str], object]
) -> None:
    cursor = malformed(issue_cursor(form, BINDING, STATE))
    assert refusal(lambda: verify_cursor(form, cursor, BINDING, AVAILABLE)) == {"reason": "invalid"}


def test_a_cursor_unknown_to_the_server_is_refused(form: CursorForm) -> None:
    other = type(form)()  # another key, or another server's state
    cursor = issue_cursor(other, BINDING, STATE)
    assert refusal(lambda: verify_cursor(form, cursor, BINDING, AVAILABLE)) == {"reason": "invalid"}
    fresh = _b64(secrets.token_bytes(64))
    assert refusal(lambda: verify_cursor(form, fresh, BINDING, AVAILABLE)) == {"reason": "invalid"}


@pytest.mark.parametrize(
    "change",
    [
        {"permission_fingerprint": "sha256:" + hashlib.sha256(b"another caller").hexdigest()},
        {"query_fingerprint": "sha256:" + hashlib.sha256(b"q2").hexdigest()},
        {"model_fingerprint": "sha256:" + hashlib.sha256(b"m2").hexdigest()},
        {"page_size": 51},
    ],
)
def test_a_cursor_replayed_with_another_binding_is_refused(
    form: CursorForm, change: dict[str, object]
) -> None:
    cursor = issue_cursor(form, BINDING, STATE)
    other = replace(BINDING, **change)  # type: ignore[arg-type]
    assert refusal(lambda: verify_cursor(form, cursor, other, AVAILABLE)) == {"reason": "invalid"}


def test_a_cursor_presented_with_another_data_version_is_refused(form: CursorForm) -> None:
    cursor = issue_cursor(form, BINDING, STATE)
    foreign = lambda version: SnapshotStatus.FOREIGN  # noqa: E731
    assert refusal(lambda: verify_cursor(form, cursor, BINDING, foreign)) == {"reason": "invalid"}


def test_an_expired_snapshot_is_the_only_named_reason(form: CursorForm) -> None:
    cursor = issue_cursor(form, BINDING, STATE)
    seen: list[str] = []

    def expired(version: str) -> SnapshotStatus:
        seen.append(version)
        return SnapshotStatus.EXPIRED

    assert refusal(lambda: verify_cursor(form, cursor, BINDING, expired)) == {"reason": "expired"}
    assert seen == ["snapshot-41"]
    # Another caller never learns that a cursor is genuine: the other checks come first.
    other = replace(BINDING, permission_fingerprint="sha256:" + "0" * 64)
    assert refusal(lambda: verify_cursor(form, cursor, other, expired)) == {"reason": "invalid"}
    forged = cursor[:-2] + ("AA" if not cursor.endswith("AA") else "BB")
    assert refusal(lambda: verify_cursor(form, forged, BINDING, expired)) == {"reason": "invalid"}


def test_a_role_change_between_two_pages_restarts_paging(form: CursorForm) -> None:
    """The verify contract takes the caller's current permission fingerprint: after a change of
    roles, the next page is refused, never served under the old permissions."""
    first_page = issue_cursor(form, BINDING, STATE)
    roles_changed = replace(
        BINDING, permission_fingerprint="sha256:" + hashlib.sha256(b"analyst,auditor").hexdigest()
    )
    assert refusal(lambda: verify_cursor(form, first_page, roles_changed, AVAILABLE)) == {
        "reason": "invalid"
    }


def test_a_cursor_reveals_nothing_it_holds(form: CursorForm) -> None:
    binding = CursorBinding(
        query_fingerprint="sha256:" + "c4" * 32,
        model_fingerprint="sha256:" + "4e" * 32,
        permission_fingerprint="sha256:" + "a7" * 32,
        page_size=987_654_321,
    )
    state = CursorState(f"snapshot-{CANARY}", (f"{CANARY}-key", "31415926535.89", 271828))
    canaries = [CANARY, "c4" * 8, "4e" * 8, "a7" * 8, "987654321", "31415926535", "271828"]
    cursor = issue_cursor(form, binding, state)
    raw = _unb64(cursor)
    for canary in canaries:
        assert canary not in cursor
        assert canary.encode() not in raw
        assert _b64(canary.encode())[:-1] not in cursor
    again = issue_cursor(form, binding, state)
    assert again != cursor  # two cursors for one position cannot be linked
    assert verify_cursor(form, again, binding, AVAILABLE) == state


def test_a_cursor_must_be_in_the_contract_format() -> None:
    class Wrong:
        def issue(self, binding: CursorBinding, state: CursorState) -> str:
            return "not/base64url=="

        def open(self, cursor: str, binding: CursorBinding) -> CursorState | None:
            return None

    with pytest.raises(ValueError, match="URL-safe base64"):
        issue_cursor(Wrong(), BINDING, STATE)


def test_bindings_are_fingerprints_and_a_positive_page_size() -> None:
    with pytest.raises(ValueError, match="fingerprint"):
        replace(BINDING, query_fingerprint="sha256:" + "A" * 64)
    for size in (0, -1, True):
        with pytest.raises(ValueError, match="page_size"):
            replace(BINDING, page_size=size)


def test_the_sealed_form_names_a_genuine_cursor_s_snapshot_to_the_server_only() -> None:
    """P2-27a (#363): the server learns the snapshot a next page pins before compiling the
    model there; any change to the cursor, or another key, names none."""
    form = SealedForm(b"k" * 32)
    cursor = issue_cursor(form, BINDING, STATE)
    assert form.data_version(cursor) == STATE.data_version
    assert "snapshot-41" not in cursor
    assert SealedForm(b"j" * 32).data_version(cursor) is None
    flipped = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    for wrong in (flipped, cursor[:-4], "", "x" * 60, None, 41):
        assert form.data_version(wrong) is None
    assert verify_cursor(form, cursor, BINDING, AVAILABLE) == STATE
    with pytest.raises(SemanticError):
        verify_cursor(form, cursor, replace(BINDING, page_size=51), AVAILABLE)
