# `orvex-testing` — `orvex.testing`

Test helpers that no single module owns, shared by the modules' own tests and the repository's
tests (#486 §2 item 4). It is never shipped, depends on no Orvex module, and is installed beside a
module in its standalone job. Only tests may import it; the boundaries check refuses it anywhere
else. A helper that only one module's tests use lives in that module's tests instead.

- `bounds`: cost bounds that fail a quadratic regression, not a slow runner (#329).
- `environment`: a test that needs a service skips locally and fails in CI (`unavailable`).
- `fixtures`: shared fixture files frozen read-only, and private copies (#376).
- `facade_interface`: the recorded names and shapes of a module's façade (#246).
- `azurite`: a client of the Azurite blob emulator for object-storage tests (#127).
