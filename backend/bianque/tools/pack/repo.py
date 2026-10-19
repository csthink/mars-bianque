"""Moving the manifest repository in and out of an evidence pack.

Recording exports the repository with ``git fast-export``; replay imports it with
``git fast-import`` into a temporary bare repository. These are setup steps outside the
tool executor and outside the tools' subcommand allow-list (``log``, ``show``,
``rev-parse``); no tool can reach them. Commits are exported without signatures and
re-imported with the same authors, dates and contents, so their hashes are unchanged.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from bianque.tools.backends.git import git_environment, git_executable
from bianque.tools.pack.format import PackError


async def _git(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    process = await asyncio.create_subprocess_exec(
        git_executable(),
        "-c",
        f"safe.directory={repo}",
        "-C",
        str(repo),
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=git_environment(),
    )
    stdout, stderr = await process.communicate(stdin)
    if process.returncode != 0:
        message = stderr.decode("utf-8", "replace").strip()
        raise PackError(f"git {args[0]} in {repo} failed: {message}")
    return stdout.decode("utf-8", "replace")


async def export_repository(repo: Path) -> tuple[str, str]:
    """Return the fast-export text of every ref and the symbolic ref of ``HEAD``."""
    head = (await _git(repo, "symbolic-ref", "HEAD")).strip()
    text = await _git(repo, "fast-export", "--all", "--signed-tags=strip")
    return text, head


async def import_repository(text: str, head: str | None, target: Path) -> None:
    """Create a bare repository at ``target`` from fast-export text."""
    target.mkdir(parents=True, exist_ok=True)
    await _git(target, "init", "--quiet", "--bare")
    await _git(target, "fast-import", "--quiet", stdin=text.encode("utf-8"))
    if head:
        await _git(target, "symbolic-ref", "HEAD", head)
