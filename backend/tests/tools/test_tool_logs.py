"""Logs enter the model folded and cut to a bounded size."""

from __future__ import annotations

import json

from tool_fakes import FakeKubernetes, MemorySink, backends

from bianque.redaction import build_engine
from bianque.tools import CallerScope, ToolExecutor
from bianque.tools.logs import LINE_MAX_CHARS, LOG_MAX_LINES, fold_and_truncate


def test_consecutive_repeats_are_folded_with_their_count() -> None:
    lines, total, truncated = fold_and_truncate("start\nretry\nretry\nretry\nstop\nretry\n", 10)
    assert lines == ["start", "retry [repeated 3 times]", "stop", "retry"]
    assert (total, truncated) == (6, False)


def test_only_the_last_lines_are_kept() -> None:
    text = "\n".join(f"line {i}" for i in range(500))
    lines, total, truncated = fold_and_truncate(text, 200)
    assert lines[0] == "line 300" and lines[-1] == "line 499"
    assert (len(lines), total, truncated) == (200, 500, True)


def test_long_lines_are_cut() -> None:
    lines, _, _ = fold_and_truncate("x" * 5000, 10)
    assert lines == ["x" * LINE_MAX_CHARS + " [line truncated]"]


async def test_an_oversized_log_reaches_the_model_within_the_limit() -> None:
    log = "".join(f"request {i} ok\n" for i in range(3000)) + "panic\n" * 40 + "exit 2\n"
    kubernetes = FakeKubernetes(logs={("web-0", "app", True): log})
    executor = ToolExecutor(backends(kubernetes), MemorySink(), engine=build_engine())
    result = await executor.call(
        "logs_get_container_logs",
        {"namespace": "demo-s1", "pod": "web-0", "container": "app", "previous": True},
        scope=CallerScope(agent="logs", domain="logs"),
    )
    data = json.loads(result.model_view.text)
    assert len(data["lines"]) == LOG_MAX_LINES
    assert data["lines"][-2:] == ["panic [repeated 40 times]", "exit 2"]
    assert (data["total_lines"], data["truncated"]) == (3041, True)
    assert len(result.model_view.text) < 10_000
