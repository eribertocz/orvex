"""Server mode's administration routes (#446): the instance administrator's users and OIDC
links, and a project Builder's members, groups and member attributes, under `/api/v1/admin/`.

Every route calls one `access` operation and nothing here decides who may do what: the domain
rules do, and audit every change and refusal (one enforcement point). The request rules have
already passed (authentication, origin/CSRF, the route's declared permission, a project the
instance serves). A temporary password is in one response only, marked `no-store`, and never
logged. A write that changes nothing answers like one that did, and records nothing.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from orvex.access.audit.log import AuditLog
from orvex.access.authentication.service import SignIn
from orvex.access.directory.attributes import Attributes
from orvex.access.directory.reads import DirectoryReads
from orvex.access.directory.records import User
from orvex.access.directory.service import Directory
from orvex.access.errors import AccessError
from orvex.access.oidc.identities import Identities
from orvex.contracts.http_api import v1
from orvex.server.sign_in_errors import access_error_response, validation_response
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

PREFIX = "/api/v1/admin"


class _NotEmpty(Exception):
    """An action's body that is not `{}`."""


@dataclass(frozen=True)
class Administration:
    """What the administration routes call, and the projects the instance serves: an explicit,
    non-empty set (#446 precision 3); any other project is `PROJECT_NOT_FOUND`."""

    directory: Directory
    reads: DirectoryReads
    sign_in: SignIn
    attributes: Attributes
    identities: Identities | None
    projects: frozenset[str]
    audit: AuditLog  # mandatory (#445): no audit log, no server

    def __post_init__(self) -> None:
        if not self.projects:
            raise ValueError("server mode serves at least one project (#446): no server")
        if not isinstance(self.audit, AuditLog):
            raise ValueError("server mode needs its audit log: the server does not start")


def _user(found: User) -> dict[str, Any]:
    return v1.AdminUser(
        id=found.id,
        user_name=found.user_name,
        display_name=found.display_name,
        state=found.state,
        administrator=found.administrator,
        created_at=found.created_at,
        changed_at=found.changed_at,
    ).model_dump(mode="json")


def _secret(document: dict[str, Any], status: int) -> Response:
    response = JSONResponse(document, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    return response


def mount_administration(app: FastAPI, admin: Administration) -> None:
    """The routes of `http-api` v1 under `PREFIX`."""

    async def call[T](request: Request, operation: Callable[..., T], *args: object) -> T:
        return await run_in_threadpool(operation, request.state.session.user.id, *args)

    async def body[M: BaseModel](request: Request, model: type[M]) -> M:
        return model.model_validate_json(await request.body())

    def done() -> Response:
        return Response(status_code=204)

    def routed(handler: Callable[..., Any]) -> Callable[..., Any]:
        """A route's refusals as `http-api` errors: the domain's codes, the contract's."""

        @functools.wraps(handler)  # FastAPI reads the path parameters from its signature
        async def run(request: Request, **path: str) -> Response:
            request_id: str = request.state.request_id
            try:
                answered: Response = await handler(request, **path)
            except ValidationError as error:
                return validation_response(error, request_id)
            except _NotEmpty:
                return validation_response(None, request_id)
            except AccessError as error:
                return access_error_response(error, request_id)
            return answered

        return run

    async def nothing(request: Request) -> None:
        """An action's body: `{}` only."""
        if (await request.body()).strip() != b"{}":
            raise _NotEmpty

    # --- users (administrator) ---------------------------------------------------------------

    @app.get(PREFIX + "/users")
    @routed
    async def list_users(request: Request) -> Response:
        page = await call(request, admin.reads.users, request.query_params.get("after"))
        users = [_user(found) for found in page.users]
        return JSONResponse({"users": users, "after": page.after})

    @app.post(PREFIX + "/users")
    @routed
    async def create_user(request: Request) -> Response:
        asked = await body(request, v1.NewUserRequest)
        made = await call(request, admin.directory.create_user, asked.user_name, asked.display_name)
        document = {"user": _user(made.user), "temporary_password": made.temporary_password}
        return _secret(document, 201)

    @app.get(PREFIX + "/users/{user}")
    @routed
    async def get_user(request: Request, user: str) -> Response:
        return JSONResponse(_user(await call(request, admin.reads.user, user)))

    @app.post(PREFIX + "/users/{user}/deactivate")
    @routed
    async def deactivate_user(request: Request, user: str) -> Response:
        await nothing(request)
        await call(request, admin.directory.deactivate_user, user)
        return done()

    @app.post(PREFIX + "/users/{user}/reactivate")
    @routed
    async def reactivate_user(request: Request, user: str) -> Response:
        await nothing(request)
        await call(request, admin.directory.reactivate_user, user)
        return done()

    @app.put(PREFIX + "/users/{user}/administrator")
    @routed
    async def set_administrator(request: Request, user: str) -> Response:
        asked = await body(request, v1.AdministratorFlag)
        await call(request, admin.directory.set_administrator, user, asked.value)
        return done()

    @app.post(PREFIX + "/users/{user}/password-reset")
    @routed
    async def reset_password(request: Request, user: str) -> Response:
        await nothing(request)
        temporary = await call(request, admin.sign_in.reset_password, user)
        return _secret({"temporary_password": temporary}, 200)

    # --- OIDC links (administrator) ----------------------------------------------------------

    def identities() -> Identities:
        if admin.identities is None:
            raise AccessError("OIDC_NOT_CONFIGURED", "No sign-in provider is configured.")
        return admin.identities

    @app.get(PREFIX + "/users/{user}/identities")
    @routed
    async def list_identities(request: Request, user: str) -> Response:
        links = await call(request, identities().links, user)
        listed = [
            {"id": link.id, "provider": link.provider, "subject": link.subject,
             "linked_at": link.linked_at}
            for link in links
        ]  # fmt: skip
        return _secret({"identities": listed}, 200)

    @app.post(PREFIX + "/users/{user}/identities")
    @routed
    async def link_identity(request: Request, user: str) -> Response:
        asked = await body(request, v1.IdentityLinkRequest)
        link = await call(request, identities().link, user, asked.provider, asked.subject)
        return JSONResponse({"id": link}, status_code=201)

    @app.delete(PREFIX + "/users/{user}/identities/{link}")
    @routed
    async def unlink_identity(request: Request, user: str, link: str) -> Response:
        await call(request, identities().unlink, user, link)
        return done()

    # --- finding a user by name (administrator, or any Builder) ------------------------------

    @app.get(PREFIX + "/user-lookup")
    @routed
    async def find_user(request: Request) -> Response:
        found = await call(request, admin.reads.find, request.query_params.get("user_name", ""))
        return _secret({"id": found.id, "display_name": found.display_name}, 200)

    # --- a project's members, groups and attributes ------------------------------------------

    @app.get(PREFIX + "/projects/{project}/members")
    @routed
    async def list_members(request: Request, project: str) -> Response:
        members = await call(request, admin.reads.members, project)
        listed = [
            {"id": m.user.id, "user_name": m.user.user_name, "display_name": m.user.display_name,
             "state": m.user.state, "role": m.role}
            for m in members
        ]  # fmt: skip
        return JSONResponse({"members": listed})

    @app.put(PREFIX + "/projects/{project}/members/{user}")
    @routed
    async def set_membership(request: Request, project: str, user: str) -> Response:
        asked = await body(request, v1.RoleAssignment)
        await call(request, admin.directory.set_membership, project, user, asked.role)
        return done()

    @app.delete(PREFIX + "/projects/{project}/members/{user}")
    @routed
    async def remove_membership(request: Request, project: str, user: str) -> Response:
        await call(request, admin.directory.remove_membership, project, user)
        return done()

    @app.get(PREFIX + "/projects/{project}/groups")
    @routed
    async def list_groups(request: Request, project: str) -> Response:
        groups = await call(request, admin.reads.groups, project)
        listed = [{"name": name, "members": members} for name, members in groups.items()]
        return JSONResponse({"groups": listed})

    @app.post(PREFIX + "/projects/{project}/groups")
    @routed
    async def create_group(request: Request, project: str) -> Response:
        asked = await body(request, v1.GroupRequest)
        await call(request, admin.directory.create_group, project, asked.name)
        return JSONResponse({"name": asked.name}, status_code=201)

    @app.delete(PREFIX + "/projects/{project}/groups/{group}")
    @routed
    async def delete_group(request: Request, project: str, group: str) -> Response:
        await call(request, admin.directory.delete_group, project, group)
        return done()

    @app.put(PREFIX + "/projects/{project}/groups/{group}/members/{user}")
    @routed
    async def add_group_member(request: Request, project: str, group: str, user: str) -> Response:
        await nothing(request)
        await call(request, admin.directory.add_group_member, project, group, user)
        return done()

    @app.delete(PREFIX + "/projects/{project}/groups/{group}/members/{user}")
    @routed
    async def remove_group_member(
        request: Request, project: str, group: str, user: str
    ) -> Response:
        await call(request, admin.directory.remove_group_member, project, group, user)
        return done()

    @app.get(PREFIX + "/projects/{project}/members/{user}/attributes")
    @routed
    async def list_attributes(request: Request, project: str, user: str) -> Response:
        found = await call(request, admin.reads.attributes, project, user)
        listed = [{"name": name, **attribute} for name, attribute in sorted(found.items())]
        return JSONResponse({"attributes": listed})

    @app.put(PREFIX + "/projects/{project}/members/{user}/attributes/{name}")
    @routed
    async def set_attribute(request: Request, project: str, user: str, name: str) -> Response:
        asked = await body(request, v1.AttributeValue)
        value = asked.values if asked.type == "list" else asked.value
        await call(request, admin.attributes.set, project, user, name, asked.type, value)
        return done()

    @app.delete(PREFIX + "/projects/{project}/members/{user}/attributes/{name}")
    @routed
    async def remove_attribute(request: Request, project: str, user: str, name: str) -> Response:
        await call(request, admin.attributes.remove, project, user, name)
        return done()
