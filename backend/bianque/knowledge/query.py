"""The retrieval query of an alert.

The query is the alert name split into words, followed by the annotation values in key
order. Labels other than the alert name are object names (namespace, pod, container),
which say nothing about which runbook applies. The same alert always gives the same
query text, which the embedding fixture looks up by its hash.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def split_words(name: str) -> str:
    """Split a CamelCase or snake_case name into words: ``PodOOMKilled`` → ``Pod OOM Killed``."""
    return " ".join(_CAMEL_RE.sub(" ", name).replace("_", " ").replace("-", " ").split())


def alert_query(labels: Mapping[str, str], annotations: Mapping[str, str]) -> str:
    """Return the retrieval query of an alert from its labels and annotations."""
    parts = [split_words(labels.get("alertname", ""))]
    parts += [str(annotations[key]).strip() for key in sorted(annotations)]
    return "\n\n".join(part for part in parts if part)
