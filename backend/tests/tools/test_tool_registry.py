"""The registry refuses unsafe or inconsistent tool declarations."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from bianque.tools import ToolRegistrationError, ToolRegistry, registry
from bianque.tools.backends.base import ToolBackends
from bianque.tools.output import ToolOutput

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class _Args(BaseModel):
    namespace: str


async def _noop(args: _Args, backends: ToolBackends) -> ToolOutput:
    """Do nothing."""
    return ToolOutput("{}")


def _declare(**overrides: object) -> None:
    options: dict[str, object] = {
        "name": "probe",
        "kind": "evidence",
        "domain": "kubernetes",
        "source": "kubernetes",
        "read_only": True,
        "timeout_s": 1,
        "args": _Args,
        "identity_args": ("namespace",),
    }
    options.update(overrides)
    ToolRegistry().tool(**options)(_noop)  # type: ignore[arg-type]


def test_a_tool_not_declared_read_only_is_refused() -> None:
    with pytest.raises(ToolRegistrationError, match="not declared read-only"):
        _declare(read_only=False)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"domain": None}, "needs a domain and a source"),
        ({"source": None}, "needs a domain and a source"),
        ({"kind": "auxiliary"}, "must not have a domain or source"),
        ({"identity_args": ("pod",)}, "are not fields"),
        ({"timeout_s": 0}, "timeout must be positive"),
    ],
)
def test_inconsistent_declarations_are_refused(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ToolRegistrationError, match=message):
        _declare(**overrides)


@pytest.mark.parametrize("name", ["Probe", "k8s-probe", "1probe", "p" * 65, "a\x00b", ""])
def test_a_malformed_name_is_refused(name: str) -> None:
    with pytest.raises(ToolRegistrationError, match="does not match"):
        _declare(name=name)


def test_a_name_of_64_characters_is_accepted() -> None:
    _declare(name="p" * 64)


def test_an_auxiliary_tool_has_no_domain() -> None:
    _declare(kind="auxiliary", domain=None, source=None, identity_args=())


def test_a_name_is_registered_once() -> None:
    tools = ToolRegistry()
    tools.tool(name="probe", kind="auxiliary", read_only=True, timeout_s=1, args=_Args)(_noop)
    with pytest.raises(ToolRegistrationError, match="already registered"):
        tools.tool(name="probe", kind="auxiliary", read_only=True, timeout_s=1, args=_Args)(_noop)


def test_importing_a_module_with_a_write_tool_stops_the_process(tmp_path: Path) -> None:
    module = tmp_path / "write_tool.py"
    module.write_text(
        "from pydantic import BaseModel\n"
        "from bianque.tools.registry import tool\n"
        "class Args(BaseModel):\n"
        "    name: str\n"
        "@tool(name='k8s_delete_pod', kind='evidence', domain='kubernetes',\n"
        "      source='kubernetes', read_only=False, timeout_s=5, args=Args)\n"
        "async def k8s_delete_pod(args, backends):\n"
        "    raise AssertionError('never registered')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", "import bianque.tools, write_tool"],
        cwd=BACKEND_ROOT,
        env={"PYTHONPATH": f"{BACKEND_ROOT}:{tmp_path}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "ToolRegistrationError: tool k8s_delete_pod is not declared read-only" in result.stderr


def test_every_registered_tool_is_read_only() -> None:
    tools = list(registry)
    assert [item.name for item in tools] == sorted(
        [
            "change_get_rollout_history",
            "change_list_commits",
            "change_show_commit",
            "investigation_get_report",
            "investigation_list",
            "k8s_get_deployment",
            "k8s_get_pod_status",
            "k8s_list_events",
            "k8s_list_pods",
            "knowledge_search",
            "logs_get_container_logs",
        ]
    )
    assert all(item.read_only is True for item in tools)
    evidence = [item for item in tools if item.kind == "evidence"]
    assert {item.domain for item in evidence} == {"kubernetes", "logs", "change"}
    auxiliary = [item for item in tools if item.kind == "auxiliary"]
    assert [(item.name, item.domain, item.source) for item in auxiliary] == [
        ("investigation_get_report", None, None),
        ("investigation_list", None, None),
        ("knowledge_search", None, None),
    ]


def test_specs_of_one_domain_list_only_its_evidence_tools() -> None:
    names = [spec.name for spec in registry.specs(domain="logs")]
    assert names == ["logs_get_container_logs"]
    spec = registry.specs(domain="change")[0]
    assert spec.parameters["type"] == "object"
    assert "title" not in spec.parameters
    assert spec.description
