"""The required result of a scoped check (#267): its workflow's aggregator job.

Reads `CHECK` (its name), `CHANGES` (the `changes` job's result), `RELEVANT` (its output),
`RESULT` (the real check job's result) and `RELEVANT_PATHS`. It passes when the check applied
and its job succeeded, and reports "Not applicable" only when the `changes` job succeeded and
proved that nothing relevant changed, with the real job skipped. Anything else fails.
"""

import enum
import os
import sys


class Verdict(enum.Enum):
    PASSED = "passed"
    NOT_APPLICABLE = "not applicable"
    FAILED = "failed"


def decide(changes: str, relevant: str, result: str) -> Verdict:
    if changes != "success":
        return Verdict.FAILED
    if relevant == "true":
        return Verdict.PASSED if result == "success" else Verdict.FAILED
    if relevant == "false" and result == "skipped":
        return Verdict.NOT_APPLICABLE
    return Verdict.FAILED


def main() -> int:
    check = os.environ.get("CHECK", "check")
    changes = os.environ.get("CHANGES", "")
    relevant = os.environ.get("RELEVANT", "")
    result = os.environ.get("RESULT", "")
    paths = [p.strip() for p in os.environ.get("RELEVANT_PATHS", "").splitlines() if p.strip()]
    verdict = decide(changes, relevant, result)
    if verdict is Verdict.NOT_APPLICABLE:
        text = f"Not applicable: no change under {', '.join(paths)}"
        print(f"::notice title=Not applicable::{check}: {text}")
    elif verdict is Verdict.PASSED:
        text = f"Passed: {check} ran on this change and succeeded"
    else:
        text = (
            f"Failed: changes={changes or 'none'}, relevant={relevant or 'none'}, "
            f"{check} job={result or 'none'}"
        )
        print(f"::error title={check}::{text}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### {check}\n\n{text}\n")
    print(text)
    return 1 if verdict is Verdict.FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
