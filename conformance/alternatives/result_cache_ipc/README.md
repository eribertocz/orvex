# `orvex-alternative-result-cache-ipc`

An independent implementation of the result-cache interface contract
(`orvex.contracts.semantic.interface`), written from the contract and its kit alone, for the
real substitution test of #486 G1. It is used only in tests and never shipped.

Its design differs from the semantic module's cache: every entry is held as Arrow IPC bytes and
decoded on each hit, and recency is tracked with a counter and a heap. It imports only
`orvex.contracts`, `pyarrow` and the standard library.
