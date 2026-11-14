# `http-api` v1

OpenAPI 3.1 description of the public HTTP API.

- **Producer:** `server`.
- **Consumers:** `web` (through generated types and `web/src/lib/api/client.ts`), external clients.
- **Error model:** every non-2xx response is an `ErrorEnvelope` with a stable `code` (`UPPER_SNAKE_CASE`), a human message, structured `details` (name/value pairs) and the `request_id` that also appears in the `X-Request-Id` header and in the server log.
- **Compatibility:** later issues add paths and schemas compatibly. Removing a path or a field, or changing the meaning of an error code, needs `v2` and an ADR.

## Identity

OpenAPI 3.1 does not allow a root `$id`, so the P0-05 `$id` rule applies to JSON Schema contracts only. This contract is identified by its path (`contracts/http-api/v1`) and `info.version`; its schemas are addressed as `#/components/schemas/<Name>`.

## Error codes

The registry is `components.x-error-codes` in `openapi.yaml`; a test checks that every code used in the examples, and later every code the server sends, is listed there. Codes that only the web client produces, and the server never sends:

| Code | Meaning |
|---|---|
| `HTTP_ERROR` | A non-2xx response whose body is not an `ErrorEnvelope` (for example from a proxy) |
| `INVALID_RESPONSE` | A 2xx response whose body is not the JSON the contract promises |
| `NETWORK_ERROR` | The request never reached the server or got no response (offline, DNS, connection reset) |

## Deliberate limits of v1

- `ErrorDetail.value` is a string. Richer values would be a breaking change for typed clients.
- `Health.status` is the constant `ok`. Reporting degraded states later is a breaking change, so it would come as a new field or in v2.
- `modules`, `contracts` and `details` are arrays of name/value objects, not maps, because every object in a contract is closed (`additionalProperties: false`).

## Semantic routes (P2-27a; server mode, #445)

`POST /api/v1/semantic/query`, `POST /api/v1/semantic/explain` (P2-27b, #364) and `GET /api/v1/semantic/catalog` exist in local mode and, with the job routes, in server mode. `explain` answers a `semantic-explain` v1 document and reads no data; its errors follow the same table, except that the plan's own refusals (categories `correctness`, `budget`, `engine`) are explained with `outcome: refused` instead.

- **Credentials.** Each serve session has three, all revoked when the server stops:
  - a one-time bootstrap code, valid for a few minutes, exchanged at `/?code=…` for a session cookie and a redirect without the code;
  - the session cookie (`HttpOnly`, `SameSite=Strict`, its name carries the port);
  - a bearer token for scripts, in a file only its owner can read.
- **Requests.** A semantic request is refused before any project work when:
  - its `Host` is not `127.0.0.1:<port>` or `localhost:<port>`;
  - its `Origin` is not the server's own (`null` included), or its `Sec-Fetch-Site` is not `same-origin` or `none`;
  - a `POST` body is not `application/json`;
  - it carries no live credential.
- **Server mode (#445).** Every request passes, in this order: authentication (`401 SESSION_EXPIRED`), origin and CSRF (`403 ORIGIN_REFUSED`), authorization (`403 ACCESS_FORBIDDEN`, audited), then the body type (`403 ORIGIN_REFUSED`, `content-type`). A request refused at one step is told nothing of the later ones. Each route declares its permission, and an application with an undeclared route does not build. The semantic routes need the explorer permission: a Builder or an Author. A Viewer reaches data only through pages (P4).
- **Limits (#445).** Each role has a number of questions running at once per user and project, and a run time, set by the instance administrator (defaults: Builder 4 and 120 s, Author 2 and 60 s, Viewer 2 and 30 s). A question beyond it is refused before any work, `429 QUERY_BUDGET_EXCEEDED` with `budget: concurrent_queries` and `Retry-After`, never queued. No limit is on rows: an answer is whole or an error.
- **Asynchronous questions (#445).** `POST /api/v1/semantic/query` with `Prefer: respond-async` answers `202` with the job's id and `Location: /api/v1/semantic/jobs/{job}`. `GET` on it gives `running`, `cancelled`, or `done` with the answer (JSON, or Arrow with `Accept`); `DELETE` cancels it. A job is its owner's alone, kept 10 minutes after it ends, and lost at a restart (`404 SEMANTIC_JOB_NOT_FOUND`).
- **Delivery (#445).** Every answer, synchronous or a job's, read, from the result cache or not, is checked against the caller's permissions at the moment it is sent: a change since it was computed is `409 QUERY_RESULT_WITHDRAWN`, and nothing of it is sent. Each delivered answer is recorded in the audit log first (`query`); when that cannot be recorded, `503 AUDIT_UNAVAILABLE` and nothing is sent, except an answer a row policy withheld whole (#443).
- **Engine errors.** Every `semantic-error` v1 code is also an error code of this API. `error.code` is the engine's code; `error.semantic` is its whole message, with the builder audience in local mode; `details` repeats its details as name/value text.
- **Status codes:**

  | Status | When |
  |---|---|
  | 400 | category `query` |
  | 403 | category `security` |
  | 404 | `MEMBER_NOT_FOUND` |
  | 409 | `EVIDENCE_VIOLATED`, `QUERY_RESULT_WITHDRAWN` |
  | 413 | `MATRIX_TOO_WIDE`, and a size budget |
  | 422 | categories `model`, `correctness` and `engine` |
  | 429 | the time budget (`budget: time_ms`), and the concurrency limit (`budget: concurrent_queries`, with `Retry-After`) |
  | 503 | `ENGINE_VERSION_UNTESTED`, and `AUDIT_UNAVAILABLE` |
- **Arrow.** The Arrow answer is the result's Arrow form. Its schema metadata also holds `semantic.envelope` and `semantic.receipt`, the RFC 8785 bytes of the JSON answer's envelope and receipt view.
