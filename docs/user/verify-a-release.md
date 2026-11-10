# Verify a release before installing it

Each release of Orvex publishes, next to its wheels:

- `SHA256SUMS`: the SHA-256 checksum of every file;
- `orvex-<version>.cdx.json`: the software bill of materials (SBOM). It is a
  [CycloneDX 1.6](https://cyclonedx.org/docs/1.6/json/) list of every product wheel, every
  Python package the product needs at run time and every JavaScript package in its web
  interface, each with its version, package URL and hashes.

GitHub also keeps two signed records (attestations) for every release file:

- **build provenance**: the file was built by this repository's `release` workflow, from the
  tagged commit;
- **SBOM**: the SBOM belongs to each wheel.

The records are signed through [Sigstore](https://www.sigstore.dev/). Checking them needs the
[GitHub CLI](https://cli.github.com/) 2.49 or newer, and nothing else.

## 1. Check the checksums

Put the downloaded files in one folder and run:

```console
$ sha256sum --check --ignore-missing SHA256SUMS
orvex-0.3.1-py3-none-any.whl: OK
orvex-0.3.1.cdx.json: OK
```

Every file you downloaded must say `OK`. On macOS, use `shasum -a 256 --check --ignore-missing
SHA256SUMS`.

## 2. Check where each file was built

```console
$ gh attestation verify orvex-0.3.1-py3-none-any.whl --repo eribertocz/orvex
Loaded digest sha256:… for file://orvex-0.3.1-py3-none-any.whl
Loaded 1 attestation from GitHub API
✓ Verification succeeded!

The following policy criteria will be enforced:
- Predicate type must match:................ https://slsa.dev/provenance/v1
- Source Repository Owner URI must match:... https://github.com/eribertocz
- Source Repository URI must match:......... https://github.com/eribertocz/orvex
- Subject Alternative Name must match regex: (?i)^https://github.com/eribertocz/orvex/
- OIDC Issuer must match:................... https://token.actions.githubusercontent.com
```

Repeat for each wheel and for the SBOM. The output names the signing workflow,
`.github/workflows/release.yml`, and the commit it ran on, which must be the release's tag. To
require both, add `--signer-workflow eribertocz/orvex/.github/workflows/release.yml` and
`--source-ref refs/tags/v0.3.1`.

## 3. Check the SBOM belongs to the wheel

```console
$ gh attestation verify orvex-0.3.1-py3-none-any.whl --repo eribertocz/orvex \
    --predicate-type https://cyclonedx.org/bom
✓ Verification succeeded!
```

## When a check fails

Do not install the file. It does not match what this repository built and published. It may have
been altered after the build, damaged while downloading, or built by someone else. For example,
a wheel changed after the release gives:

```console
$ gh attestation verify orvex-0.3.1-py3-none-any.whl --repo eribertocz/orvex
Loaded digest sha256:… for file://orvex-0.3.1-py3-none-any.whl
✗ Verification failed: no attestations found for subject
```

`sha256sum` says `FAILED` for the same file. Download it again from the release page. If it
still fails, report it through the repository's security policy.
