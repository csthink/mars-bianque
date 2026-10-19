"""The replay backend needs no cluster and no network.

The Kubernetes client and every outbound socket connection are replaced with doubles
that fail as soon as they are used; the tool calls behind S1-a's required evidence still
return normally from the evidence pack.
"""

from __future__ import annotations

import json
import socket

import pytest
from kubernetes_asyncio import client
from s1_evidence import check_required_evidence
from tool_fakes import CASES

from bianque.tools.backends.pack import open_pack


async def test_replay_never_touches_the_cluster_or_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched: list[str] = []

    def fail(name: str) -> object:
        def _fail(*args: object, **kwargs: object) -> object:
            touched.append(name)
            raise AssertionError(f"{name} used during replay")

        return _fail

    monkeypatch.setattr(client.ApiClient, "__init__", fail("ApiClient"))
    monkeypatch.setattr(client.ApiClient, "call_api", fail("call_api"))
    monkeypatch.setattr(socket.socket, "connect", fail("socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", fail("socket.connect_ex"))

    pack = CASES / "s1-a" / "pack"
    alert = json.loads((pack / "alert.json").read_text())
    async with open_pack(pack) as backends:
        await check_required_evidence(backends, "a", alert)
    assert touched == []
