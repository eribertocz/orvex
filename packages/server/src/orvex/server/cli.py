"""Command line: `<slug>` opens an in-memory project, `<slug> <path>` opens or creates a project."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from importlib.resources import files
from pathlib import Path

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
from orvex.server.versions import (
    ContractVersionIncompatible,
    Versions,
    check_compatibility,
    current_versions,
    installed_declarations,
)

LOCAL_HOST = "127.0.0.1"
COMMANDS = frozenset({"validate", "run", "runs"})
DEFAULT_PORT = 7878
PORT_ATTEMPTS = 100


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
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="first port to try")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--version", action="store_true", help="print versions and exit")
    return parser


def free_port(first: int, host: str = LOCAL_HOST, attempts: int = PORT_ATTEMPTS) -> int:
    """The first port from `first` on that can be bound on `host`."""
    for port in range(first, first + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind((host, port))
            except OSError:
                continue
            return port
    raise OSError(f"no free port in {first}..{first + attempts - 1}")


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
        create_app(versions, web_build),
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
    run = commands.add_parser("run", help="build every model of a project into its local lake")
    run.add_argument("project", type=Path, help="project folder")
    history = commands.add_parser("runs", help="list recent runs of a project")
    history.add_argument("project", type=Path, help="project folder")
    history.add_argument("--limit", type=int, default=20, help="how many runs (default 20)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    identity = load_identity()
    given = list(sys.argv[1:] if argv is None else argv)
    if given and given[0] in COMMANDS:
        # `<slug> validate <folder>` and `<slug> run <folder>`; a project folder literally named
        # like a command is opened with `<slug> ./validate`.
        command = command_parser(identity).parse_args(given)
        if command.command == "validate":
            return validate(command.project, sys.stdout)
        if command.command == "runs":
            return runs(command.project, identity, max(1, command.limit), sys.stdout)
        return run(command.project, identity, sys.stdout)
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
        port = free_port(arguments.port)
        url = f"http://{LOCAL_HOST}:{port}/"
        print(f"{identity.name}: {project.path} -> {url}", flush=True)
        server = uvicorn.Server(server_config(port, versions, web_build_directory()))
        if not arguments.no_browser:
            threading.Timer(1.0, webbrowser.open, args=(url,)).start()
        server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
