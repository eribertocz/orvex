# `semantic-explain` contract v1

**Producer:** `semantic` (`orvex.semantic.explanation.explain`). **Consumers:** `server` (`POST /api/v1/semantic/explain`, P2-27b #364), later `web` and `mcp`.

What the engine would do with a question, and why, with **no data read**. The steps before a read run: binding, `analyze`, the cursor's snapshot, `plan`, the adapter, discharge (C9), the evidence relied on, the choice of the aggregate table and the estimate. The engine's read is never called. Zones are not checked, because checking them reads. The only metadata used is the snapshot, the tables and columns at it, file statistics, and the evidence records.

## Outcome and stage

- `outcome: planned`: the question would be answered. `stage` is `planned`.
- `outcome: refused`: the plan itself refuses the question with an error of category `correctness`, `budget` or `engine`. Violated evidence the plan relies on counts here too (`EVIDENCE_VIOLATED`). `refusal` holds that `semantic-error` v1 message in the caller's audience: public details only, unless the caller is a builder.
- Errors of category `query` or `security` are **not** explanations. They are an invalid query, an unknown or hidden member, a masked member or a cursor, and the API sends them as errors, as `query` does. So a hidden member is never confirmed to exist.
- `stage` is the last stage reached: `received`, `bound`, `analyzed` or `planned`. Each part appears from the stage that produces it on:

| Part | From | Audience |
|---|---|---|
| `kind`, `version`, `outcome`, `stage`, `model_fingerprint`, `snapshot` | always | every caller |
| `query_fingerprint`, `question` | `bound` | every caller (`question` restricted to what the caller sees) |
| `plan` | `analyzed` | builder |
| `evidence`, `materialization`, `estimate` | `planned` | builder; `estimate` left out under a row policy |
| `refusal` | `outcome: refused` | every caller, its details by audience |

## The receipt's parts, the receipt's way

`question`, `plan` and `evidence` are built and restricted exactly as the receipt's view of the same question on the same snapshot shows them (`receipt` v1, "Views"): same shapes and allow-list, and the same entries for the same viewer. Their definitions here are copies of `receipt` v1's `$defs`, and a test keeps them equal. The one difference is `Evidence.status`, which may also be `violated`.

- `materialization`: the aggregate table read in place of its base (C8), or the reason none is: `no_eligible_table` or `not_verified_consistent`.
- `estimate`: the read-cost upper bound from the snapshot's file statistics (#352), in the receipt's `Figures` shape. It is absent when the store gives no statistics, and always absent whenever the plan reads a dataset with a row policy, since it would reveal a table's size (#238 rule 12).

## Never

No SQL, statement text, bound parameter or data value, for any audience, builders included, as `receipt` v1 forbids. Every string is a `Label`, a fingerprint, an instant or an enumerated value.

## Rules beyond the schema shape

- `outcome: refused` ⇔ `refusal` is present. `outcome: planned` ⇒ `stage: planned`.
- `stage: received` ⇒ neither `query_fingerprint` nor `question`; any later stage ⇒ both.
- `plan` only from `analyzed` on; `evidence`, `materialization`, `estimate` only at `planned`.
