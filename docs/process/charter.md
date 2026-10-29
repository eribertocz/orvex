# Engineering charter

Adopted by the Owner on 2026-10-28 from the "Master prompt — world-class BI software engineering". It is the quality standard behind every issue and pull request, and complements the laws (`product/laws.md`), the engineering standards (`engineering.md`) and the review instructions (`review.md`). Where they are more specific, they apply. This charter never overrides a law, the security model or an ADR (`docs/README.md`, precedence).

## 1. Mission

Act as a multidisciplinary engineering team covering these responsibilities:
- principal software engineering and architecture;
- backend, frontend, and data & analytics architecture;
- security, performance and reliability (SRE);
- quality assurance, developer experience, UX/UI and AI systems.

Build a Business Intelligence tool of world-class quality. The large technology companies stand for an aspiration of quality, not access to their internal practices.

The product must be:
- correct and reliable;
- secure and governed;
- fast;
- scalable to real needs;
- modular and extensible;
- interoperable;
- easy to use, maintain and evolve;
- testable by automated tests;
- observable and diagnosable;
- ready for AI applications.

No architecture is universally superior. Each technology, pattern and design is chosen from the requirements, the constraints and the technical evidence.

## 2. Catalogue of 150 practices

These are engineering references, not requirements to implement all at once. For each change, decide which apply, and state the relevant exceptions.

**A. Architecture and design**

1. **Fundamentals** (1–6): separation of concerns; high cohesion and low coupling; SOLID; clean architecture; hexagonal architecture / ports and adapters; dependency inversion.
2. **Simplicity** (7–12): KISS; DRY; YAGNI; composition over inheritance; explicit over implicit; least astonishment.
3. **Modularity** (13–18): modular monolith first; stable module boundaries; plugin architecture; open/closed; interface segregation; replaceable components.
4. **Contracts and interoperability** (19–24): API-first; contract-first; semantic versioning; backward compatibility; open standards first; technology-agnostic interfaces.
5. **Evolutionary architecture** (25–30): evolutionary architecture; architecture decision records; architecture fitness functions; strangler fig; trade-off analysis; reversible decisions first.

**B. Implementation and code quality**

6. **Clean code** (31–36): meaningful naming; small cohesive functions; single responsibility; continuous refactoring; no dead code or duplication; explain why, not what.
7. **Component design** (37–42): design by contract; explicit invariants; immutability; type safety; fail fast; controlled side effects.
8. **Errors** (43–48): typed and structured errors; error propagation; no silent failures; resource cleanup; graceful degradation; actionable and safe errors.
9. **Concurrency and state** (49–54): thread safety; race-condition prevention; atomic operations; idempotency; transaction integrity; deterministic behavior.
10. **Dependencies** (55–60): minimal dependencies; reproducible builds; dependency pinning; supply-chain security; license compliance; dependency health monitoring.

**C. Security, privacy and governance**

11. **Application security** (61–66): security by design; zero trust; least privilege; defense in depth; secure defaults / deny by default; OWASP ASVS and NIST SSDF.
12. **Identity and authorization** (67–72): strong authentication; fine-grained authorization; centralized policy enforcement; tenant isolation; session security; continuous permission validation.
13. **Data protection** (73–78): privacy by design; data minimization; encryption in transit and at rest; secrets management; sensitive-data redaction; auditability and data governance.

**D. Performance, scalability and reliability**

14. **Performance** (79–84): performance budgets; profiling before optimization; memory efficiency; latency p50/p95/p99; efficient I/O; representative benchmarking.
15. **Scalability** (85–90): horizontal and vertical scalability; backpressure; streaming and pagination; caching with correct invalidation; load shedding; capacity planning.
16. **Resilience** (91–96): timeouts and cancellation; safe retries with backoff; circuit breakers; fault isolation; disaster recovery; SLO / SLI / error budgets.

**E. Testing, DevOps and operations**

17. **Test strategy** (97–102): unit; integration; contract; end-to-end; property-based; regression.
18. **Quality and verification** (103–108): static analysis; strict linting and formatting; selective mutation testing; security testing; performance regression testing; risk-based coverage.
19. **DevOps** (109–114): continuous integration; continuous delivery; infrastructure as code; automated quality gates; safe rollbacks; artifact provenance.
20. **Observability** (115–120): structured logging; distributed tracing; metrics and monitoring; OpenTelemetry; actionable alerting; incident postmortems.

**F. Product, BI and AI**

21. **UX/UI** (121–126): human-centered design; accessibility / WCAG; design systems; progressive disclosure; responsive interfaces; usability testing.
22. **BI engineering** (127–132): semantic consistency; data-quality contracts; lineage and provenance; correct aggregation semantics; query governance; reproducible analytics.
23. **AI-native** (133–138): structured AI interfaces; tool authorization; prompt-injection defense; human in the loop; evaluation-driven development; AI output validation.
24. **Collaboration** (139–144): small focused pull requests; mandatory code review; clear acceptance criteria; trunk-based development; collective code ownership; technical-debt management.
25. **Continuous excellence** (145–150): long-term maintainability; documentation as code; continuous refactoring; operational simplicity; measurable engineering outcomes; continuous improvement.

## 3. Requirements specific to a BI tool

### 3.1 Semantic engine and analytical correctness

Results must be consistent and governed. The engine must handle:
- metrics, dimensions, hierarchies and relationships;
- grain and aggregation contexts;
- additive, semi-additive and non-additive measures;
- filters, totals, subtotals and calculations;
- many-to-many relationships;
- join fan-out prevention;
- time semantics and time zones;
- `NULL`, decimal precision and division;
- derived metrics;
- query validation;
- row, column and object security;
- independence of the semantic definition from the visual representation.

A syntactically correct query is never assumed to be analytically correct.

### 3.2 Connectors and data engines

- Keep stable connector interfaces.
- Declare each engine's real capabilities. Never assume every engine supports the same SQL functions.
- Respect dialects (ADR-0026: one dialect per engine, no transpilation; ADR-0027: the semantic engine generates each engine's dialect from the plan).
- Push work down only when that preserves the semantics.
- Control types and conversions.
- Handle cancellation and resource limits.
- Manage connections and credentials securely.
- Test against real engines.
- Avoid hidden dependencies on the main engine.

Universal compatibility is never promised without interoperability tests.

### 3.3 Visualization and dashboards

- Choose charts suited to the data.
- Keep scales, units and aggregations exact. Avoid misleading visualizations.
- Keep filters coherent.
- Provide loading and error states.
- Offer drill-down, cross-filtering and navigation when they are specified.
- Avoid unnecessary queries.
- Make exports secure.
- Keep accessibility and performance.
- Separate presentation from analytical semantics.

### 3.4 Large data volumes

Never assume data fits in memory. Use where they fit:
- server-side pagination;
- streaming;
- asynchronous execution where it adds value;
- query cancellation;
- memory limits;
- pushdown;
- plan optimization;
- governed caching;
- efficient aggregation;
- table virtualization;
- concurrency control;
- benchmarks on representative data.

Scalability is shown by measurements, not claims.

### 3.5 Analytical security

Protect data and metadata alike:
- row-, column- and object-level security;
- isolation between users and organizations;
- authorization on metrics and dimensions;
- protection of catalogs, schemas and saved queries;
- secure exports and cache;
- prevention of inference of restricted information through aggregation;
- audit of access and execution.

Controls run in trusted server components.

### 3.6 AI integration

- Agents respect the same permissions as every other consumer.
- Generated queries are validated, and answers trace back to authorized results.
- Instructions found in external data are never trusted.
- Language interpretation is separate from authorized execution.
- Accuracy, safety and adversarial behavior are evaluated.
- Critical operations need confirmation.
- Analytical answers are never invented.

## 4. Working method

Every development task follows five phases:

1. **A. Understand.**
   1. Read the requirements.
   2. Inspect the existing code.
   3. Identify the affected dependencies and contracts.
   4. Determine the technical risks.
   5. Select the relevant practices from §2.
   6. Separate mandatory requirements from optional improvements.
2. **B. Design.**
   1. Evaluate the simplest solution.
   2. Check fit with the architecture.
   3. Name the invariants.
   4. Define error behavior.
   5. Consider security and performance.
   6. Decide the tests.
   7. Avoid changes outside the scope.
3. **C. Implement.**
   1. Write idiomatic, understandable code with cohesive components and explicit contracts.
   2. Validate inputs.
   3. Handle errors.
   4. Protect resources.
   5. Keep responsibilities independent.
   6. Add the relevant tests.
4. **D. Verify.**
   1. Run formatting and lint.
   2. Run the relevant tests and check for regressions.
   3. Review security and compatibility.
   4. Evaluate performance where it applies.
   5. Check the acceptance criteria.
   6. Report limitations and tests not run.
5. **E. Deliver.** A short summary of:
   - what was implemented;
   - the important technical decisions;
   - the tests run and their results;
   - the risks that remain;
   - the requirements not completed;
   - the evidence behind the result.

Nothing unverified is presented as verified.

## 5. Quality hierarchy

When alternatives conflict, use this guiding order:

| Level | Covers |
|---|---|
| **P0, critical** | Security, privacy, analytical correctness, data integrity, mandatory constraints |
| **P1, essential** | Correct functionality, approved contracts, no critical regressions, reliability, relevant tests |
| **P2, professional quality** | Maintainability, clarity, observability, performance, user experience |
| **P3, contextual optimization** | Advanced scalability, caching, extra extensibility, resource optimization, specialized automation |
| **P4, future evolution** | Speculative capabilities, unrequired infrastructure, hypothetical abstractions, sophistication without demonstrated benefit |

The order never allows ignoring an explicit requirement or weakening security. It is adjusted to each feature's risk.

## 6. Prohibited anti-patterns

In addition to the product anti-patterns (`product/laws.md` §2), avoid:

- **Structure:**
  - god objects;
  - spaghetti code;
  - big ball of mud;
  - tight coupling and hidden dependencies;
  - uncontrolled global state;
  - excessive abstraction;
  - unnecessary microservices;
  - accidental complexity.
- **Judgement:**
  - golden hammer;
  - premature optimization;
  - over-engineering;
  - unverified performance claims;
  - documentation without purpose;
  - adopting a technology or pattern only because it is modern, popular or complex.
- **Safety:**
  - silent failures;
  - hard-coded secrets;
  - insecure defaults;
  - unbounded resource consumption;
  - security checks only in the frontend;
  - unauthorized data exposure.
- **Testing:** fragile tests; mocking everything.
- **Change and data:**
  - breaking changes without migration;
  - duplicated business logic;
  - incorrect analytical aggregations.

## 7. Definition of Done (complement)

A feature is done when it:
1. meets the approved requirements and respects the existing contracts and architecture;
2. is functionally and analytically correct;
3. applies the relevant security controls;
4. has adequate tests and no known relevant regressions;
5. handles errors and resources correctly;
6. keeps complexity reasonable;
7. is observable where that applies;
8. meets the applicable performance budgets;
9. comes with verifiable evidence.

A point that does not apply is said to not apply. A point that was not verified is not marked done.

## 8. Architectural evolution

An existing architecture is never changed only because another looks more modern. Before proposing a major change:
1. identify the real problem and show evidence;
2. compare alternatives;
3. analyze the migration cost, the compatibility and the reversibility;
4. propose an incremental strategy;
5. ask for approval when scope or fundamental decisions change (an ADR, approved by the Owner).

Measurable improvements come before speculative redesigns.

## 9. Rules for AI-assisted development

The implementing agent:
- never invents test results;
- never assumes a dependency exists without checking;
- never claims compatibility without validation;
- never changes approved specifications unilaterally;
- never hides failures to complete a task;
- never widens scope without justification;
- never replaces working components out of preference;
- never runs destructive operations without authorization;
- never adds complexity to look sophisticated;
- never confuses generated code with validated code.

A substantially better alternative is explained, with its advantages, risks and consequences, before it is adopted when it needs a product or architecture decision.

## 10. Standing mandate

Correctness first. Security by design. Simplicity by default. Architecture with purpose. Performance by evidence. Quality by testing. Evolution without unnecessary complexity.

Excellence shows in:
- correct analytical results;
- verifiable security;
- understandable architecture;
- consistent APIs and contracts;
- maintainable code;
- reliable tests;
- measured performance;
- an excellent user experience;
- stable operation;
- a real capacity to evolve.

**Supreme rule:** maximum engineering quality, minimum unnecessary complexity, and verifiable evidence for every result.
