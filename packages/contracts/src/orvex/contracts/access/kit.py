"""The conformance kit of the `access` interface contract (#486 G1, G3): every operation, the
security cases its consumers rely on (sign-in, sessions, revocation, attributes, the security
context, fail-closed refusals) and one idempotency case per operation that changes state.

Run it with `orvex.contracts.kit_run.verify_kit(ACCESS_KIT, make)`, where `make` builds the
implementation composed with `KitProviderFetcher` (`kit_world.py`). It has no skip list.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from orvex.contracts.access.interface import ACCESS
from orvex.contracts.access.kit_world import (
    KIT_CLOCK,
    KIT_PROVIDER,
    PASSWORD,
    PROJECT,
    World,
    call,
    code_of,
    member,
    signed_in,
    world,
)
from orvex.contracts.access.values import (
    Attribute,
    ContextFixture,
    ContextMember,
    FingerprintInputs,
    Instance,
    RoleLimit,
)
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.kit import Idempotent, Kit, KitCase
from orvex.contracts.kit_client import KitClient

POLICIES = "sha256:" + "0" * 64
MAPPING = {"lake": "7"}
NEW_PASSWORD = "another-kit-password-1"
UNKNOWN = Instance("not-an-instance")


def _expect(condition: bool, text: str) -> None:
    if not condition:
        raise AssertionError(text)


def _refused(client: KitClient, code: str, actor: str, operation: str, *arguments: object) -> None:
    found = code_of(client, actor, operation, *arguments)
    _expect(found == code, f"{operation}: expected {code}, got {found}")


# --- instance ------------------------------------------------------------------------------


def open_and_close(client: KitClient) -> None:
    found = world(client)
    instance = found.opened
    _expect(call(client, "system", "oidc_providers", instance) == (KIT_PROVIDER,), "providers")
    call(client, "system", "close", instance)
    found.instance = None
    _refused(client, "ACCESS_INSTANCE_UNKNOWN", "system", "oidc_providers", instance)


def unknown_instance_refused(client: KitClient) -> None:
    for operation, arguments in (
        ("session", ("s",)),
        ("users", (None,)),
        ("role_of", ("u", PROJECT)),
        ("query_limits", ()),
    ):
        _refused(client, "ACCESS_INSTANCE_UNKNOWN", "system", operation, UNKNOWN, *arguments)


def initialize_writes_recovery(client: KitClient) -> None:
    found = world(client)
    _expect(call(client, "system", "initialize", found.opened) is True, "first initialization")
    _expect(os.path.exists(found.configuration.recovery_file), "no recovery file")
    _expect(call(client, "system", "initialize", found.opened) is False, "second initialization")


def administrator_needs_recovery(client: KitClient) -> None:
    found = signed_in(client)
    _refused(
        client,
        "ACCESS_FORBIDDEN",
        "system",
        "create_administrator",
        found.opened,
        "second",
        "Second",
        False,
        None,
    )


# --- sign-in and sessions ------------------------------------------------------------------


def temporary_password_only_changes(client: KitClient) -> None:
    found = signed_in(client)
    instance = found.opened
    made = call(client, found.actor("create_user"), "create_user", instance, "newcomer", "Newcomer")
    signed = call(
        client, "anonymous", "login", instance, "newcomer", made.temporary_password, None, None
    )
    _expect(signed.password_change_only, "a temporary password opens a full session")
    live = call(client, "anonymous", "session", instance, signed.session_id)
    _expect(live.password_change_only and live.user.id == made.user.id, "the session's user")


def wrong_password_and_unknown_user_alike(client: KitClient) -> None:
    found = signed_in(client)
    instance = found.opened
    wrong = code_of(
        client, "anonymous", "login", instance, "admin", "not-the-password", "192.0.2.1", None
    )
    unknown = code_of(client, "anonymous", "login", instance, "nobody", PASSWORD, "192.0.2.2", None)
    _expect(wrong == unknown == "LOGIN_FAILED", f"wrong {wrong}, unknown {unknown}")


def session_reads_the_user_now(client: KitClient) -> None:
    found = signed_in(client)
    live = call(client, found.actor("session"), "session", found.opened, found.session)
    _expect(live.user.id == found.admin and live.user.administrator, "the administrator")
    _expect(not live.password_change_only, "a chosen password opens a full session")


def idle_session_expires(client: KitClient) -> None:
    found = signed_in(client)
    current = call(client, found.admin, "sign_in_settings", found.opened)
    KIT_CLOCK.advance(current.idle + timedelta(seconds=1))
    _refused(client, "SESSION_EXPIRED", "anonymous", "session", found.opened, found.session)


def unknown_session_expired(client: KitClient) -> None:
    found = signed_in(client)
    _refused(client, "SESSION_EXPIRED", "anonymous", "session", found.opened, "no-such-session")


def logout_ends_the_session(client: KitClient) -> None:
    found = signed_in(client)
    call(client, found.actor("logout"), "logout", found.opened, found.session)
    _refused(client, "SESSION_EXPIRED", "anonymous", "session", found.opened, found.session)


def password_change_ends_every_session(client: KitClient) -> None:
    found = signed_in(client)
    instance = found.opened
    other = call(client, "anonymous", "login", instance, "admin", PASSWORD, None, None)
    renewed = call(
        client,
        found.actor("change_password"),
        "change_password",
        instance,
        found.session,
        PASSWORD,
        NEW_PASSWORD,
        None,
    )
    _refused(client, "SESSION_EXPIRED", "anonymous", "session", instance, found.session)
    _refused(client, "SESSION_EXPIRED", "anonymous", "session", instance, other.session_id)
    _expect(
        call(client, "anonymous", "session", instance, renewed).user.id == found.actor("new"), "new"
    )
    _refused(client, "LOGIN_FAILED", "anonymous", "login", instance, "admin", PASSWORD, None, None)


def short_password_rejected(client: KitClient) -> None:
    found = signed_in(client)
    _refused(
        client,
        "PASSWORD_REJECTED",
        found.actor("change_password"),
        "change_password",
        found.opened,
        found.session,
        PASSWORD,
        "short",
        None,
    )


def reset_ends_sessions(client: KitClient) -> None:
    found = signed_in(client)
    user, session = member(client, "reader")
    temporary = call(client, found.actor("reset_password"), "reset_password", found.opened, user)
    _refused(client, "SESSION_EXPIRED", "anonymous", "session", found.opened, session)
    signed = call(client, "anonymous", "login", found.opened, "reader", temporary, None, None)
    _expect(signed.password_change_only, "a reset password is temporary")


def deactivation_revokes(client: KitClient) -> None:
    found = signed_in(client)
    user, session = member(client, "leaver")
    call(client, found.actor("deactivate_user"), "deactivate_user", found.opened, user)
    _refused(client, "SESSION_EXPIRED", "anonymous", "session", found.opened, session)
    _refused(
        client, "LOGIN_FAILED", "anonymous", "login", found.opened, "leaver", PASSWORD, None, None
    )
    _expect(call(client, "system", "role_of", found.opened, user, PROJECT) is None, "a role")


def settings_read(client: KitClient) -> None:
    found = signed_in(client)
    current = call(client, found.actor("sign_in_settings"), "sign_in_settings", found.opened)
    _expect(current.absolute > current.idle, f"lifetimes {current}")


def settings_only_by_administrator(client: KitClient) -> None:
    found = signed_in(client)
    user, _ = member(client, "viewer")
    _refused(
        client,
        "ACCESS_FORBIDDEN",
        user,
        "change_sign_in_settings",
        found.opened,
        None,
        600,
        None,
        None,
    )


# --- directory -----------------------------------------------------------------------------


def users_listed(client: KitClient) -> None:
    found = signed_in(client)
    user, _ = member(client, "listed")
    page = call(client, found.actor("users"), "users", found.opened, None)
    _expect({each.id for each in page.users} == {found.admin, found.builder, user}, f"users {page}")
    _expect(
        call(client, found.actor("user"), "user", found.opened, user).user_name == "listed", "user"
    )


def non_administrator_cannot_list(client: KitClient) -> None:
    found = signed_in(client)
    user, _ = member(client, "curious")
    _refused(client, "ACCESS_FORBIDDEN", user, "users", found.opened, None)
    _refused(client, "ACCESS_FORBIDDEN", user, "create_user", found.opened, "x-user", "X")


def unknown_user_not_found(client: KitClient) -> None:
    found = signed_in(client)
    _refused(client, "USER_NOT_FOUND", found.actor("user"), "user", found.opened, "no-such-user")


def user_name_taken(client: KitClient) -> None:
    found = signed_in(client)
    _refused(
        client,
        "USER_NAME_TAKEN",
        found.actor("create_user"),
        "create_user",
        found.opened,
        "ADMIN",
        "A",
    )


def find_by_name(client: KitClient) -> None:
    found = signed_in(client)
    user, _ = member(client, "findable")
    hit = call(client, found.actor("find_user"), "find_user", found.opened, "findable")
    _expect(hit.id == user and hit.display_name == "Findable", f"found {hit}")


def members_and_roles(client: KitClient) -> None:
    found = signed_in(client)
    user, _ = member(client, "partner", "builder")
    listed = call(client, found.actor("members"), "members", found.opened, PROJECT)
    roles = {(m.user.id, m.role) for m in listed}
    _expect(roles == {(found.builder, "builder"), (user, "builder")}, f"members {listed}")
    _expect(call(client, "system", "role_of", found.opened, user, PROJECT) == "builder", "role")
    _expect(
        call(client, "system", "audience_of", found.opened, user, PROJECT) == "builder", "audience"
    )


def last_builder_kept(client: KitClient) -> None:
    found = signed_in(client)
    _refused(client, "LAST_BUILDER", found.admin, "deactivate_user", found.opened, found.builder)


def last_administrator_kept(client: KitClient) -> None:
    found = signed_in(client)
    _refused(
        client,
        "LAST_ADMINISTRATOR",
        found.actor("set_administrator"),
        "set_administrator",
        found.opened,
        found.admin,
        False,
    )


def groups_listed(client: KitClient) -> None:
    found = signed_in(client)
    user, _ = member(client, "grouped", "builder")
    call(client, found.actor("create_group"), "create_group", found.opened, PROJECT, "finance")
    call(
        client,
        found.actor("add_group_member"),
        "add_group_member",
        found.opened,
        PROJECT,
        "finance",
        user,
    )
    groups = call(client, found.actor("groups"), "groups", found.opened, PROJECT)
    _expect(groups == {"finance": (user,)}, f"groups {groups}")


def attributes_typed(client: KitClient) -> None:
    found = signed_in(client)
    user, _ = member(client, "typed", "builder")
    call(
        client,
        found.actor("set_attribute"),
        "set_attribute",
        found.opened,
        PROJECT,
        user,
        "zone",
        "text",
        "7",
    )
    call(
        client,
        found.actor("set_attribute"),
        "set_attribute",
        found.opened,
        PROJECT,
        user,
        "flag",
        "boolean",
        True,
    )
    found_attributes = call(
        client, found.actor("attributes"), "attributes", found.opened, PROJECT, user
    )
    _expect(
        found_attributes == {"zone": Attribute("text", "7"), "flag": Attribute("boolean", True)},
        f"attributes {found_attributes}",
    )


def attribute_never_coerced(client: KitClient) -> None:
    found = signed_in(client)
    user, _ = member(client, "strict", "builder")
    for kind, value in (("boolean", "true"), ("integer", "01"), ("integer", 1.0)):
        _refused(
            client,
            "VALIDATION_FAILED",
            found.actor("set_attribute"),
            "set_attribute",
            found.opened,
            PROJECT,
            user,
            "value",
            kind,
            value,
        )


def identity_links_listed(client: KitClient) -> None:
    found = signed_in(client)
    user, _ = member(client, "linked")
    link = call(
        client,
        found.actor("link_identity"),
        "link_identity",
        found.opened,
        user,
        KIT_PROVIDER,
        "sub-1",
    )
    links = call(client, found.actor("identity_links"), "identity_links", found.opened, user)
    _expect([(each.id, each.subject) for each in links] == [(link, "sub-1")], f"links {links}")


def identity_taken(client: KitClient) -> None:
    found = signed_in(client)
    first, _ = member(client, "first")
    second, _ = member(client, "second")
    call(
        client,
        found.actor("link_identity"),
        "link_identity",
        found.opened,
        first,
        KIT_PROVIDER,
        "sub-2",
    )
    _refused(
        client,
        "IDENTITY_TAKEN",
        found.actor("link_identity"),
        "link_identity",
        found.opened,
        second,
        KIT_PROVIDER,
        "sub-2",
    )


# --- limits --------------------------------------------------------------------------------


def limits_per_role(client: KitClient) -> None:
    found = signed_in(client)
    limits = call(client, found.actor("query_limits"), "query_limits", found.opened)
    _expect(set(limits) == {"builder", "author", "viewer"}, f"limits {limits}")
    _expect(all(type(limit) is RoleLimit for limit in limits.values()), "role limits")


def limits_only_by_administrator(client: KitClient) -> None:
    found = signed_in(client)
    user, _ = member(client, "limited")
    _refused(
        client,
        "ACCESS_FORBIDDEN",
        user,
        "change_query_limits",
        found.opened,
        {"viewer": RoleLimit(1, 1000)},
    )


# --- security context ----------------------------------------------------------------------


def server_context_now(client: KitClient) -> None:
    found = signed_in(client)
    user, _ = member(client, "context", "author")
    call(
        client,
        found.actor("set_attribute"),
        "set_attribute",
        found.opened,
        PROJECT,
        user,
        "zone",
        "text",
        "7",
    )
    document = json.loads(
        call(client, "system", "server_context", found.opened, user, PROJECT, POLICIES, MAPPING)
    )
    _expect(document["role"] == "author" and document["user"] == user, f"context {document}")
    _expect(document["attributes"] == {"zone": {"type": "text", "value": "7"}}, "attributes")
    _expect(document["permission_fingerprint"].startswith("sha256:"), "a fingerprint")
    _expect(call(client, "system", "read_context", json.dumps(document)) is not None, "read")


def no_role_no_context(client: KitClient) -> None:
    found = signed_in(client)
    user, _ = member(client, "outsider")
    call(client, found.actor("remove_membership"), "remove_membership", found.opened, PROJECT, user)
    _refused(
        client,
        "ACCESS_FORBIDDEN",
        "system",
        "server_context",
        found.opened,
        user,
        PROJECT,
        POLICIES,
        MAPPING,
    )
    _expect(call(client, "system", "audience_of", found.opened, user, PROJECT) == "other", "other")


def fingerprint_follows_permissions(client: KitClient) -> None:
    found = signed_in(client)
    user, _ = member(client, "moving", "builder")

    def fingerprint() -> str:
        text = call(
            client, "system", "server_context", found.opened, user, PROJECT, POLICIES, MAPPING
        )
        return str(json.loads(text)["permission_fingerprint"])

    before = fingerprint()
    call(client, found.actor("create_group"), "create_group", found.opened, PROJECT, "ops")
    call(
        client,
        found.actor("add_group_member"),
        "add_group_member",
        found.opened,
        PROJECT,
        "ops",
        user,
    )
    _expect(fingerprint() != before, "a new group leaves the fingerprint unchanged")


def local_context_needs_authentication(client: KitClient) -> None:
    document = json.loads(call(client, "local", "local_context", PROJECT, True, POLICIES, MAPPING))
    _expect(document["role"] == "builder" and document["user"] == "local", f"local {document}")
    _refused(client, "CONTEXT_INVALID", "local", "local_context", PROJECT, False, POLICIES, MAPPING)


FIXTURE = ContextFixture(
    POLICIES,
    {"security.user_zone": "41"},
    (ContextMember("u-7", PROJECT, "viewer", ("b", "a"), (("zone_id", "7"),)),),
)


def fixture_context_normalized(client: KitClient) -> None:
    document = json.loads(call(client, "system", "fixture_context", FIXTURE, "u-7", PROJECT))
    _expect(document["groups"] == ["a", "b"], f"groups {document['groups']}")
    _refused(client, "CONTEXT_INVALID", "system", "fixture_context", FIXTURE, "u-8", PROJECT)


def reuse_only_with_current_permissions(client: KitClient) -> None:
    document = json.loads(call(client, "system", "fixture_context", FIXTURE, "u-7", PROJECT))
    stored = document["permission_fingerprint"]
    _expect(call(client, "system", "fixture_may_reuse", stored, FIXTURE, "u-7", PROJECT), "reuse")
    changed = ContextFixture(POLICIES, {"security.user_zone": "42"}, FIXTURE.members)
    _expect(
        not call(client, "system", "fixture_may_reuse", stored, changed, "u-7", PROJECT), "stale"
    )
    for refused in (None, "sha256:short", stored.upper()):
        _expect(
            not call(client, "system", "fixture_may_reuse", refused, FIXTURE, "u-7", PROJECT),
            f"{refused!r} reused",
        )


def unnormalized_context_refused(client: KitClient) -> None:
    document = json.loads(call(client, "system", "fixture_context", FIXTURE, "u-7", PROJECT))
    document["groups"] = ["b", "a"]
    _refused(client, "CONTEXT_INVALID", "system", "read_context", json.dumps(document))
    _refused(client, "CONTEXT_INVALID", "system", "read_context", "not json")


def fingerprint_covers_everything(client: KitClient) -> None:
    inputs = FingerprintInputs("u", PROJECT, "viewer", (), "{}", POLICIES, MAPPING)
    first = call(client, "system", "permission_fingerprint", inputs)
    again = call(client, "system", "permission_fingerprint", inputs)
    other = FingerprintInputs("u", PROJECT, "author", (), "{}", POLICIES, MAPPING)
    _expect(first == again != call(client, "system", "permission_fingerprint", other), "role")
    bad = FingerprintInputs("u", PROJECT, "viewer", (), "{}", "md5:1", MAPPING)
    _refused(client, "CONTEXT_INVALID", "system", "permission_fingerprint", bad)


# --- OIDC ----------------------------------------------------------------------------------


def oidc_start_and_failed_callback(client: KitClient) -> None:
    found = signed_in(client)
    started = call(client, "anonymous", "oidc_start", found.opened, KIT_PROVIDER, None)
    _expect(started.location.startswith("https://idp.kit.invalid/auth?"), started.location)
    _refused(
        client,
        "OIDC_FAILED",
        "anonymous",
        "oidc_callback",
        found.opened,
        KIT_PROVIDER,
        {"code": "c", "state": "forged"},
        started.reference,
        None,
    )


def oidc_unknown_provider(client: KitClient) -> None:
    found = signed_in(client)
    _refused(client, "OIDC_NOT_CONFIGURED", "anonymous", "oidc_start", found.opened, "other", None)


def oidc_password_session_not_due(client: KitClient) -> None:
    found = signed_in(client)
    _expect(
        call(client, "anonymous", "oidc_refresh_due", found.opened, found.session) is None, "due"
    )


def idempotency_key_bound_to_its_call(client: KitClient) -> None:
    found = signed_in(client)
    envelope = client.envelope("create_user", actor=found.admin)
    client.send("create_user", envelope, found.opened, "once", "Once")
    try:
        client.send("create_user", envelope, found.opened, "other", "Other")
    except Exception as error:  # noqa: BLE001 - the case checks the code
        _expect(getattr(error, "code", None) == "VALIDATION_FAILED", f"code {error}")
        return
    raise AssertionError("one idempotency key ran two different calls")


# --- idempotency ---------------------------------------------------------------------------


def _answered(operation: str, *arguments: object) -> Callable[..., object]:
    """A call whose answers are kept, to observe that a repeat returned the first one."""

    def send(client: KitClient, envelope: CallEnvelope) -> object:
        found = signed_in(client)
        answer = client.send(operation, envelope, *_resolved(found, arguments))
        found.answers.append(answer)
        return answer

    return send


def _distinct(client: KitClient) -> int:
    return len({repr(answer) for answer in world(client).answers})


def _resolved(found: World, arguments: tuple[object, ...]) -> list[object]:
    """`arguments`, with the kit's markers replaced by the world's own values."""
    marked: dict[str, Callable[[], object]] = {
        "$instance": lambda: found.opened,
        "$admin": lambda: found.admin,
    }
    return [marked[a]() if isinstance(a, str) and a in marked else a for a in arguments]


def _as_admin(operation: str, *arguments: object) -> Callable[[KitClient, CallEnvelope], object]:
    def send(client: KitClient, envelope: CallEnvelope) -> object:
        found = signed_in(client)
        mine = dataclasses.replace(envelope, actor=found.actor(operation))
        return client.send(operation, mine, *_resolved(found, arguments))

    return send


def _read(operation: str, *arguments: object) -> Callable[[KitClient], object]:
    def observe(client: KitClient) -> object:
        found = signed_in(client)
        return repr(call(client, found.actor(operation), operation, *_resolved(found, arguments)))

    return observe


def _with(prepare: Callable[[KitClient], object], observe: Callable[[KitClient], object]) -> Any:  # noqa: ANN401
    """`observe`, after `prepare` ran once for the case's world (the world records it, so a
    later run's world is never taken for an earlier one's)."""

    def observed(client: KitClient) -> object:
        found = signed_in(client)
        if not found.prepared:
            found.prepared = True
            prepare(client)
        return observe(client)

    return observed


def _member(user_name: str, role: str = "viewer") -> Callable[[KitClient], None]:
    def prepare(client: KitClient) -> None:
        user, session = member(client, user_name, role)
        world(client).answers.append((user, session))

    return prepare


def _prepared(client: KitClient, index: int = 0) -> str:
    return str(world(client).answers[0][index])  # type: ignore[index]


def _on_member(operation: str, *arguments: object) -> Callable[[KitClient, CallEnvelope], object]:
    """`operation` by the administrator on the member `prepare` made (`$user`)."""

    def send(client: KitClient, envelope: CallEnvelope) -> object:
        found = signed_in(client)
        mine = dataclasses.replace(envelope, actor=found.actor(operation))
        resolved = [_prepared(client) if a == "$user" else a for a in _resolved(found, arguments)]
        return client.send(operation, mine, *resolved)

    return send


def _member_read(operation: str, *arguments: object) -> Callable[[KitClient], object]:
    def observe(client: KitClient) -> object:
        found = signed_in(client)
        resolved = [_prepared(client) if a == "$user" else a for a in _resolved(found, arguments)]
        return repr(call(client, found.actor(operation), operation, *resolved))

    return observe


def _session_live(client: KitClient) -> object:
    found = signed_in(client)
    return code_of(client, "anonymous", "session", found.opened, _prepared(client, 1))


def _live_answers(client: KitClient) -> int:
    found = world(client)
    sessions = {getattr(answer, "session_id", answer) for answer in found.answers}
    return sum(
        1
        for session in sessions
        if isinstance(session, str)
        and code_of(client, "a", "session", found.opened, session) is None
    )


def _login(client: KitClient, envelope: CallEnvelope) -> object:
    found = signed_in(client)
    answer = client.send("login", envelope, found.opened, "admin", PASSWORD, None, None)
    found.answers.append(answer)
    return answer


def _change_password(client: KitClient, envelope: CallEnvelope) -> object:
    found = signed_in(client)
    answer = client.send(
        "change_password", envelope, found.opened, found.session, PASSWORD, NEW_PASSWORD, None
    )
    found.answers.append(answer)
    return answer


def _open(client: KitClient, envelope: CallEnvelope) -> object:
    found = world(client)
    answer = client.send("open", envelope, found.configuration)
    found.answers.append(answer)
    found.instance = answer
    return answer


def _usable_opened(client: KitClient) -> object:
    found = world(client)
    return [code_of(client, "system", "oidc_providers", answer) for answer in set(found.answers)]


def _close(client: KitClient, envelope: CallEnvelope) -> object:
    return client.send("close", envelope, world(client).opened)


def _closed(client: KitClient) -> object:
    return code_of(client, "system", "oidc_providers", world(client).opened)


def _initialize(client: KitClient, envelope: CallEnvelope) -> object:
    return client.send("initialize", envelope, world(client).opened)


def _recovery_written(client: KitClient) -> object:
    return os.path.exists(world(client).configuration.recovery_file)


def _create_administrator(client: KitClient, envelope: CallEnvelope) -> object:
    found = world(client)
    answer = client.send(
        "create_administrator", envelope, found.opened, "admin", "Admin", False, None
    )
    found.answers.append(answer)
    return answer


def _administrators(client: KitClient) -> object:
    found = world(client)
    if not found.answers:
        return 0
    first = found.answers[0].user.id  # type: ignore[attr-defined]
    return len(call(client, first, "users", found.opened, None).users)


def _callback(client: KitClient, envelope: CallEnvelope) -> object:
    found = signed_in(client)
    if not found.answers:
        found.answers.append(
            call(client, "anonymous", "oidc_start", found.opened, KIT_PROVIDER, None)
        )
    reference = found.answers[0].reference  # type: ignore[attr-defined]
    try:
        client.send(
            "oidc_callback",
            envelope,
            found.opened,
            KIT_PROVIDER,
            {"state": "forged"},
            reference,
            None,
        )
    except Exception as error:  # noqa: BLE001 - the answer is the refusal's code
        found.answers.append(getattr(error, "code", type(error).__name__))
        return found.answers[-1]
    return None


def _callbacks(client: KitClient) -> object:
    return world(client).answers[1:2]


def _logout(client: KitClient, envelope: CallEnvelope) -> object:
    found = signed_in(client)
    return client.send("logout", envelope, found.opened, _prepared(client, 1))


def _reset(client: KitClient, envelope: CallEnvelope) -> object:
    found = signed_in(client)
    mine = dataclasses.replace(envelope, actor=found.actor("reset_password"))
    answer = client.send("reset_password", mine, found.opened, _prepared(client))
    found.answers.append(answer)
    return answer


def _linked_member(client: KitClient) -> None:
    """A member with one linked identity, its link kept after the member."""
    _member("unlinked")(client)
    link = _on_member("link_identity", "$instance", "$user", KIT_PROVIDER, "sub-gone")
    world(client).answers.append(link(client, client.envelope("link_identity")))


def _prepared_link(client: KitClient) -> str:
    return str(world(client).answers[1])


def _case(operation: str, expectation: str, check: Callable[[KitClient], None]) -> KitCase:
    return KitCase(
        f"{operation.replace('_', '-')}.{check.__name__.replace('_', '-')}",
        operation,
        expectation,
        check,
    )


def _once(operation: str, expectation: str, call_: Any, observe: Any) -> KitCase:  # noqa: ANN401
    return KitCase(
        f"{operation.replace('_', '-')}.idempotent",
        operation,
        expectation,
        Idempotent(call_, observe),
    )


CASES = (
    _case("oidc_providers", "an opened instance serves until it is closed", open_and_close),
    _case("close", "a closed or unknown instance is refused", unknown_instance_refused),
    _case(
        "initialize",
        "the first initialization writes the recovery file",
        initialize_writes_recovery,
    ),
    _case(
        "create_administrator",
        "a second administrator needs recovery",
        administrator_needs_recovery,
    ),
    _case(
        "login",
        "a temporary password opens a session that may only change it",
        temporary_password_only_changes,
    ),
    _case(
        "login",
        "a wrong password and an unknown user get the same refusal",
        wrong_password_and_unknown_user_alike,
    ),
    _case(
        "session",
        "a session reads its user as the directory knows them now",
        session_reads_the_user_now,
    ),
    _case("session", "an unknown session is SESSION_EXPIRED", unknown_session_expired),
    _case("session", "a session idle past its lifetime has ended", idle_session_expires),
    _case("logout", "signing out ends the session", logout_ends_the_session),
    _case(
        "change_password",
        "a password change ends every session of the user",
        password_change_ends_every_session,
    ),
    _case("change_password", "a short password is rejected", short_password_rejected),
    _case(
        "reset_password",
        "a reset ends the user's sessions and sets a temporary password",
        reset_ends_sessions,
    ),
    _case(
        "deactivate_user", "deactivation ends sessions, sign-ins and roles", deactivation_revokes
    ),
    _case("sign_in_settings", "the settings have an absolute and an idle lifetime", settings_read),
    _case(
        "change_sign_in_settings",
        "only an administrator changes the settings",
        settings_only_by_administrator,
    ),
    _case("users", "an administrator lists the users", users_listed),
    _case("user", "an unknown user is USER_NOT_FOUND", unknown_user_not_found),
    _case("create_user", "a non-administrator manages no users", non_administrator_cannot_list),
    _case("create_user", "user names are unique without regard to case", user_name_taken),
    _case("find_user", "a user is found by name", find_by_name),
    _case("members", "members carry their role", members_and_roles),
    _case("deactivate_user", "a project keeps its last Builder", last_builder_kept),
    _case(
        "set_administrator", "the instance keeps its last administrator", last_administrator_kept
    ),
    _case("groups", "groups list their members", groups_listed),
    _case("attributes", "attributes are typed as security-context v1", attributes_typed),
    _case("set_attribute", "an attribute value is never coerced", attribute_never_coerced),
    _case("identity_links", "a linked identity is listed", identity_links_listed),
    _case("link_identity", "a provider identity links to one user only", identity_taken),
    _case("query_limits", "every role has its limits", limits_per_role),
    _case(
        "change_query_limits",
        "only an administrator changes the limits",
        limits_only_by_administrator,
    ),
    _case("server_context", "a context is the directory now", server_context_now),
    _case("audience_of", "no role, no context: refused", no_role_no_context),
    _case(
        "role_of", "a permission change changes the fingerprint", fingerprint_follows_permissions
    ),
    _case(
        "local_context",
        "a local context needs an authenticated request",
        local_context_needs_authentication,
    ),
    _case(
        "fixture_context",
        "fixture lists are normalized; no member, no context",
        fixture_context_normalized,
    ),
    _case(
        "fixture_may_reuse",
        "reuse only with the current permissions",
        reuse_only_with_current_permissions,
    ),
    _case(
        "read_context",
        "a context not in its normalized form is refused",
        unnormalized_context_refused,
    ),
    _case(
        "permission_fingerprint",
        "the fingerprint covers the role and checks its inputs",
        fingerprint_covers_everything,
    ),
    _case(
        "oidc_start",
        "a start leads to the provider; a forged state fails",
        oidc_start_and_failed_callback,
    ),
    _case("oidc_callback", "an unknown provider is not configured", oidc_unknown_provider),
    _case(
        "oidc_refresh_due",
        "a password session is never due for a refresh",
        oidc_password_session_not_due,
    ),
    _case(
        "create_user", "one idempotency key runs one call only", idempotency_key_bound_to_its_call
    ),
    _once("open", "one key opens one instance", _open, _usable_opened),
    _once("close", "one key closes once", _close, _closed),
    _once("initialize", "one key initializes once", _initialize, _recovery_written),
    _once(
        "create_administrator",
        "one key creates one administrator",
        _create_administrator,
        _administrators,
    ),
    _once(
        "change_sign_in_settings",
        "one key changes the settings once",
        _as_admin("change_sign_in_settings", "$instance", None, 600, None, None),
        _read("sign_in_settings", "$instance"),
    ),
    _once("login", "one key opens one session", _login, _live_answers),
    _once("logout", "one key signs out once", _logout, _with(_member("leaving"), _session_live)),
    _once("change_password", "one key changes the password once", _change_password, _live_answers),
    _once(
        "reset_password",
        "one key resets once",
        _reset,
        _with(_member("forgetful"), lambda c: len({repr(a) for a in world(c).answers[1:]})),
    ),
    _once(
        "create_user",
        "one key creates one user",
        _as_admin("create_user", "$instance", "made-once", "Made"),
        _read("users", "$instance", None),
    ),
    _once(
        "deactivate_user",
        "one key deactivates once",
        _on_member("deactivate_user", "$instance", "$user"),
        _with(_member("deactivated"), _member_read("user", "$instance", "$user")),
    ),
    _once(
        "reactivate_user",
        "one key reactivates once",
        _on_member("reactivate_user", "$instance", "$user"),
        _with(
            lambda c: (
                _member("returning")(c),
                _on_member("deactivate_user", "$instance", "$user")(
                    c, c.envelope("deactivate_user")
                ),
            ),
            _member_read("user", "$instance", "$user"),
        ),
    ),
    _once(
        "set_administrator",
        "one key grants once",
        _on_member("set_administrator", "$instance", "$user", True),
        _with(_member("promoted"), _member_read("user", "$instance", "$user")),
    ),
    _once(
        "set_membership",
        "one key sets a role once",
        _on_member("set_membership", "$instance", PROJECT, "$user", "author"),
        _with(_member("roled"), _read("members", "$instance", PROJECT)),
    ),
    _once(
        "remove_membership",
        "one key removes once",
        _on_member("remove_membership", "$instance", PROJECT, "$user"),
        _with(_member("removed"), _read("members", "$instance", PROJECT)),
    ),
    _once(
        "create_group",
        "one key creates one group",
        _as_admin("create_group", "$instance", PROJECT, "made-once"),
        _with(_member("anchor", "builder"), _read("groups", "$instance", PROJECT)),
    ),
    _once(
        "delete_group",
        "one key deletes once",
        _as_admin("delete_group", "$instance", PROJECT, "doomed"),
        _with(
            lambda c: (
                _member("keeper", "builder")(c),
                _as_admin("create_group", "$instance", PROJECT, "doomed")(
                    c, c.envelope("create_group")
                ),
            ),
            _read("groups", "$instance", PROJECT),
        ),
    ),
    _once(
        "add_group_member",
        "one key adds once",
        _on_member("add_group_member", "$instance", PROJECT, "crew", "$user"),
        _with(
            lambda c: (
                _member("crewman", "builder")(c),
                _as_admin("create_group", "$instance", PROJECT, "crew")(
                    c, c.envelope("create_group")
                ),
            ),
            _read("groups", "$instance", PROJECT),
        ),
    ),
    _once(
        "remove_group_member",
        "one key removes a member once",
        _on_member("remove_group_member", "$instance", PROJECT, "crew", "$user"),
        _with(
            lambda c: (
                _member("sailor", "builder")(c),
                _as_admin("create_group", "$instance", PROJECT, "crew")(
                    c, c.envelope("create_group")
                ),
                _on_member("add_group_member", "$instance", PROJECT, "crew", "$user")(
                    c, c.envelope("add_group_member")
                ),
            ),
            _read("groups", "$instance", PROJECT),
        ),
    ),
    _once(
        "set_attribute",
        "one key sets an attribute once",
        _on_member("set_attribute", "$instance", PROJECT, "$user", "zone", "text", "7"),
        _with(
            _member("zoned", "builder"), _member_read("attributes", "$instance", PROJECT, "$user")
        ),
    ),
    _once(
        "remove_attribute",
        "one key removes an attribute once",
        _on_member("remove_attribute", "$instance", PROJECT, "$user", "zone"),
        _with(
            lambda c: (
                _member("unzoned", "builder")(c),
                _on_member("set_attribute", "$instance", PROJECT, "$user", "zone", "text", "7")(
                    c, c.envelope("set_attribute")
                ),
            ),
            _member_read("attributes", "$instance", PROJECT, "$user"),
        ),
    ),
    _once(
        "link_identity",
        "one key links once",
        _on_member("link_identity", "$instance", "$user", KIT_PROVIDER, "sub-once"),
        _with(_member("identified"), _member_read("identity_links", "$instance", "$user")),
    ),
    _once(
        "unlink_identity",
        "one key unlinks once",
        lambda c, e: _on_member("unlink_identity", "$instance", "$user", _prepared_link(c))(c, e),
        _with(
            _linked_member,
            _member_read("identity_links", "$instance", "$user"),
        ),
    ),
    _once(
        "change_query_limits",
        "one key changes the limits once",
        _as_admin("change_query_limits", "$instance", {"viewer": RoleLimit(2, 5000)}),
        _read("query_limits", "$instance"),
    ),
    _once(
        "oidc_start",
        "one key starts once",
        _answered("oidc_start", "$instance", KIT_PROVIDER, None),
        _distinct,
    ),
    _once("oidc_callback", "one key ends one callback", _callback, _callbacks),
)


ACCESS_KIT = Kit(ACCESS, recorded=69, cases=CASES)
