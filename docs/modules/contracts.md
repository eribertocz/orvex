# Module pack — `contracts`

**Responsibility:** the language-neutral definitions every module produces or consumes, and the generation of typed code from them.
**Location:** `contracts/<name>/v<N>/` (JSON Schema 2020-12, OpenAPI 3.1, Arrow schemas, MCP tool definitions).

## Contracts
The complete list, producers and consumers: `architecture/overview.md` §3. Each contract is created by the issue named in the roadmap, inside the module that owns it; this pack governs **how** a contract is written, not what it contains.

## Required reading
| Document | Sections |
|---|---|
| `architecture/overview.md` | §2, §3 |
| `process/versioning.md` | §1, §3 |
| `process/engineering.md` | §2, §3 |
| The owning module's pack | Contracts |

## ADRs
0004, 0009, 0017.

## Rules
- Layout: `contracts/<name>/v<N>/` with the schema, a `README.md` (purpose, producer, consumers, compatibility notes) and **valid and invalid examples**; the validation test runs every example.
- Generated types (Python and TypeScript) are produced by a script in `tools/`, committed, and checked as current by CI; hand-written duplicates are prohibited.
- Values that must stay exact (`DECIMAL` and integers wider than 53 bits: `BIGINT`, `UBIGINT`, `HUGEINT`) are strings in JSON; timestamps carry a time zone; `null` is explicit, never omitted to mean zero.
- Error codes are enumerated in the contract; removing a code or changing its meaning is breaking.
- Compatible change (new optional field, new enum value documented as such) ⇒ same major version. Anything else ⇒ new `v<N+1>` directory, an ADR, Owner approval and the `breaking-change` label; the producer supports the previous version during the transition period set in the ADR.
- An issue creates or extends **at most one** contract (`process/issues.md` §2).
- Contracts never mention Python, DuckDB or Svelte types (`DuckDBPyRelation` never appears in a contract, ADR-0025).
