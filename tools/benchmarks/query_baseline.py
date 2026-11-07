"""P2 query-performance baseline (#364): ask the semantic HTTP API fixed questions over a
synthetic dataset and measure the answers, each checked against an independent reference.

Builds the load baseline's project (`load_baseline.project`) over a dataset written by
`tools.synthetic.generate`, adds the fixed semantic model of `benchmarks/query-baseline/`, and
loads it once with the real `run` command. Then, for each of `--rounds` independent rounds, it
starts a new server process (`serve --no-browser`) and, for every question of
`benchmarks/query-baseline/questions.json`:

1. 5 warm-up requests, not timed;
2. 50 timed requests one at a time (concurrency 1);
3. 200 timed requests from 4 clients at once (concurrency 4).

- **Time** is measured on the client, from sending the request to its last byte. p50 and p95 use
  the nearest-rank method over every timed request of a round. Throughput is completed requests
  per second at concurrency 4.
- **Every answer is checked.** A question with a `reference` must return exactly the rows of that
  SQL, run by DuckDB through the store on the same snapshot: values as `store/v1` JSON text, so a
  decimal is compared exactly. `catalog` must list the model's members, and `explain` must say
  `planned`. A wrong answer counts as an error, never as a fast one.
- **Memory** is the server process's peak resident set, sampled every 100 ms.

Nothing is cached by the product. The operating system's file cache stays warm after the warm-up,
and the results say so. The results are written as JSON and Markdown, with the commit, the CI
run, the versions, the machine and the rows per table.

    python -m tools.benchmarks.query_baseline --data data/large --work /tmp/qb --out out
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from orvex.contracts.store.v1 import ReadRequest
from orvex.server import commands
from orvex.server.cli import free_port
from orvex.server.identity import load_identity
from orvex.server.instance import home_variable
from orvex.server.project import load_project
from orvex.store.json_encoding import arrow_to_json

from tools.benchmarks import load_baseline

ROOT = Path(__file__).resolve().parents[2]
FIXED = ROOT / "benchmarks" / "query-baseline"
WARM_UP = 5
SEQUENTIAL = 50
CONCURRENT = 200
CLIENTS = 4
SAMPLE_SECONDS = 0.1
REQUEST_SECONDS = 600
START_SECONDS = 120
IDENTITY = load_identity()
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback, never a proxy


@dataclass
class Timings:
    """One question in one round."""

    question: str
    round: int
    sequential_ms: list[float] = field(default_factory=list)
    concurrent_ms: list[float] = field(default_factory=list)
    concurrent_seconds: float = 0.0
    errors: dict[str, int] = field(default_factory=dict)
    answer_sha256: str = ""


def nearest_rank(values: list[float], percent: float) -> float:
    """The nearest-rank percentile: the smallest value with at least `percent` of the values at
    or below it."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percent / 100 * len(ordered)) - 1)]


def reference_rows(folder: Path, sql: str) -> list[list[object]]:
    """The reference answer: `sql` run by DuckDB through the store on the lake's current
    snapshot, as `store/v1` JSON values."""
    home = folder / f".{IDENTITY.slug}"
    with commands.project_lock(home):
        lake = commands.open_existing(home, commands.lake_location(load_project(folder), home))
        try:
            snapshot = lake.current_snapshot().id
            reader = lake.read(
                ReadRequest(
                    kind="read", sql=sql, parameters=[], snapshot=snapshot, batch_size=65_536
                )
            )
            rows: list[list[object]] = json.loads("".join(arrow_to_json(reader)))["rows"]
            return rows
        finally:
            lake.close()


class Server:
    """A new server process for one round, and its peak resident memory."""

    def __init__(self, folder: Path, instance: Path) -> None:
        self.port = free_port(18_000)
        environment = {**os.environ, home_variable(IDENTITY): str(instance)}
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "orvex.server",
                str(folder),
                "--no-browser",
                "--port",
                str(self.port),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
        )
        assert self.process.stdout is not None
        deadline = time.monotonic() + START_SECONDS
        lines: list[str] = []
        token_file: Path | None = None
        while token_file is None:
            line = self.process.stdout.readline()
            if not line or time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("the server did not start: " + "".join(lines[-20:]))
            lines.append(line)
            if "Authorization: Bearer" in line:
                token_file = Path(line.rsplit(": ", 1)[1].strip())
        self.token = token_file.read_text().strip()
        while True:  # the address is announced before the server listens
            try:
                if self.request("/api/v1/health", None)[0] == 200:
                    break
            except urllib.error.URLError:
                pass
            if time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("the server never answered its health check")
            time.sleep(0.2)
        self.peak = 0
        self._running = True
        self._sampler = threading.Thread(target=self._sample, daemon=True)
        self._sampler.start()
        threading.Thread(target=self.process.stdout.read, daemon=True).start()

    def _sample(self) -> None:
        while self._running:
            resident = load_baseline._resident(self.process.pid)
            if resident is not None:
                self.peak = max(self.peak, resident)
            time.sleep(SAMPLE_SECONDS)

    def stop(self) -> None:
        self._running = False
        self.process.terminate()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

    def request(self, path: str, body: object | None) -> tuple[int, bytes, float]:
        """One request: status, body and milliseconds from sending to the last byte."""
        data = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        call = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers=headers,
            method="GET" if data is None else "POST",
        )
        started = time.perf_counter()
        try:
            with DIRECT.open(call, timeout=REQUEST_SECONDS) as response:
                content = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            content, status = error.read(), error.code
        return status, content, (time.perf_counter() - started) * 1000


def checker(
    name: str, spec: dict[str, Any], expected: dict[str, Any]
) -> Callable[[int, bytes], str]:
    """A check of one answer: "" when it is right, else the kind of error."""

    def check(status: int, content: bytes) -> str:
        if status != 200:
            try:
                return f"{status} {json.loads(content)['error']['code']}"
            except ValueError, KeyError, TypeError:
                return str(status)
        document = json.loads(content)
        if "reference" in spec:
            if document["result"]["rows"] != expected[name]:
                return "wrong rows"
        elif name == "catalog":
            if json.dumps(document).count('"name"') < expected["catalog"]:
                return "wrong catalog"
        elif document.get("outcome") != "planned":
            return "not planned"
        return ""

    return check


def bodies(questions: dict[str, Any], server: Server) -> dict[str, object | None]:
    """Each question's body; a next page asks with the first page's cursor."""
    found: dict[str, object | None] = {}
    for name, spec in questions.items():
        if "body" in spec:
            found[name] = spec["body"]
        elif "explains" in spec:
            found[name] = questions[spec["explains"]]["body"]
        elif "next_page_of" in spec:
            first = questions[spec["next_page_of"]]["body"]
            _, content, _ = server.request(questions[spec["next_page_of"]]["path"], first)
            cursor = json.loads(content)["result"]["page"]["next"]
            found[name] = {**first, "page": {**first["page"], "after": cursor}}
        else:
            found[name] = None
    return found


def measure_round(
    number: int, folder: Path, instance: Path, questions: dict[str, Any], expected: dict[str, Any]
) -> tuple[list[Timings], int]:
    server = Server(folder, instance)
    timings = []
    try:
        asked = bodies(questions, server)
        for name, spec in questions.items():
            check = checker(name, spec, expected)
            body = asked[name]
            found = Timings(name, number)
            errors: Counter[str] = Counter()
            for _ in range(WARM_UP):
                status, content, _ = server.request(spec["path"], body)
                if problem := check(status, content):
                    errors[problem] += 1
            for _ in range(SEQUENTIAL):
                status, content, elapsed = server.request(spec["path"], body)
                if problem := check(status, content):
                    errors[problem] += 1
                else:
                    found.sequential_ms.append(round(elapsed, 3))
                    found.answer_sha256 = (
                        hashlib.sha256(content).hexdigest() if "reference" in spec else ""
                    )

            def one(
                _: int,
                spec: dict[str, Any] = spec,
                body: object = body,
                check: Callable[[int, bytes], str] = check,
            ) -> tuple[str, float]:
                status, content, elapsed = server.request(spec["path"], body)
                return check(status, content), elapsed

            started = time.perf_counter()
            with ThreadPoolExecutor(CLIENTS) as pool:
                for problem, elapsed in pool.map(one, range(CONCURRENT)):
                    if problem:
                        errors[problem] += 1
                    else:
                        found.concurrent_ms.append(round(elapsed, 3))
            found.concurrent_seconds = round(time.perf_counter() - started, 3)
            found.errors = dict(errors)
            timings.append(found)
            print(json.dumps(summary_of(found)), flush=True)
    finally:
        server.stop()
    return timings, server.peak


def summary_of(found: Timings) -> dict[str, object]:
    shown: dict[str, object] = {"question": found.question, "round": found.round}
    for label, values in (("c1", found.sequential_ms), ("c4", found.concurrent_ms)):
        if values:
            shown[f"{label}_p50_ms"] = nearest_rank(values, 50)
            shown[f"{label}_p95_ms"] = nearest_rank(values, 95)
            shown[f"{label}_max_ms"] = max(values)
    if found.concurrent_seconds:
        shown["c4_throughput_per_s"] = round(len(found.concurrent_ms) / found.concurrent_seconds, 2)
    shown["errors"] = found.errors
    return shown


def markdown(result: dict[str, Any]) -> str:
    rows = [
        "| Question | Round | c1 p50 / p95 / max (ms) | c4 p50 / p95 / max (ms) "
        "| c4 req/s | Errors |",
        "|---|---|---|---|---|---|",
    ]
    for item in result["rounds"]:
        for shown in item["questions"]:
            c1 = " / ".join(f"{shown.get(f'c1_{k}_ms', 0):.1f}" for k in ("p50", "p95", "max"))
            c4 = " / ".join(f"{shown.get(f'c4_{k}_ms', 0):.1f}" for k in ("p50", "p95", "max"))
            errors = ", ".join(f"{k}: {v}" for k, v in shown["errors"].items()) or "none"
            rows.append(
                f"| {shown['question']} | {shown['round']} | {c1} | {c4} "
                f"| {shown.get('c4_throughput_per_s', 0)} | {errors} |"
            )
    rows += ["", "| Round | Server peak memory (MiB) |", "|---|---|"]
    rows += [
        f"| {r['round']} | {r['peak_rss_bytes'] / (1024 * 1024):.0f} |" for r in result["rounds"]
    ]
    return "\n".join(rows) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="a generated dataset folder")
    parser.add_argument("--work", type=Path, required=True, help="where the project is built")
    parser.add_argument("--out", type=Path, required=True, help="where the results are written")
    parser.add_argument("--rounds", type=int, default=3)
    arguments = parser.parse_args()
    if arguments.rounds < 1:
        print("--rounds must be at least 1", file=sys.stderr)
        return 2
    data = arguments.data.resolve()
    work = arguments.work.resolve()
    folder, instance = work / "project", work / "instance"
    shutil.rmtree(work, ignore_errors=True)
    load_baseline.project(folder, data)
    (folder / "semantic").mkdir(parents=True)
    shutil.copy(FIXED / "model.ossie.json", folder / "semantic" / "query_baseline.ossie.json")
    load = subprocess.run(
        [sys.executable, "-m", "orvex.server", "run", str(folder)],
        capture_output=True,
        text=True,
        env={**os.environ, home_variable(IDENTITY): str(instance)},
        check=False,
    )
    if load.returncode != 0:
        print(load.stdout[-4000:], load.stderr[-4000:], file=sys.stderr)
        return 1
    questions: dict[str, Any] = json.loads((FIXED / "questions.json").read_text("utf-8"))
    expected: dict[str, Any] = {
        name: reference_rows(folder, spec["reference"])
        for name, spec in questions.items()
        if "reference" in spec
    }
    model: dict[str, Any] = json.loads((FIXED / "model.ossie.json").read_text("utf-8"))
    datasets: list[dict[str, Any]] = model["datasets"]
    expected["catalog"] = len(model["metrics"]) + sum(len(d["fields"]) for d in datasets)
    rounds = []
    all_timings: list[list[Timings]] = []
    for number in range(1, arguments.rounds + 1):
        timings, peak = measure_round(number, folder, instance, questions, expected)
        all_timings.append(timings)
        rounds.append(
            {
                "round": number,
                "peak_rss_bytes": peak,
                "questions": [summary_of(t) for t in timings],
                "raw": [asdict(t) for t in timings],
            }
        )
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    result: dict[str, Any] = {
        "commit": os.environ.get("GITHUB_SHA", ""),
        "run_url": (
            f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
            if "GITHUB_RUN_ID" in os.environ
            else ""
        ),
        "dataset": {key: manifest[key] for key in ("size", "seed", "rows") if key in manifest},
        "versions": load_baseline.versions(),
        "machine": load_baseline.machine(),
        "method": {
            "warm_up": WARM_UP,
            "sequential": SEQUENTIAL,
            "concurrent": CONCURRENT,
            "clients": CLIENTS,
            "percentiles": "nearest rank",
            "caches": "no result cache in the product; the OS file cache is warm after warm-up",
        },
        "expected_rows": {
            name: len(rows) for name, rows in expected.items() if isinstance(rows, list)
        },
        "rounds": rounds,
    }
    arguments.out.mkdir(parents=True, exist_ok=True)
    (arguments.out / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    (arguments.out / "results.md").write_text(markdown(result))
    print(markdown(result))
    failed = any(t.errors for item in all_timings for t in item)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
