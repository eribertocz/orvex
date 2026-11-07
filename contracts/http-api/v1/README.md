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

## Semantic routes (P2-27a)

`POST /api/v1/semantic/query` and `GET /api/v1/semantic/catalog` exist only in local mode.

- **Credentials.** Each serve session has three, all revoked when the server stops:
  - a one-time bootstrap code, valid for a few minutes, exchanged at `/?code=…` for a session cookie and a redirect without the code;
  - the session cookie (`HttpOnly`, `SameSite=Strict`, its name carries the port);
  - a bearer token for scripts, in a file only its owner can read.
- **Requests.** A semantic request is refused before any project work when:
  - its `Host` is not `127.0.0.1:<port>` or `localhost:<port>`;
  - its `Origin` is not the server's own (`null` included), or its `Sec-Fetch-Site` is not `same-origin` or `none`;
  - a `POST` body is not `application/json`;
  - it carries no live credential.
- **Engine errors.** Every `semantic-error` v1 code is also an error code of this API. `error.code` is the engine's code; `error.semantic` is its whole message, with the builder audience in local mode; `details` repeats its details as name/value text.
- **Status codes:**

  | Status | When |
  |---|---|
  | 400 | category `query` |
  | 403 | category `security` |
  | 404 | `MEMBER_NOT_FOUND` |
  | 409 | `EVIDENCE_VIOLATED` |
  | 413 | `MATRIX_TOO_WIDE`, and a size budget |
  | 422 | categories `model`, `correctness` and `engine` |
  | 429 | the time budget (`budget: time_ms`) |
  | 503 | `ENGINE_VERSION_UNTESTED` |
- **Arrow.** The Arrow answer is the result's Arrow form. Its schema metadata also holds `semantic.envelope` and `semantic.receipt`, the RFC 8785 bytes of the JSON answer's envelope and receipt view.
