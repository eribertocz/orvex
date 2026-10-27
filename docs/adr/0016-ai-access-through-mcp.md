# 0016 — AI access through MCP

**Status:** Accepted (2026-10-26)

## Context
The Model Context Protocol was donated by Anthropic to the Agentic AI Foundation (a directed fund of the Linux Foundation, co-founded with Block and OpenAI) in December 2025 and is integrated in major AI assistants. Rill exposes its semantic layer through an MCP server. Apache Ossie includes `ai_context` on models and members.

## Decision
- A separate `mcp` module exposes tools `describe_model`, `query`, `explain` and `list_dashboards` over the semantic contract only.
- AI acts as the authenticated user with the same permissions; it never sends SQL; read-only by default; every call audited.
- Answers cite the certified metric and include the receipt.
- Every project contains a generated `AGENTS.md` so coding agents understand it.

## Alternatives considered
- Text-to-SQL: bypasses the single source of truth and security.

## Consequences
The `mcp` module depends only on contracts and can evolve independently.
