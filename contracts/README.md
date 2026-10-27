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
python -m tools.contracts.validate   # CONTRACT_SCHEMA_INVALID, CONTRACT_EXAMPLE_REJECTED, CONTRACT_EXAMPLE_ACCEPTED, CONTRACT_OPEN_OBJECT
python -m tools.contracts.generate   # Python (packages/contracts) and TypeScript (web/src/lib/contracts)
```

Generated code is committed and never edited by hand; CI fails when it is stale.
