# Changelog

Product releases of Orvex. Module versions are listed in `versions.toml`; the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow
[Semantic Versioning](https://semver.org/) (`docs/process/versioning.md`).

## v0.1.0-alpha.1 — 2026-10-27

Phase P0 (Foundation). Alpha: formats and contracts may still change between phases.

Modules, all `0.1.0`: access, contracts, dashboards, mcp, semantic, server, store, transform.
Contracts: `http-api` v1.

### Added
- Repository governance: governed paths with the `governance` check, status labels, Dependabot (#15).
- Python workspace: one distribution per module, exact pins, lock files for Python 3.15 and 3.14, ruff, mypy strict, pytest (#20).
- Web workspace: SvelteKit, Svelte 5, TypeScript strict, Tailwind CSS 4, license allow-list (#22).
- Identity loader, one-step rename script, product-name and module-boundary checks (#24).
- Contracts workspace: JSON Schema and OpenAPI validation, generated Python and TypeScript types (#27).
- `http-api` contract v1 (health, version, error envelope) and the typed web client (#29).
- Server: `orvex` command, local mode on 127.0.0.1, project folders, JSON logs with request id (#31).
- Synthetic electricity dataset: consumers, meters, readings, invoices, payments and credit notes, byte-reproducible from a seed (#32, #34).
- Semantic corpus: bi-platform's 21 normative cases imported unchanged, Apache Ossie schema pinned, corpus runner reporting `not-run` (#35).
- Version manifest, contract compatibility check at startup, release workflow, Docker skeleton (#37).

### Changed
- The product is named Orvex (previously Sextant); `SEXTANT` stays accepted as a previous Ossie vendor name (#39).

### Fixed
- The server wheel declares its dependency on the contracts wheel; release tags must match `versions.toml` (#40).
