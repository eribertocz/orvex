# 0029 — Semantic engine scope for v1.0.0: S1–S14

**Status:** Proposed (2026-11-01). Owner decision #237: the complete engine in v1.0.0.

This record amends **ADR-0010**: its first decision ("C1–C9 and S1–S6 for v1.0.0") becomes C1–C9 and S1–S14. Its other decisions stay as they are: the evidence checks, and the normative corpus with new cases from `CASE-101`.

## Context
- ADR-0010 sets v1.0.0 at the correctness core C1–C9 plus capabilities S1–S6.
- The Owner's RFC "ORVEX Semantic Engine — más allá de Power BI y Tableau" (#237) proposes S7–S14.
- The Owner decided that v1.0.0 ships the complete engine, not a reduced one.
- Two related directions are approved:
  - **#238:** row-policy propagation and combination, and filter-context rules;
  - **#239:** one type system, answerability and stability levels in the P2 contracts.

## Decision
1. **Scope.** v1.0.0 implements C1–C9 and S1–S14:
   - **S7:** metrics over metrics.
   - **S8:** formal context algebra (the precise definition of S1).
   - **S9:** window algebra. Correct values at subtotals and totals, densification against the calendar, and computation before paging, for every window operation. Includes the general `window` operator.
   - **S10:** Top N + Others, typed parameters, calculated dimensions and segments.
   - **S11:** cohorts, funnels and re-aggregation classes.
   - **S12:** bitemporality (`as_known_at`).
   - **S13:** the optimizer beyond the S5 certified aggregates.
   - **S14:** rejection with repairs: up to three valid alternative queries for a rejected one.
2. **Out of v1.0.0:**
   - cross-engine federation, which the RFC itself defers;
   - the comparative benchmark against Power BI and Tableau, pending a review of their license terms.
3. **One quality bar for every capability**, the same as S1–S6:
   - normative conformance cases, negative ones included;
   - exact values;
   - a stable rejection code instead of a wrong number;
   - an independent Reviewer pass.

   No capability ships in part or behind an empty contract field.
4. **Contracts.** P2-01 to P2-06 define each capability's representation from the start. Ordinary technical choices within them, a compatibility matrix included, are decided by the Agent following the approved contracts and recorded in the issue or pull request; product questions still go to the Owner. The contracts include #239's direction:
   - one type system shared by S7, S8, S9 and S11;
   - answerability, which unifies the catalog, the correctness core and S14 repairs;
   - stability levels, extending ADR-0004 rule 6.
5. **Security.** #238's rules apply to every capability, each with adversarial conformance cases:
   - policies propagate along relationships and combine unambiguously;
   - filter-context scopes follow one precedence;
   - no query, derived metric, window, cohort or repair bypasses an access restriction.
6. **Schedule.**
   - The new capabilities enter as issues in P2–P5, each created when its phase is planned. The approved issues keep their order.
   - About 15–20 new issues is an estimate, not a list to create in advance. It is a count only: the roadmap has no durations.
   - Each phase's exit criteria include its new capabilities.
   - Planning the new capabilities does not block P2-01, which starts at once.

## Consequences
- v1.0.0 arrives later, in proportion to the new issues.
- The engine's public contracts cover every capability from P2. Later changes follow compatible evolution (ADR-0004 rule 6 and the stability levels of decision 4): additions are optional fields, and an incompatible change needs a new contract version with a transition period.
- ADR-0010's status line notes that it is amended by this record. `docs/architecture/semantic-engine.md` gains S7–S14 when their contracts are specified. `docs/roadmap.md` gains the new issues once they are created.

## Resumen en español
v1 incluye el motor semántico completo, de S7 a S14, con las mismas exigencias de exactitud, pruebas y seguridad que lo ya existente. Solo quedan fuera la federación entre motores y la comparación con Power BI y Tableau. Los contratos de P2 cubren todas las capacidades desde el inicio y cambian después solo de forma compatible. P2-01 empieza ya, sin esperar la planificación del resto.
