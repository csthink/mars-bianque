"""Tool backends: the real cluster and repository, or a recorded evidence pack."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from bianque.config import Settings
from bianque.tools.backends.base import ToolBackends
from bianque.tools.backends.git import GitCli, MissingRepository
from bianque.tools.backends.pack import open_pack

__all__ = ["ToolBackends", "open_backends", "open_live", "open_pack"]


@asynccontextmanager
async def open_live(settings: Settings) -> AsyncIterator[ToolBackends]:
    """Open the real backends: the Kubernetes API and the manifest repository."""
    from bianque.tools.backends.kubernetes_live import LiveKubernetes

    kubernetes = await LiveKubernetes.connect(context=settings.kube_context)
    git = GitCli(settings.git_repo_path) if settings.git_repo_path else MissingRepository()
    try:
        yield ToolBackends(
            kind="live", kubernetes=kubernetes, git=git, now=lambda: datetime.now(UTC)
        )
    finally:
        await kubernetes.close()


@asynccontextmanager
async def open_backends(
    settings: Settings, *, case: str | None = None
) -> AsyncIterator[ToolBackends]:
    """Open the backends selected by ``BIANQUE_TOOL_BACKEND``.

    Args:
        settings: Process settings.
        case: The evaluation case to replay; ``BIANQUE_PACK_CASE`` when omitted. Only
            used by the ``pack`` backend.
    """
    if settings.tool_backend == "live":
        async with open_live(settings) as backends:
            yield backends
    else:
        async with open_pack(
            settings.cases_dir / (case or settings.pack_case) / "pack"
        ) as backends:
            yield backends
