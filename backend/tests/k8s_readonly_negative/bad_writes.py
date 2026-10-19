"""Negative fixture for tools/check_k8s_readonly.py: every line below must be reported.

This file is never imported; the check parses it.
"""

from kubernetes_asyncio import client


async def violations(core: client.CoreV1Api, api: client.ApiClient, name: str) -> None:
    await core.delete_namespaced_pod("pod", "ns")
    await core.patch_namespaced_pod_status("pod", "ns", {})
    await core.connect_get_namespaced_pod_exec("pod", "ns")
    method = "create_namespaced_pod"
    await getattr(core, method)("ns", {})
    await getattr(core, name)("ns")
    await api.call_api("/api/v1/namespaces", "POST")
    await api.request("POST", "/api/v1/namespaces")
