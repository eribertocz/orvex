"""Who and what a CLI command or a scheduled run acts as: the `CallContext` of its contract calls,
built from real values only.

- The actor is the operating-system user running the process (the local Builder, or the one who
  started the scheduler).
- The project is its folder's absolute path.
- The revision is the project's files as they are now: `git:<commit>` when the folder is a Git
  checkout whose project files match that commit, `git:<commit>+files:<digest>` when they
  differ from it, and `files:<digest>` without Git. The digest is a SHA-256 of every project
  file a check reads, by path and content.

A value that cannot be found is refused (`CallContext`: CONTRACT_CONTEXT_INVALID), never
replaced by a stand-in.
"""

from __future__ import annotations

import getpass
import hashlib
from pathlib import Path

from orvex.contracts.context import CallContext
from orvex.server.call_context import command_context
from orvex.server.project_files import project_files
from orvex.server.run_metadata import _project_commit


def project_id(folder: Path) -> str:
    return str(Path(folder).expanduser().absolute())


def files_digest(folder: Path) -> str:
    """A SHA-256 of the project files a check reads, by relative path and content."""
    root = Path(folder).expanduser().absolute()
    digest = hashlib.sha256()
    for path in project_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big") + relative)
        digest.update(len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def project_revision(folder: Path) -> str:
    commit, dirty = _project_commit(Path(folder).expanduser().absolute())
    if commit is None:
        return f"files:{files_digest(folder)}"
    if dirty is False:
        return f"git:{commit}"
    return f"git:{commit}+files:{files_digest(folder)}"


def project_context(folder: Path) -> CallContext:
    """The context of one command's contract calls on the project in `folder`."""
    return command_context(getpass.getuser(), project_id(folder), project_revision(folder))
