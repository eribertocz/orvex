# `http-api` v1

OpenAPI 3.1 description of the public HTTP API.

- **Producer:** `server`.
- **Consumers:** `web` (through generated types and `web/src/lib/api/client.ts`), external clients.
- **Error model:** every non-2xx response is an `ErrorEnvelope` with a stable `code` (`UPPER_SNAKE_CASE`), a human message, structured `details` (name/value pairs) and the `request_id` that also appears in the `X-Request-Id` header and in the server log.
- **Compatibility:** later issues add paths and schemas compatibly. Removing a path or a field, or changing the meaning of an error code, needs `v2` and an ADR.

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
