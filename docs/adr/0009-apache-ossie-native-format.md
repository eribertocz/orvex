# 0009 — Apache Ossie as the native semantic format with `SEXTANT` extensions

**Status:** Accepted (2026-10-26)

## Context
Apache Ossie (formerly Open Semantic Interchange) is an incubating Apache project defining a vendor-neutral YAML/JSON model of datasets, fields, metrics, relationships and AI context. It describes meaning, not execution, and does not define caching, additivity, grain, units, time roles or policies. Every element accepts `custom_extensions` entries (`vendor_name` + JSON `data`).

## Decision
- The semantic model is a valid Ossie document, read and written natively — not converted from a private format.
- Semantics Ossie lacks are carried in `custom_extensions` with `vendor_name` taken from `identity.toml` (`SEXTANT`), validated by `contracts/ossie-extensions/v1` (JSON Schema, published).
- The supported Ossie specification is pinned to an exact commit of `apache/ossie` (recorded in Phase 0); upgrades go through an issue and an ADR. All Ossie knowledge lives in the Ossie adapter.
- Export to plain Ossie reports every semantic that cannot be represented.

## Alternatives considered
- Separate sidecar files for semantics Ossie lacks: keeps Ossie pure but splits one model across files.
- A private format with Ossie import/export only: violates "Ossie as a first-class citizen".

## Consequences
Models remain portable to other Ossie tools, which ignore unknown vendor extensions. The extension schema becomes a public contract.
