"""The git command line backend, shared by the real and the replay backend.

The real backend points it at the read-only manifest repository; the replay backend
imports an evidence pack's ``repo.fast-export`` into a temporary repository and points
it there. Only ``log``, ``show`` and ``rev-parse`` can run. Arguments are passed as a
list, never through a shell. Every call ignores the system and global git configuration
and marks the repository as a safe directory with ``-c``, so no configuration file on
the host or in the container is written or read; ``log`` and ``show`` disable external
diff and text conversion drivers.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import get_args

from bianque.tools.backends.base import GitSubcommand
from bianque.tools.errors import ToolError

_ALLOWED: frozenset[str] = frozenset(get_args(GitSubcommand))

#: Messages git prints when a revision or path does not exist.
_NOT_FOUND_MARKERS = (
    "unknown revision",
    "bad object",
    "bad revision",
    "needed a single revision",
    "does not have any commits",
    "ambiguous argument",
)


def git_environment() -> dict[str, str]:
    """Return the complete environment of a git process.

    Nothing is inherited from the Bianque process: no ``GIT_*`` variable can redirect
    git to another repository, and the system and global configuration are hidden.
    """
    return {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }


def git_executable() -> str:
    """Return the absolute path of ``git``; the process environment has no ``PATH``.

    Raises:
        ToolError: ``failed`` when git is not installed.
    """
    path = shutil.which("git")
    if path is None:
        raise ToolError("failed", "git", "git is not installed")
    return path


class GitCli:
    """Runs allowed git subcommands in one repository."""

    def __init__(self, repo: Path) -> None:
        """Bind to the repository at ``repo``."""
        self._repo = repo

    async def run(self, subcommand: GitSubcommand, args: Sequence[str]) -> str:
        """Run ``git <subcommand> <args>`` and return its standard output.

        Raises:
            ToolError: ``failed`` for a subcommand outside the allowed set or a git
                failure, ``not_found`` when a revision does not exist.
        """
        if subcommand not in _ALLOWED:
            raise ToolError("failed", f"git {subcommand}", "subcommand not allowed")
        extra = ["--no-ext-diff", "--no-textconv"] if subcommand in ("log", "show") else []
        command = [
            git_executable(),
            "-c",
            f"safe.directory={self._repo}",
            "--no-pager",
            "-C",
            str(self._repo),
            subcommand,
            *extra,
            *args,
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=git_environment(),
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode != 0:
            message = stderr.decode("utf-8", "replace").strip()
            if any(marker in message for marker in _NOT_FOUND_MARKERS):
                raise ToolError("not_found", "revision", message)
            raise ToolError("failed", f"git {subcommand}", message)
        return stdout.decode("utf-8", "replace")


class MissingRepository:
    """The git source of an evidence pack that has no repository."""

    async def run(self, subcommand: GitSubcommand, args: Sequence[str]) -> str:
        """Fail every call with ``not_found``."""
        raise ToolError("not_found", "repository")
