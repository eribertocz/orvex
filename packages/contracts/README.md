# `orvex-contracts` — `orvex.contracts`

Every module may import this package. It holds:

- Python types generated from [`contracts/`](../../contracts/README.md) by `python -m tools.contracts.generate` (`<contract>/v<N>.py`), never edited by hand;
- the interface-contract format (ADR-0033, #486): `interface.py` (operations, inputs, outputs, errors, capabilities, version), `kit.py` and `kit_run.py` (conformance kits: cases, recorded count, no skips, run in process and serialized), `binding.py`, `codec.py` and `serializable.py` (what may cross a contract), `manifest.py` (`module.toml`), `registry.py` and `interface_errors.py`;
- `module.toml`, this module's manifest, and `interface-record.json` (beside `pyproject.toml`), the record of every interface contract that `python -m tools.contracts.interface_record` writes and `python -m tools.contracts.interface_compat` compares with the last release.

Module pack: [`docs/modules/contracts.md`](../../docs/modules/contracts.md).

## Tests

```bash
pytest packages/contracts
python -m tools.ci.standalone contracts   # built, installed and tested alone, outside the repository
```
