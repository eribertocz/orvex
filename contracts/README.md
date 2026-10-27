# Contracts

Language-neutral definitions that modules produce and consume (`docs/modules/contracts.md`, ADR-0004).

```
contracts/<name>/v<N>/
├── schema.json          # JSON Schema 2020-12 (or openapi.yaml, OpenAPI 3.1)
├── README.md            # purpose, producer, consumers, compatibility notes
└── examples/
    ├── valid/*.json     # must validate
    └── invalid/*.json   # must be rejected; <name>.expected.txt holds text the error must contain
```

For an OpenAPI contract, an example named `<Schema>.<case>.json` is validated against `#/components/schemas/<Schema>`.

Contracts whose name starts with `_` are fixtures that exercise the tooling; they are not part of the product.

```bash
python -m tools.contracts.validate   # CONTRACT_SCHEMA_INVALID, CONTRACT_EXAMPLE_REJECTED, CONTRACT_EXAMPLE_ACCEPTED, CONTRACT_OPEN_OBJECT, CONTRACT_LAYOUT_INVALID
python -m tools.contracts.generate   # Python (packages/contracts) and TypeScript (web/src/lib/contracts); CONTRACT_TYPES_STALE, CONTRACT_TOOLING_MISSING
```

Generated code is committed and never edited by hand; CI fails when it is stale (`CONTRACT_TYPES_STALE`, including orphaned files).

`CONTRACT_EXAMPLE_ACCEPTED` covers both an invalid example that validates and one rejected for a reason other than its `.expected.txt`; the finding's detail says which.

## Closed objects

Every object schema sets `additionalProperties: false`; `unevaluatedProperties: false` alone is reported as open, to keep one rule. Keys matched by `patternProperties` count as declared. The check follows every local `$ref` to its target, wherever the target lives in the document.
