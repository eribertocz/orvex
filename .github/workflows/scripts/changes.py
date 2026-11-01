"""Whether a scoped check applies to this run (#267): the `changes` job of its workflow.

Reads `EVENT`, `BASE`, `HEAD` (the pull request's base and head commits) and `RELEVANT_PATHS`
(one path per line; `folder/**` means everything under the folder), and writes
`relevant=true` or `relevant=false` to `GITHUB_OUTPUT`. A run that is not a pull request always
applies. Any failure (a missing input, a commit git cannot read) exits non-zero and writes
nothing, so the check's aggregator fails instead of reporting "Not applicable".
"""

import os
import subprocess
import sys


def relevant_files(changed: list[str], paths: list[str]) -> list[str]:
    def matches(name: str, path: str) -> bool:
        if path.endswith("/**"):
            return name.startswith(path[: -len("**")])
        return name == path

    return [name for name in changed if any(matches(name, path) for path in paths)]


def main() -> int:
    paths = [line.strip() for line in os.environ.get("RELEVANT_PATHS", "").splitlines()]
    paths = [path for path in paths if path]
    if not paths:
        print("::error title=changes::RELEVANT_PATHS is empty")
        return 1
    if os.environ.get("EVENT") != "pull_request":
        relevant = True
    else:
        base, head = os.environ.get("BASE", ""), os.environ.get("HEAD", "")
        if not base or not head:
            print("::error title=changes::the pull request's base or head commit is missing")
            return 1
        # --no-renames: a file moved out of a relevant path is listed under its old name too;
        # -z: names are not quoted (non-ASCII characters) and are split exactly.
        process = subprocess.run(
            ["git", "diff", "--no-renames", "--name-only", "-z", f"{base}...{head}"],
            capture_output=True,
            text=True,
        )
        if process.returncode != 0:
            print(f"::error title=changes::git diff failed: {process.stderr.strip()}")
            return 1
        found = relevant_files([n for n in process.stdout.split("\0") if n], paths)
        print("Relevant changes:" if found else "No change under: " + ", ".join(paths))
        for name in found:
            print(f"  {name}")
        relevant = bool(found)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"relevant={'true' if relevant else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
