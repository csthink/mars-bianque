"""The real Kubernetes backend.

This is the only module that imports ``kubernetes_asyncio`` (an import-linter contract
enforces it). It calls only ``read_`` and ``list_`` methods of the API classes;
``tools/check_k8s_readonly.py`` fails the build when any other API method name appears
in the package. Results are serialized into the API's JSON form, the same form an
evidence pack stores.
"""

from __future__ import annotations

from typing import Any

import aiohttp
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException

from bianque.tools.backends.base import KubeObject
from bianque.tools.errors import ToolError

#: Upper bound on the bytes of one container log read from the API.
LOG_LIMIT_BYTES = 1024 * 1024


def _error(exc: Exception, target: str) -> ToolError:
    if isinstance(exc, ApiException):
        status = exc.status or 0
        if status == 404:
            return ToolError("not_found", target, str(exc.reason))
        if status == 403:
            return ToolError("forbidden", target, str(exc.reason))
        if status == 429:
            return ToolError("throttled", target, str(exc.reason))
        if status >= 500:
            return ToolError("server_error", target, f"HTTP {status}")
        return ToolError("failed", target, f"HTTP {status}: {exc.reason}")
    if isinstance(exc, aiohttp.ClientConnectionError | ConnectionError):
        return ToolError("connection", target, type(exc).__name__)
    return ToolError("failed", target, f"{type(exc).__name__}: {exc}")


class LiveKubernetes:
    """Reads a cluster through the Kubernetes API."""

    def __init__(self, api_client: client.ApiClient) -> None:
        """Wrap an API client; the caller owns its lifetime."""
        self._client = api_client
        self._core = client.CoreV1Api(api_client)
        self._apps = client.AppsV1Api(api_client)

    @classmethod
    async def connect(cls, *, context: str | None) -> LiveKubernetes:
        """Load the configuration and return a connected backend.

        Inside a cluster with no ``context`` given, the pod's service account is used;
        otherwise the kubeconfig, with ``context`` when given, so that the current
        context of the host never decides which cluster is read.
        """
        configuration = client.Configuration()
        if context is None:
            try:
                # The library leaves this function unannotated.
                config.load_incluster_config(client_configuration=configuration)  # type: ignore[no-untyped-call]
            except config.ConfigException:
                await config.load_kube_config(client_configuration=configuration)
        else:
            await config.load_kube_config(context=context, client_configuration=configuration)
        return cls(client.ApiClient(configuration))

    async def close(self) -> None:
        """Close the HTTP session."""
        await self._client.close()

    def _plain(self, value: Any) -> Any:
        return self._client.sanitize_for_serialization(value)

    def _items(self, value: Any) -> list[KubeObject]:
        items: list[KubeObject] = self._plain(value).get("items") or []
        return items

    async def list_pods(self, namespace: str, label_selector: str | None) -> list[KubeObject]:
        """Return the pods of a namespace."""
        try:
            if label_selector:
                result = await self._core.list_namespaced_pod(
                    namespace, label_selector=label_selector
                )
            else:
                result = await self._core.list_namespaced_pod(namespace)
        except Exception as exc:
            raise _error(exc, f"pods {namespace}") from exc
        return self._items(result)

    async def get_pod(self, namespace: str, name: str) -> KubeObject:
        """Return one pod."""
        try:
            pod: KubeObject = self._plain(await self._core.read_namespaced_pod(name, namespace))
        except Exception as exc:
            raise _error(exc, f"pod {namespace}/{name}") from exc
        return pod

    async def list_events(self, namespace: str) -> list[KubeObject]:
        """Return all events of a namespace."""
        try:
            result = await self._core.list_namespaced_event(namespace)
        except Exception as exc:
            raise _error(exc, f"events {namespace}") from exc
        return self._items(result)

    async def get_deployment(self, namespace: str, name: str) -> KubeObject:
        """Return one deployment."""
        try:
            deployment: KubeObject = self._plain(
                await self._apps.read_namespaced_deployment(name, namespace)
            )
        except Exception as exc:
            raise _error(exc, f"deployment {namespace}/{name}") from exc
        return deployment

    async def list_deployments(self, namespace: str) -> list[KubeObject]:
        """Return all deployments of a namespace."""
        try:
            result = await self._apps.list_namespaced_deployment(namespace)
        except Exception as exc:
            raise _error(exc, f"deployments {namespace}") from exc
        return self._items(result)

    async def list_replicasets(self, namespace: str) -> list[KubeObject]:
        """Return all replica sets of a namespace."""
        try:
            result = await self._apps.list_namespaced_replica_set(namespace)
        except Exception as exc:
            raise _error(exc, f"replicasets {namespace}") from exc
        return self._items(result)

    async def read_log(self, namespace: str, pod: str, container: str, previous: bool) -> str:
        """Return a container's log; a missing previous container is ``not_found``."""
        target = f"log {namespace}/{pod}/{container}" + (" previous" if previous else "")
        try:
            text: str = await self._core.read_namespaced_pod_log(
                pod,
                namespace,
                container=container,
                previous=previous,
                limit_bytes=LOG_LIMIT_BYTES,
            )
        except ApiException as exc:
            # The API answers 400 when the container has no previous instance or has
            # not started yet; to the caller that log does not exist.
            if exc.status == 400:
                raise ToolError("not_found", target, str(exc.reason)) from exc
            raise _error(exc, target) from exc
        except Exception as exc:
            raise _error(exc, target) from exc
        return text
