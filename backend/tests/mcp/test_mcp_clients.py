"""MCP client configuration: validation, tool pattern expansion and token matching."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bianque.config import Settings
from bianque.config.settings import MCPClient
from bianque.mcp.clients import (
    ClientConfigError,
    expand_tools,
    find_client,
    resolve_clients,
    token_sha256,
)
from bianque.tools import registry

HASH = token_sha256("token-a")


def entry(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "name": "claude-code",
        "token_sha256": HASH,
        "tools": ["*"],
        "namespaces": ["demo-s1"],
    }
    values.update(overrides)
    return values


def test_clients_are_read_from_json_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "BIANQUE_MCP_CLIENTS",
        f'[{{"name": "a", "token_sha256": "{HASH}", "tools": ["k8s_*"],'
        ' "namespaces": ["demo-s1"]}]',
    )
    [client] = Settings().mcp_clients
    assert (client.name, client.tools, client.namespaces) == ("a", ("k8s_*",), ("demo-s1",))
    monkeypatch.setenv("BIANQUE_MCP_CLIENTS", "")
    assert Settings().mcp_clients == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"token_sha256": "not-a-hash"},
        {"token_sha256": HASH.upper()},
        {"tools": []},
        {"namespaces": []},
        {"namespaces": ["Not_A_Namespace"]},
        {"name": "Has Space"},
        {"token": "plain"},
    ],
)
def test_a_malformed_client_is_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MCPClient.model_validate(entry(**overrides))


def test_two_clients_cannot_share_a_name_or_a_token() -> None:
    other = token_sha256("token-b")
    with pytest.raises(ValidationError, match="share a name"):
        Settings.model_validate({"mcp_clients": [entry(), entry(token_sha256=other)]})
    with pytest.raises(ValidationError, match="share a token_sha256"):
        Settings.model_validate({"mcp_clients": [entry(), entry(name="other")]})


def test_tool_patterns_expand_against_the_registry() -> None:
    names = [definition.name for definition in registry]
    assert expand_tools(["k8s_*"], names) == {n for n in names if n.startswith("k8s_")}
    assert expand_tools(["*"], names) == set(names)
    assert expand_tools(["knowledge_search", "change_show_commit"], names) == {
        "knowledge_search",
        "change_show_commit",
    }
    with pytest.raises(ClientConfigError, match="'kubectl_\\*' matches no tool"):
        expand_tools(["k8s_*", "kubectl_*"], names)


def test_a_client_whose_pattern_matches_nothing_stops_the_start() -> None:
    clients = [MCPClient.model_validate(entry(tools=["k8s_*", "kubectl_delete"]))]
    with pytest.raises(ClientConfigError, match="MCP client claude-code"):
        resolve_clients(clients, registry)


def test_a_token_finds_its_client_only() -> None:
    clients = resolve_clients(
        [
            MCPClient.model_validate(entry()),
            MCPClient.model_validate(entry(name="b", token_sha256=token_sha256("token-b"))),
        ],
        registry,
    )
    found = find_client(clients, "token-b")
    assert found is not None and found.name == "b"
    assert find_client(clients, "token-c") is None
    scope = found.scope()
    assert (scope.agent, scope.caller, scope.namespaces) == ("mcp", "b", frozenset({"demo-s1"}))
    assert scope.tools == frozenset(definition.name for definition in registry)
