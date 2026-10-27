# Contributing

Rules for every change: [`AGENTS.md`](AGENTS.md) and [`docs/process/`](docs/process/README.md). This page only lists the commands. They are the same commands CI runs.

## Python workspace

Requirements: Python 3.15 (also tested on 3.14) and pip 26.2.2 or newer, because pip installs from `pylock.toml` only from 26.1 on.

Every module under `packages/` is its own distribution in the `orvex` namespace (PEP 420). The root `pyproject.toml` holds only tool settings and the `dev` dependency group.

### Set up

```bash
python3.15 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install pip==26.2.2
python -m pip install -r pylock.toml # Python 3.14: pylock.py314.toml
for package in packages/*/; do python -m pip install --no-deps --no-build-isolation -e "$package"; done
```

On Windows PowerShell, replace the last line with:
`Get-ChildItem packages -Directory | ForEach-Object { python -m pip install --no-deps --no-build-isolation -e $_.FullName }`

### Check

```bash
ruff format --check .
ruff check .
mypy -p orvex.store -p orvex.transform -p orvex.semantic -p orvex.access -p orvex.dashboards -p orvex.mcp -p orvex.server -p orvex.contracts
mypy packages/*/tests tests tools .github/workflows/scripts
pytest
```

### Architecture and naming checks

```bash
python -m tools.checks.product_name   # PRODUCT_NAME_HARDCODED
python -m tools.checks.boundaries     # MODULE_BOUNDARY_VIOLATION, MATERIALIZATION_IN_PYTHON
```

### Contracts

```bash
python -m tools.contracts.validate          # schemas, examples, closed objects
python -m tools.contracts.generate          # regenerate Python and TypeScript types (needs web/node_modules)
python -m tools.contracts.generate --check  # CONTRACT_TYPES_STALE when the committed types are out of date
```

### Rename the product

```bash
python -m tools.rename.rename "New Name" new-slug --dry-run   # show what would change
python -m tools.rename.rename "New Name" new-slug             # apply, then review git diff
```

### Update dependencies

1. Change the exact pin (`==`) in `pyproject.toml`: the `dev` group for tools, the `runtime` group **and** the package's `dependencies` for runtime libraries.
2. Regenerate both lock files, each with its own Python version:

   ```bash
   python3.15 -m pip lock --group dev --group runtime -o pylock.toml
   python3.14 -m pip lock --group dev --group runtime -o pylock.py314.toml
   ```

`pip lock` is still marked experimental by pip. The lock files are valid for Linux x86_64, the CI platform. Lock files for Windows and macOS arrive with the operating-system matrix (P9-10). `tests/workspace/test_lock_files.py` fails while a lock file disagrees with `pyproject.toml`.

## Web workspace

Requirements: Node.js from `web/.nvmrc`, the latest LTS line.

```bash
cd web
npm ci
npm run check    # svelte-check, warnings are errors
npm run lint     # Prettier and ESLint
npm run test     # Vitest
npm run build    # static files in web/build
```

## Licenses

```bash
python tools/licenses/check_licenses.py   # with the Python workspace installed and web/package-lock.json present
```

The allow-list is `tools/licenses/allowed.toml`. A runtime dependency may use only the runtime licenses; development tools may also use the development licenses.
