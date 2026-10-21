"""A case investigated again in ``recorded`` mode makes the same model requests.

The recordings are looked up by request key, and a second investigation of a case
runs in a database that already holds the first one. Nothing of the first investigation
may reach a prompt of the second, or the second misses a recording and fails. The
investigations use the settings of ``make eval``: the evidence pack backend, the
committed recordings and their manifest, and the embedding fixture.
"""

from __future__ import annotations

from collections import Counter

import pytest
from investigation_harness import CASES, EMBEDDING_FIXTURE, RUNBOOKS, database, no_wait
from sqlalchemy import Engine

from bianque.agents.events import EventNotifier
from bianque.agents.service import InvestigationService
from bianque.agents.startup import load_skill_catalog
from bianque.config import Settings
from bianque.config.settings import with_recorded_model
from bianque.knowledge.embedding import FixtureEmbedder
from bianque.knowledge.ingest import ingest
from bianque.llm.providers.recorded import RecordedProvider
from bianque.llm.request import ModelRequest, request_key
from bianque.llm.types import LLMCompletion
from bianque.storage.repositories import InvestigationRepository

RECORDINGS = CASES.parent / "recordings"


def recorded_settings(pg_url: str) -> Settings:
    """The settings of the replay evaluation, on the test database."""
    return with_recorded_model(
        Settings(
            database_url=pg_url,
            llm_mode="recorded",
            llm_recordings_dir=RECORDINGS,
            tool_backend="pack",
            cases_dir=CASES,
            knowledge_dir=RUNBOOKS,
            embedding_mode="fixture",
            embedding_fixture=EMBEDDING_FIXTURE,
        )
    )


async def test_a_case_investigated_twice_hits_every_recording(
    migrated: Engine, pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys: list[str] = []
    complete = RecordedProvider.complete

    async def keyed(self: RecordedProvider, request: ModelRequest) -> LLMCompletion:
        keys.append(request_key(request))
        return await complete(self, request)

    monkeypatch.setattr(RecordedProvider, "complete", keyed)
    settings = recorded_settings(pg_url)
    runs: list[Counter[str]] = []
    async with database(pg_url) as sessions:
        await ingest(RUNBOOKS, sessions=sessions, embedder=FixtureEmbedder(EMBEDDING_FIXTURE))
        service = InvestigationService(
            settings,
            sessions,
            EventNotifier(),
            skills=load_skill_catalog(settings),
            tool_sleep=no_wait,
            model_sleep=no_wait,
        )
        for _ in range(2):
            keys.clear()
            investigation_id = await service.replay_case("s1-a")
            await service.wait(investigation_id)
            async with sessions() as session:
                investigation = await InvestigationRepository(session).get(investigation_id)
            assert (investigation.status, investigation.error) == ("reported", None)
            runs.append(Counter(keys))

    first, second = runs
    assert first and first == second
