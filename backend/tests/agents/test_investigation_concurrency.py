"""Sub-agents of one round run in parallel, and their order never reaches a request key."""

from __future__ import annotations

import asyncio
import random

from investigation_harness import hooked, investigate
from sqlalchemy import Engine

from bianque.llm import ScriptedReply
from bianque.llm.request import ModelRequest, request_key

#: Runs of the request key test, each with its own random delays.
RUNS = 20


async def test_first_round_sub_agents_overlap(migrated: Engine, pg_url: str) -> None:
    """The first calls of both sub-agents wait for each other at a barrier.

    Run one after the other, the first call would wait alone until the timeout and fail.
    """
    barrier = asyncio.Barrier(2)
    waiting = {"list_pods", "read_log"}

    async def meet(method: str) -> None:
        if method in waiting:
            waiting.discard(method)
            async with asyncio.timeout(5):
                await barrier.wait()

    result = await investigate(pg_url, "s1-a", wrap=hooked(meet))

    pods = next(c for c in result.calls if c.name == "k8s_list_pods")
    logs = next(c for c in result.calls if c.name == "logs_get_container_logs")
    assert pods.status == "ok" and logs.status == "ok"
    assert pods.agent == "kubernetes" and logs.agent == "logs"
    assert pods.latency_ms is not None and logs.latency_ms is not None
    pods_end = pods.started_at.timestamp() * 1000 + pods.latency_ms
    logs_end = logs.started_at.timestamp() * 1000 + logs.latency_ms
    assert pods.started_at.timestamp() * 1000 < logs_end
    assert logs.started_at.timestamp() * 1000 < pods_end


async def test_request_keys_do_not_depend_on_completion_order(
    migrated: Engine, pg_url: str
) -> None:
    """Random delays reorder the sub-agents' calls; every request key stays the same."""

    async def run(seed: int | None) -> list[str]:
        keys: list[str] = []
        rng = random.Random(seed)

        def spy(request: ModelRequest) -> ScriptedReply | None:
            keys.append(request_key(request))
            return None

        async def delay(method: str) -> None:
            await asyncio.sleep(rng.uniform(0, 0.02))

        wrap = hooked(delay) if seed is not None else None
        await investigate(pg_url, "s1-a", rules=[spy], wrap=wrap)
        return sorted(keys)

    baseline = await run(None)
    assert len(baseline) > 20
    for seed in range(RUNS):
        assert await run(seed) == baseline, f"request keys changed with seed {seed}"
