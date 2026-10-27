"""Command line: `<slug>` opens an in-memory project, `<slug> <path>` opens or creates a project."""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import webbrowser
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from importlib.resources import files
from pathlib import Path

import uvicorn
from sextant.server.app import create_app
from sextant.server.identity import Identity, load_identity
from sextant.server.instance import (
    Project,
    ProjectPathError,
    ProjectRegistry,
    in_memory_project,
    instance_directory,
    open_project,
)
from sextant.server.logs import configure_logging
from sextant.server.versions import Versions, current_versions

LOCAL_HOST = "127.0.0.1"
DEFAULT_PORT = 7878
PORT_ATTEMPTS = 100
log = logging.getLogger("sextant.server")


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
    packaged = Path(str(files("sextant.server").joinpath("web")))
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


def main(argv: Sequence[str] | None = None) -> int:
    identity = load_identity()
    arguments = parser_for(identity).parse_args(argv)
    versions = current_versions(identity)
    if arguments.version:
        print(describe_versions(identity, versions))
        return 0

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
