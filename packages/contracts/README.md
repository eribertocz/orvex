# `orvex-contracts` — `orvex.contracts`

Every module may import this package. It holds:

- Python types generated from [`contracts/`](../../contracts/README.md) by `python -m tools.contracts.generate` (`<contract>/v<N>.py`), never edited by hand;
- the interface-contract format (ADR-0033, #486): `interface.py` (operations, inputs, outputs, errors, capabilities, state changes) and `version.py`; `envelope.py` (the call envelope every binding validates) and `context.py` (the call context envelopes are made from); `kit.py`, `kit_client.py`, `kit_case.py`, `kit_process.py` and `kit_run.py` (conformance kits, cases with their own environment run in their own process: cases, recorded count, idempotency, no skips, run in process and serialized by consumers at N-1 and N); `binding.py`, `codec.py` and `serializable.py` (what may cross a contract); `adapter.py` (public test adapters); `manifest.py` (`module.toml`); `registry.py` and `interface_errors.py`;
- `module.toml`, this module's manifest, and `interface-record.json` (beside `pyproject.toml`), the record of every interface contract that `python -m tools.contracts.interface_record` writes and `python -m tools.contracts.interface_compat` compares with the last release.

Module pack: [`docs/modules/contracts.md`](../../docs/modules/contracts.md).

## Tests

```bash
pytest packages/contracts
python -m tools.ci.standalone contracts   # built, installed and tested alone, outside the repository
```
