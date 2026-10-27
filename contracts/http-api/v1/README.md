# `http-api` v1

OpenAPI 3.1 description of the public HTTP API.

- **Producer:** `server`.
- **Consumers:** `web` (through generated types and `web/src/lib/api/client.ts`), external clients.
- **Error model:** every non-2xx response is an `ErrorEnvelope` with a stable `code` (`UPPER_SNAKE_CASE`), a human message, structured `details` (name/value pairs) and the `request_id` that also appears in the `X-Request-Id` header and in the server log.
- **Compatibility:** later issues add paths and schemas compatibly. Removing a path or a field, or changing the meaning of an error code, needs `v2` and an ADR.
