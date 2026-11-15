"""A `sequence` case whose steps exist only over HTTP (#447 D1): `next-page`, `async-start` and
`async-result`, asked through the HTTP API in server mode, in-process, with the real delivery
step (#445) and the real page cursor re-check (#440).

- **The server.** `create_server_app` over the fixture on the managed lake, with an access store
  and an audit log of its own in a temporary folder.
- **The caller.** Provisioned from the case's `security_context` document: a user with the case's
  user id, its role in the fixture's project, its groups and its attributes. The server then
  computes the context, and its own permission fingerprint, as for any session. The case's
  fingerprint stays the case's identifier (D1).
- **Changes.** A step's `change` is applied before it: `delete_row` on the managed lake (a
  mapping), `remove_group` in the directory.
- **Deterministic** (Owner precision 4). Asynchronous jobs run at once (`run_jobs`), so an
  `async-start` answers once its job has executed. No step sleeps, polls or waits on a clock.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from orvex.contracts.access.client import AccessClient, system
from orvex.contracts.access.values import Configuration, SqliteLocation
from orvex.server import semantic_http, semantic_model
from orvex.server.assembly import access_bindings
from orvex.server.identity import load_identity
from orvex.server.server_app import ServedProject, create_server_app
from orvex.server.versions import Versions
from orvex.store.lake import Lake

from conformance.runners import security_fixtures as sf
from conformance.runners.security_engines import Received, column_name, managed_delete

HTTP_KINDS = frozenset({"next-page", "async-start", "async-result"})
ORIGIN = "https://security-cases.example"
SAME = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
PASSWORD = "a passphrase for the security cases"
VERSIONS = Versions("0.0.0", (("server", "0.0.0"),), (("http-api", "v1"),))
QUERY = "/api/v1/semantic/query"
JOBS = "/api/v1/semantic/jobs/"


def over_http(request: Mapping[str, Any]) -> bool:
    """Whether a case's request has a step that exists only over HTTP."""
    steps = request.get("steps", []) if request.get("kind") == "sequence" else []
    return any(step["request"]["kind"] in HTTP_KINDS for step in steps)


@dataclass
class _Source:
    """The fixture as server mode's lake source: its managed lake, its compiled model."""

    lake: Lake
    model_of: semantic_model.SemanticModel

    @contextmanager
    def session(self) -> Iterator[Lake]:
        yield self.lake

    def model(self, lake: Lake, snapshot: int) -> semantic_model.Loaded:
        return semantic_model.Loaded(
            semantic_model.SemanticModel(
                self.model_of.compiled, self.model_of.policies, snapshot, "model.ossie.json"
            )
        )


class Instance:
    """One server-mode application over the fixture, with the case's callers in its directory."""

    def __init__(self, found: sf.Fixture, lake: Lake, folder: Path, groups: set[str]) -> None:
        self.found, self.lake = found, lake
        self._ids: list[str] = []
        identity = load_identity()
        bindings = access_bindings(identity, new_id=self._next_id)
        configuration = Configuration(
            store=SqliteLocation(str(folder / "access.sqlite")),
            audit_key=str(folder / "audit.key"),
            audit_anchors=str(folder / "anchors.log"),
            recovery_file=str(folder / "recovery.key"),
        )
        self.access = AccessClient(*bindings, system("security-cases")).open(configuration)
        self._ids.append("runner-admin")
        first = self.access.create_administrator("root", "Root")
        self.admin = self.access.as_actor(first.user.id)
        self.admin.change_sign_in_settings(public_origin=ORIGIN)
        self.builder_id = self.person("runner-builder")
        self.project = found.name
        self.builder = self.admin.as_actor(self.builder_id, self.project)
        policies = found.policies(groups)
        model = semantic_model.SemanticModel(found.compiled, policies, 0, "model.ossie.json")
        semantic = semantic_http.Semantic(
            _Source(lake, model),
            semantic_http.server_context(self.access),
            semantic_http.session_settings(),
            self.access,
            semantic_http.server_limits(self.access),
            identity=semantic_http.server_identity(self.access),
        )
        self.admin.set_membership(self.project, self.builder_id, "builder")
        for group in sorted(groups):
            self.builder.create_group(self.project, group)
        self.app = create_server_app(
            VERSIONS,
            self.access,
            identity.slug,
            served=ServedProject(self.project, semantic),
            run_jobs=lambda work: work(),  # at once: deterministic (precision 4)
        )
        self.clients: dict[str, TestClient] = {}

    def _next_id(self) -> str:
        return self._ids.pop()

    def person(self, user_id: str) -> str:
        self._ids.append(user_id)
        made = self.admin.create_user(user_id, user_id)
        signed = self.access.login(user_id, made.temporary_password, "192.0.2.1")
        self.access.as_actor(made.user.id).change_password(
            signed.session_id, made.temporary_password, PASSWORD, "192.0.2.1"
        )
        return made.user.id

    def close(self) -> None:
        self.access.close()

    def caller(self, context: Mapping[str, Any]) -> TestClient:
        """The case's caller, provisioned once, signed in."""
        user = str(context["user"])
        if user in self.clients:
            return self.clients[user]
        self.person(user)
        self.builder.set_membership(self.project, user, str(context["role"]))
        for group in context["groups"]:
            self.builder.add_group_member(self.project, group, user)
        for name, attribute in dict(context["attributes"]).items():
            kind = str(attribute["type"])
            value = attribute["values"] if kind == "list" else attribute["value"]
            self.builder.set_attribute(self.project, user, name, kind, value)
        http = TestClient(self.app, base_url=ORIGIN)
        signed = http.post(
            "/api/v1/auth/login", json={"user_name": user, "password": PASSWORD}, headers=SAME
        )
        if signed.status_code != 200:
            raise RuntimeError(f"the case's caller could not sign in: {signed.text[:200]}")
        self.clients[user] = http
        return http

    def change(self, change: Mapping[str, Any]) -> None:
        if change["op"] == "delete_row":
            managed_delete(self.lake, str(change["table"]), change["row"])
            return
        if change["op"] == "remove_group":
            self.builder.remove_group_member(
                self.project, str(change["group"]), str(change["user"])
            )
            return
        raise RuntimeError(f"no change {change['op']!r} over HTTP")


def received(answer: Any) -> Received:  # noqa: ANN401 - an HTTP response
    """What the caller receives in one HTTP answer: its result, or its error's code."""
    document = answer.json()
    if "error" in document:
        return Received(None, [], [], str(document["error"]["code"]), [answer.text])
    found = document.get("answer", document)
    result = found["result"]
    rows = [[None if v is None else str(v) for v in row] for row in result["rows"]]
    columns = [column_name(c) for c in result["columns"]]
    notices = [str(n["code"]) for n in result.get("notices", [])]
    return Received(rows, columns, notices, None, [answer.text])


def run(
    found: sf.Fixture,
    request: Mapping[str, Any],
    changes: bool = True,
    caller_of_last: Mapping[str, Any] | None = None,
) -> list[Received]:
    """Every step's answer over HTTP, in order; `changes` as in `security_sequences.run`;
    `caller_of_last` asks the last step as another caller (a case's control)."""
    steps = list(request["steps"])
    if caller_of_last is not None:
        steps[-1] = {**steps[-1], "caller": caller_of_last}
    groups = {g for step in steps for g in step["caller"]["groups"]}
    with sf.managed_lake(found) as lake, tempfile.TemporaryDirectory() as folder:
        instance = Instance(found, lake, Path(folder), groups)
        try:
            return _steps(instance, steps, changes)
        finally:
            instance.close()


def _steps(instance: Instance, steps: list[Any], changes: bool) -> list[Received]:
    """Every step's answer, asked of `instance` in order; `changes` as in `run`."""
    answers: list[Received] = []
    last_body: dict[str, Any] | None = None
    last_next: str | None = None
    job: str | None = None
    for step in steps:
        if changes and step.get("change") is not None:
            instance.change(step["change"])
        http = instance.caller(step["caller"])
        kind = step["request"]["kind"]
        if kind == "semantic-query":
            last_body = dict(step["request"]["body"])
            answer = http.post(QUERY, json=last_body, headers=SAME)
            got = received(answer)
            last_next = answer.json().get("result", {}).get("page", {}).get("next")
        elif kind == "next-page":
            if last_body is None or last_next is None:
                raise RuntimeError("a next page with no page before it")
            body = {**last_body, "page": {**last_body["page"], "after": last_next}}
            got = received(http.post(QUERY, json=body, headers=SAME))
        elif kind == "async-start":
            started = http.post(
                QUERY,
                json=step["request"]["body"],
                headers={**SAME, "Prefer": "respond-async"},
            )
            if started.status_code != 202:
                got = received(started)
            else:
                job = str(started.json()["job"])
                got = Received(None, [], [], None, [started.text])
        elif kind == "async-result":
            if job is None:
                raise RuntimeError("an asynchronous result with no job started")
            got = received(http.get(JOBS + job))
        else:
            raise RuntimeError(f"no step {kind!r} over HTTP")
        answers.append(got)
    return answers
