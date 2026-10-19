"""The executor: permission checks, timeouts and retries, redaction, evidence."""

from __future__ import annotations

import asyncio
import json

import pytest
from tool_fakes import MemorySink, backends, no_sleep

from bianque.redaction import build_engine
from bianque.storage.ids import evidence_id
from bianque.tools import CallerScope, InvestigationContext, ToolError, ToolExecutor, ToolRegistry
from bianque.tools.backends.base import ToolBackends
from bianque.tools.fields import ToolArgs
from bianque.tools.output import ToolOutput

#: A pod name long and random enough for the entropy fallback to mask it.
POD = "s1-app-7d9f8c6b5-x2k4p"
#: A secret of a shape no rule knows; only the entropy fallback catches it.
SECRET = "Zq8" + "Lw2Xk9Vb7Nr4Tp6Ys3Md5"


class _Args(ToolArgs):
    namespace: str
    pod: str = POD
    tail: int = 10


class _Behaviour:
    """What the test tools do on each call, set by each test."""

    def __init__(self) -> None:
        self.calls = 0
        self.errors: list[BaseException] = []
        self.delay = 0.0
        self.text = json.dumps({"pod": POD, "note": f"token-free {SECRET}"})

    async def run(self, args: _Args, backends: ToolBackends) -> ToolOutput:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.errors:
            raise self.errors.pop(0)
        return ToolOutput(self.text, (POD, args.namespace))


@pytest.fixture
def behaviour() -> _Behaviour:
    return _Behaviour()


@pytest.fixture
def tools(behaviour: _Behaviour) -> ToolRegistry:
    registry = ToolRegistry()
    for name, kind, domain in (
        ("k8s_probe", "evidence", "kubernetes"),
        ("logs_probe", "evidence", "logs"),
        ("aux_probe", "auxiliary", None),
    ):

        async def function(args: _Args, backends: ToolBackends) -> ToolOutput:
            """Probe."""
            return await behaviour.run(args, backends)

        registry.tool(
            name=name,
            kind=kind,  # type: ignore[arg-type]
            domain=domain,
            source="kubernetes" if domain else None,
            read_only=True,
            timeout_s=0.05,
            args=_Args,
            identity_args=("namespace", "pod"),
        )(function)
    return registry


def _executor(
    tools: ToolRegistry, sink: MemorySink, delays: list[float] | None = None, **kwargs: object
) -> ToolExecutor:
    async def sleep(delay: float) -> None:
        if delays is not None:
            delays.append(delay)

    return ToolExecutor(
        backends(**kwargs),  # type: ignore[arg-type]
        sink,
        tools=tools,
        engine=build_engine(),
        sleep=sleep if delays is not None else no_sleep,
    )


SUBAGENT = CallerScope(agent="logs", domain="logs")
INVESTIGATION = InvestigationContext("inv_01TEST", step_id=7)


async def test_a_sub_agent_cannot_call_another_domain(
    tools: ToolRegistry, behaviour: _Behaviour
) -> None:
    sink = MemorySink()
    result = await _executor(tools, sink).call(
        "k8s_probe", {"namespace": "demo-s1"}, scope=SUBAGENT, investigation=INVESTIGATION
    )
    assert result.status == "rejected"
    assert result.error == "rejected: k8s_probe is not a tool of the logs domain"
    assert result.model_view.text == result.error
    assert behaviour.calls == 0
    assert [(c.status, c.agent, c.name) for c in sink.calls] == [("rejected", "logs", "k8s_probe")]
    assert sink.evidence == []


async def test_a_sub_agent_cannot_call_an_auxiliary_tool(tools: ToolRegistry) -> None:
    result = await _executor(tools, MemorySink()).call(
        "aux_probe", {"namespace": "demo-s1"}, scope=SUBAGENT
    )
    assert result.status == "rejected"


async def test_an_unknown_tool_is_rejected(tools: ToolRegistry) -> None:
    sink = MemorySink()
    result = await _executor(tools, sink).call("k8s_delete_pod", "{}", scope=SUBAGENT)
    assert (result.status, result.error) == ("rejected", "unknown_tool: k8s_delete_pod")
    assert sink.calls[0].status == "rejected"


@pytest.mark.parametrize(
    "arguments", ["{not json", '{"pod": "x"}', '{"namespace": "a", "extra_field": 1}', "[]"]
)
async def test_invalid_arguments_are_rejected(
    tools: ToolRegistry, behaviour: _Behaviour, arguments: str
) -> None:
    sink = MemorySink()
    result = await _executor(tools, sink).call("logs_probe", arguments, scope=SUBAGENT)
    assert result.status == "rejected"
    assert result.error == "invalid_arguments: logs_probe"
    assert behaviour.calls == 0
    assert sink.calls[0].error is not None and sink.calls[0].error.startswith("invalid_arguments")


async def test_an_mcp_client_is_limited_to_its_tools_and_namespaces(
    tools: ToolRegistry, behaviour: _Behaviour
) -> None:
    scope = CallerScope(
        agent="mcp",
        tools=frozenset({"k8s_probe"}),
        namespaces=frozenset({"demo-s1"}),
        caller="claude-code",
    )
    sink = MemorySink()
    executor = _executor(tools, sink)
    denied_tool = await executor.call("logs_probe", {"namespace": "demo-s1"}, scope=scope)
    denied_ns = await executor.call("k8s_probe", {"namespace": "kube-system"}, scope=scope)
    allowed = await executor.call("k8s_probe", {"namespace": "demo-s1"}, scope=scope)
    assert denied_tool.error == "rejected: logs_probe is not allowed for this client"
    assert denied_ns.error == "rejected: namespace kube-system is not allowed for this client"
    assert allowed.status == "ok"
    assert behaviour.calls == 1
    assert [c.caller for c in sink.calls] == ["claude-code"] * 3


async def test_a_timeout_is_retried_twice_then_reported_without_timings(
    tools: ToolRegistry, behaviour: _Behaviour
) -> None:
    behaviour.delay = 1.0
    delays: list[float] = []
    sink = MemorySink()
    result = await _executor(tools, sink, delays).call(
        "logs_probe", {"namespace": "demo-s1"}, scope=SUBAGENT, investigation=INVESTIGATION
    )
    assert result.status == "error"
    assert result.error == f"timeout: logs_probe namespace=demo-s1 pod={POD}"
    assert result.model_view.text == result.error
    assert behaviour.calls == 3
    assert delays == [0.5, 1.0]
    call = sink.calls[0]
    assert (call.status, call.retries) == ("error", 2)
    assert sink.evidence == []


async def test_a_connection_error_is_retried_and_can_succeed(
    tools: ToolRegistry, behaviour: _Behaviour
) -> None:
    behaviour.errors = [ToolError("connection", "pods demo-s1", "ClientConnectorError")]
    sink = MemorySink()
    result = await _executor(tools, sink).call(
        "logs_probe", {"namespace": "demo-s1"}, scope=SUBAGENT
    )
    assert result.status == "ok"
    assert behaviour.calls == 2
    assert sink.calls[0].retries == 1


@pytest.mark.parametrize(
    "error",
    [
        ToolError("not_found", f"pod demo-s1/{POD}"),
        ToolError("forbidden", "pods demo-s1"),
        ValueError("broken"),
    ],
)
async def test_final_failures_are_not_retried(
    tools: ToolRegistry, behaviour: _Behaviour, error: BaseException
) -> None:
    behaviour.errors = [error]
    sink = MemorySink()
    result = await _executor(tools, sink).call(
        "logs_probe", {"namespace": "demo-s1"}, scope=SUBAGENT
    )
    assert result.status == "error"
    assert behaviour.calls == 1
    assert sink.calls[0].retries == 0
    if isinstance(error, ToolError):
        assert result.error == error.model_text
        # The pod name in the error stays readable for the model.
        assert result.model_view.text == error.model_text
    else:
        assert result.error == f"failed: logs_probe namespace=demo-s1 pod={POD}"
        assert sink.calls[0].error is not None and "ValueError: broken" in sink.calls[0].error


async def test_an_injected_fault_takes_the_timeout_path(
    tools: ToolRegistry, behaviour: _Behaviour
) -> None:
    sink = MemorySink()
    executor = _executor(tools, sink, faults={"logs_probe": "timeout"})
    result = await executor.call("logs_probe", {"namespace": "demo-s1"}, scope=SUBAGENT)
    assert result.error == f"timeout: logs_probe namespace=demo-s1 pod={POD}"
    assert behaviour.calls == 0
    assert sink.calls[0].retries == 2


async def test_an_investigation_call_stores_evidence_with_a_content_id(
    tools: ToolRegistry, behaviour: _Behaviour
) -> None:
    sink = MemorySink()
    executor = _executor(tools, sink)
    first = await executor.call(
        "logs_probe", {"namespace": "demo-s1"}, scope=SUBAGENT, investigation=INVESTIGATION
    )
    again = await executor.call(
        "logs_probe",
        {"namespace": "demo-s1", "tail": 50},
        scope=SUBAGENT,
        investigation=INVESTIGATION,
    )
    assert first.status == "ok"
    expected = evidence_id(
        "logs_probe", {"namespace": "demo-s1", "pod": POD}, first.model_view.text
    )
    # tail is not an identity argument, so the same content keeps the same ID.
    assert first.evidence_ids == again.evidence_ids == (expected,)
    stored = sink.evidence[0]
    assert (stored.investigation_id, stored.step_id, stored.source, stored.tool) == (
        "inv_01TEST",
        7,
        "kubernetes",
        "logs_probe",
    )
    assert stored.raw_content == behaviour.text
    assert stored.redacted_content == first.model_view.text
    assert stored.meta == {
        "arguments": {"namespace": "demo-s1", "pod": POD, "tail": 10},
        "backend": "pack",
    }
    assert POD in stored.identifiers
    assert sink.calls[0].investigation_id == "inv_01TEST"


async def test_calls_without_an_investigation_store_no_evidence(tools: ToolRegistry) -> None:
    sink = MemorySink()
    executor = _executor(tools, sink)
    mcp = CallerScope(agent="mcp", caller="claude-code")
    assert (await executor.call("k8s_probe", {"namespace": "demo-s1"}, scope=mcp)).status == "ok"
    aux = await executor.call(
        "aux_probe", {"namespace": "demo-s1"}, scope=mcp, investigation=INVESTIGATION
    )
    assert aux.status == "ok" and aux.evidence_ids == ()
    assert sink.evidence == []
    assert len(sink.calls) == 2


async def test_redaction_keeps_identifiers_and_masks_secrets(tools: ToolRegistry) -> None:
    sink = MemorySink()
    result = await _executor(tools, sink).call(
        "logs_probe", {"namespace": "demo-s1"}, scope=SUBAGENT
    )
    assert POD in result.model_view.text
    assert SECRET not in result.model_view.text
    assert "<high-entropy:" in result.model_view.text
    assert POD in result.model_view.preserved
    assert SECRET not in (sink.calls[0].output_summary or "")
    assert POD in sink.calls[0].input_summary
