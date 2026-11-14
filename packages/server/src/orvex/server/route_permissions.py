"""The permission every HTTP route needs (#445; `security.md` §3.1), declared once, and the rule
that an application with an undeclared route is never built (deny by default).

- `public`: no session (status, sign-in, the OIDC start and callback, which check their own).
- `session`: a live session only (the session, sign-out and password routes).
- `explorer`: free-form semantic questions and their asynchronous jobs: a Builder or an Author.
  A Viewer reaches data through pages only (P4); local mode's one user is the Builder.

Every request passes, in this order: authentication, origin/CSRF, authorization (this table),
validation of the content, execution (#445, Owner precision 1).
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI
from fastapi.routing import APIRoute

type Permission = Literal["public", "session", "explorer"]

ROLES: dict[Permission, frozenset[str]] = {
    "public": frozenset({"builder", "author", "viewer"}),
    "session": frozenset({"builder", "author", "viewer"}),
    "explorer": frozenset({"builder", "author"}),
}
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
}


def permission_of(path: str) -> Permission | None:
    """The permission of the route at `path` (a concrete request path), or None."""
    if path in PERMISSIONS:
        return PERMISSIONS[path]
    for template, permission in PERMISSIONS.items():
        if "{" in template and _matches(template, path):
            return permission
    return None


def allows(permission: Permission, role: str | None) -> bool:
    """Whether a caller of `role` (None: no project role) may use a route of `permission`."""
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
