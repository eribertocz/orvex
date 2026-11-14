# 0032 — Security cases follow ADR-0027 §3 for views, macros and overlapping locations

**Status:** Proposed (2026-11-14). The Owner chose this resolution on #447; the record is accepted when the Owner merges it. It changes no decision of ADR-0027: it ratifies §3.

## Context
- **The security runner (#447) ran every normative scenario for the first time** on an existing DuckLake, through aggregate counterparts where detail pages wait for P5-14.
- **Three cases contradicted ADR-0027 §3** (rank 4 in `docs/README.md`; the cases are tests written against it):
  - `SEC-DQ-006` and `SEC-DQ-007` required an existing lake holding a view or a macro not to be served at all. §3 decides otherwise: "tables only" (a dataset on a view is refused with `STORE_SOURCE_INVALID`) and "no lake macros" (generated SQL qualifies every function, so no call resolves to one of the lake's). The lake's tables are served; nothing a view or a macro names ever is.
  - `SEC-DQ-018` required `STORE_LOCATION_INVALID` for a declared location containing the managed lake's. §3, "separate locations", rejects both directions of an overlap with `STORE_SOURCE_INVALID`.
- **The protections held in every case.** No foreign row reached a caller; the overlapping lake was refused before DuckLake attached anything and read nothing. Only the cases' expected outcomes differed from the decision.

## Decision
- **ADR-0027 §3 stands unchanged and governs these cases.**
- `SEC-DQ-006` and `SEC-DQ-007` expect the lake's own rows, with the rows the view could name forbidden; their control shows those rows once the lake's owner adds them as a table's file. A dataset on a view, and a call that could reach a lake macro, stay refused as §3 says, as the server's and the statement guard's tests show.
- `SEC-DQ-018` expects `STORE_SOURCE_INVALID`; the runner declares a real overlap and checks that the lake is refused before its attach, with no read.

## Alternatives considered
- **Change the product to match the cases:** refuse every snapshot holding a view or a macro, and give overlaps a code of their own. Rejected: it would reverse §3 without a reason of security (the guard already makes views and macros unreachable), and real lakes with views could no longer be connected.

## Consequences
- The three cases change through their normative pull request (#447), which the Owner merges; their scenarios and every other expectation stay as written.
- A later case that contradicts a ranked document is corrected the same way: the ranked document is ratified, never silently overridden.
