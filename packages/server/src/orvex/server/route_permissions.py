"""The permission every HTTP route needs (#445; `security.md` §3.1), declared once, and the rule
that an application with an undeclared route is never built (deny by default).

- `public`: no session (status, sign-in, the OIDC start and callback, which check their own).
- `session`: a live session only (the session, sign-out and password routes).
- `explorer`: free-form semantic questions and their asynchronous jobs: a Builder or an Author.
  A Viewer reaches data through pages only (P4); local mode's one user is the Builder.
- The administration routes (#446), whose callers are decided by the directory's rules
  (`DirectoryReads.allowed`), never by a project role alone:
  - `administrator`: an active instance administrator (users, OIDC links);
  - `finder`: an active administrator or any project's active Builder (a user by exact name);
  - `membership`: the project's active Builder, or an active administrator (its members; the
    domain allows an administrator only the first Builder);
  - `builder`: the project's active Builder (its groups and its members' attributes).

Every request passes, in this order: authentication, origin/CSRF, authorization (this table),
validation of the content, execution (#445, Owner precision 1).
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI
from fastapi.routing import APIRoute

type Permission = Literal[
    "public", "session", "explorer", "administrator", "finder", "membership", "builder"
]
ADMINISTRATION: frozenset[Permission] = frozenset(
    {"administrator", "finder", "membership", "builder"}
)

ROLES: dict[Permission, frozenset[str]] = {
    "public": frozenset({"builder", "author", "viewer"}),
    "session": frozenset({"builder", "author", "viewer"}),
    "explorer": frozenset({"builder", "author"}),
}
_ADMIN = "/api/v1/admin"
PERMISSIONS: dict[str, Permission] = {
    "/api/v1/health": "public",
    "/api/v1/version": "public",
    "/api/v1/auth/login": "public",
    "/api/v1/auth/oidc/{provider}/start": "public",
    "/api/v1/auth/oidc/{provider}/callback": "public",
    "/api/v1/auth/session": "session",
    "/api/v1/auth/logout": "session",
    "/api/v1/auth/password": "session",
    "/api/v1/semantic/query": "explorer",
    "/api/v1/semantic/explain": "explorer",
    "/api/v1/semantic/catalog": "explorer",
    "/api/v1/semantic/jobs/{job}": "explorer",
    _ADMIN + "/users": "administrator",
    _ADMIN + "/users/{user}": "administrator",
    _ADMIN + "/users/{user}/deactivate": "administrator",
    _ADMIN + "/users/{user}/reactivate": "administrator",
    _ADMIN + "/users/{user}/administrator": "administrator",
    _ADMIN + "/users/{user}/password-reset": "administrator",
    _ADMIN + "/users/{user}/identities": "administrator",
    _ADMIN + "/users/{user}/identities/{link}": "administrator",
    _ADMIN + "/user-lookup": "finder",
    _ADMIN + "/projects/{project}/members": "membership",
    _ADMIN + "/projects/{project}/members/{user}": "membership",
    _ADMIN + "/projects/{project}/groups": "builder",
    _ADMIN + "/projects/{project}/groups/{group}": "builder",
    _ADMIN + "/projects/{project}/groups/{group}/members/{user}": "builder",
    _ADMIN + "/projects/{project}/members/{user}/attributes": "builder",
    _ADMIN + "/projects/{project}/members/{user}/attributes/{name}": "builder",
}


def project_of(path: str) -> str | None:
    """The project an administration path names, or None."""
    parts = path.strip("/").split("/")
    if parts[:4] == ["api", "v1", "admin", "projects"] and len(parts) > 4:
        return parts[4]
    return None


def permission_of(path: str) -> Permission | None:
    """The permission of the route at `path` (a concrete request path), or None."""
    if path in PERMISSIONS:
        return PERMISSIONS[path]
    for template, permission in PERMISSIONS.items():
        if "{" in template and _matches(template, path):
            return permission
    return None


def allows(permission: Permission, role: str | None) -> bool:
    """Whether a caller of `role` (None: no project role) may use a route of `permission`; an
    administration permission is never granted by a role alone."""
    if permission in ADMINISTRATION:
        return False
    if permission in {"public", "session"}:
        return True
    return role is not None and role in ROLES[permission]


def checked_permissions(app: FastAPI) -> None:
    """Refuse an application with an API route the table does not declare."""
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path not in PERMISSIONS:
            raise ValueError(f"route {route.path} declares no permission (#445)")


def _matches(template: str, path: str) -> bool:
    wanted, given = template.strip("/").split("/"), path.strip("/").split("/")
    if len(wanted) != len(given):
        return False
    return all(
        (w.startswith("{") and w.endswith("}") and bool(g)) or w == g
        for w, g in zip(wanted, given, strict=True)
    )
