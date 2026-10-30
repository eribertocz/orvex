"""Command line: `<slug>` opens an in-memory project, `<slug> <path>` opens or creates a project."""

from __future__ import annotations

import argparse
import math
import signal
import socket
import sys
import threading
import webbrowser
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from importlib.resources import files
from pathlib import Path
from types import FrameType

import uvicorn
from orvex.server.app import create_app
from orvex.server.commands import run, runs, validate
from orvex.server.identity import Identity, load_identity
from orvex.server.instance import (
    Project,
    ProjectPathError,
    ProjectRegistry,
    in_memory_project,
    instance_directory,
    open_project,
)
from orvex.server.logs import configure_logging
from orvex.server.maintenance import history, maintain
from orvex.server.scheduler import STOP_GRACE_SECONDS, schedule
from orvex.server.versions import (
    ContractVersionIncompatible,
    Versions,
    check_compatibility,
    current_versions,
    installed_declarations,
)

LOCAL_HOST = "127.0.0.1"
COMMANDS = frozenset({"validate", "run", "runs", "schedule", "maintain"})
DEFAULT_PORT = 7878
PORT_ATTEMPTS = 100
MAX_PORT = 65_535


def parser_for(identity: Identity) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=identity.slug,
        description=f"{identity.name}: open a project in the browser.",
    )
    parser.add_argument(
        "project",
        nargs="?",
        type=Path,
        help="project folder to open (created if missing); omitted: a temporary in-memory project",
    )
    parser.add_argument(
        "--port",
        type=port_number,
        default=DEFAULT_PORT,
        help=f"first port to try, from 1 to {MAX_PORT}; the next free one is used if it is busy",
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--version", action="store_true", help="print versions and exit")
    return parser


def port_number(text: str) -> int:
    """A TCP port from 1 to 65535 (#144, decision #182): port 0 would let the system pick a port
    that is never shown, so the printed address would answer nothing."""
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= MAX_PORT:
        raise argparse.ArgumentTypeError(f"must be a port number from 1 to {MAX_PORT}")
    return value


def free_port(first: int, host: str = LOCAL_HOST, attempts: int = PORT_ATTEMPTS) -> int:
    """The first port from `first` on that can be bound on `host`, never past 65535."""
    for port in range(first, min(first + attempts, MAX_PORT + 1)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind((host, port))
            except OSError:
                continue
            return port
    raise OSError(f"no free port from {first} to {min(first + attempts - 1, MAX_PORT)}")


def web_build_directory() -> Path | None:
    """The web build packaged with the server, or the repository's web/build during development."""
    packaged = Path(str(files("orvex.server").joinpath("web")))
    if (packaged / "index.html").is_file():
        return packaged
    repository = Path(__file__).resolve().parents[5] / "web" / "build"
    return repository if (repository / "index.html").is_file() else None


def describe_versions(identity: Identity, versions: Versions) -> str:
    lines = [f"{identity.name} {versions.product}"]
    lines += [f"  module   {name} {number}" for name, number in versions.modules]
    lines += [f"  contract {name} {number}" for name, number in versions.contracts]
    return "\n".join(lines)


def server_config(port: int, versions: Versions, web_build: Path | None) -> uvicorn.Config:
    """Local mode binds to the loopback interface only, never to 0.0.0.0."""
    return uvicorn.Config(
        create_app(versions, web_build, port),
        host=LOCAL_HOST,
        port=port,
        log_config=None,
        access_log=False,
    )


def command_parser(identity: Identity) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=identity.slug)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="check a project without changing anything")
    validate.add_argument("project", type=Path, help="project folder")
    validate.add_argument(
        "--show-sql",
        metavar="MODEL",
        default=None,
        help="print the SQL that runs for MODEL (views inlined, automatic filter placed)",
    )
    run = commands.add_parser("run", help="build every model of a project into its local lake")
    run.add_argument("project", type=Path, help="project folder")
    run.add_argument(
        "--full-refresh",
        nargs="*",
        metavar="MODEL",
        default=None,
        help="rebuild incremental models from every row (the named ones, or all of them)",
    )
    scheduled = commands.add_parser(
        "schedule",
        help="keep a project's models as fresh as they declare (SIGTERM or Ctrl+C stops)",
    )
    scheduled.add_argument("project", type=Path, help="project folder")
    scheduled.add_argument(
        "--stop-grace",
        type=float,
        default=STOP_GRACE_SECONDS,
        metavar="SECONDS",
        help="after SIGTERM, how long a run in progress may finish before it is cancelled and "
        f"rolled back (default {STOP_GRACE_SECONDS:g}); set the service manager's stop timeout "
        "longer (docs/user/scheduler.md)",
    )
    history = commands.add_parser("runs", help="list recent runs of a project")
    history.add_argument("project", type=Path, help="project folder")
    history.add_argument("--limit", type=int, default=20, help="how many runs (default 20)")
    history.add_argument(
        "--maintenance", action="store_true", help="list lake maintenance instead of model runs"
    )
    maintained = commands.add_parser(
        "maintain",
        help="expire snapshots older than the project's retention and compact the lake's files",
    )
    maintained.add_argument("project", type=Path, help="project folder")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    identity = load_identity()
    given = list(sys.argv[1:] if argv is None else argv)
    if given and given[0] in COMMANDS:
        # `<slug> validate <folder>` and `<slug> run <folder>`; a project folder literally named
        # like a command is opened with `<slug> ./validate`.
        command = command_parser(identity).parse_args(given)
        if command.command == "validate":
            return validate(command.project, sys.stdout, command.show_sql)
        if command.command == "schedule":
            if not (math.isfinite(command.stop_grace) and 0 <= command.stop_grace <= 86_400):
                # `nan` and `inf` parse as floats, but no grace period can be waited out (#166).
                print("--stop-grace must be a number of seconds from 0 to 86400", file=sys.stderr)
                return 2
            return schedule(command.project, identity, sys.stdout, stop_grace=command.stop_grace)
        if command.command == "runs":
            if command.limit < 1:
                print("--limit must be at least 1", file=sys.stderr)
                return 2
            if command.maintenance:
                return history(command.project, identity, command.limit, sys.stdout)
            return runs(command.project, identity, command.limit, sys.stdout)
        if command.command == "maintain":
            return maintain(command.project, identity, sys.stdout)
        return run(command.project, identity, sys.stdout, command.full_refresh)
    arguments = parser_for(identity).parse_args(given)
    try:
        declarations = installed_declarations(identity)
    except ValueError as error:
        print(f"{ContractVersionIncompatible.code}: {error}", file=sys.stderr)
        return 3
    versions = current_versions(identity, declarations)
    if arguments.version:
        print(describe_versions(identity, versions))
        return 0
    try:
        # Before anything else: no project is opened and no port is bound when this fails.
        check_compatibility(declarations)
    except ContractVersionIncompatible as error:
        print(error, file=sys.stderr)
        return 3

    configure_logging()
    registry = ProjectRegistry(instance_directory(identity))
    project_context: AbstractContextManager[Project]
    if arguments.project is None:
        project_context = in_memory_project(identity)
    else:
        try:
            project_context = nullcontext(open_project(arguments.project, registry))
        except ProjectPathError as error:
            print(error, file=sys.stderr)
            return 2

    with project_context as project:
        try:
            port = free_port(arguments.port)
        except OSError as error:
            print(f"{error}; choose another --port", file=sys.stderr)
            return 1
        url = f"http://{LOCAL_HOST}:{port}/"
        server = uvicorn.Server(server_config(port, versions, web_build_directory()))
        with stopping(server) as received:
            print(f"{identity.name}: {project.path} -> {url}", flush=True)
            if not arguments.no_browser:
                threading.Timer(1.0, webbrowser.open, args=(url,)).start()
            server.run()
    # The in-memory project folder is gone; now end as the signal asked, as uvicorn would.
    if received:
        signal.signal(received[0], signal.SIG_DFL)
        signal.raise_signal(received[0])
    return 0


@contextmanager
def stopping(server: uvicorn.Server) -> Iterator[list[int]]:
    """While the server runs, Ctrl-C (`SIGINT`), `SIGTERM` (kill, process managers) and `SIGHUP`
    (the terminal closed) shut it down gracefully; yields the signals received.

    uvicorn raises a signal it handled again once shut down, which by default would end the
    process before the caller's cleanup runs (the in-memory project folder, #43): these handlers
    take that signal instead, and the caller ends the process after cleaning up. They are in
    place before the server's address is announced, so a signal sent once it is printed is
    handled too. Handling `SIGINT` here also keeps asyncio's own handler, and a
    `KeyboardInterrupt` raised at an arbitrary point, out of the way (#151).
    """
    received: list[int] = []

    def stop(signum: int, frame: FrameType | None) -> None:
        received.append(signum)
        server.handle_exit(signum, frame)

    numbers = [
        signal.SIGINT,
        signal.SIGTERM,
        *([signal.SIGHUP] if hasattr(signal, "SIGHUP") else []),
    ]
    previous = {number: signal.signal(number, stop) for number in numbers}
    try:
        yield received
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    sys.exit(main())
