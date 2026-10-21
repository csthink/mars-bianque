"""Static check: the RBAC the Helm chart renders only reads.

Reads the output of ``helm template`` (a multi-document YAML stream) on standard input
and checks every rule of every ``Role`` and ``ClusterRole``:

1. ``verbs`` is not empty and holds only ``get``, ``list`` and ``watch``;
2. ``verbs``, ``resources`` and ``apiGroups`` hold no ``*``;
3. no rule has ``nonResourceURLs``.

It also fails on any ``ClusterRoleBinding`` (read access is granted per namespace, with
RoleBindings) and when the rendering holds no role at all, so that a renamed template
cannot leave the check with nothing to check.

Usage, from ``backend/``::

    helm template ../deploy/helm/bianque | python -m tools.check_rbac

Exit code 0 means compliant; 1 means at least one violation, each printed to stderr.
"""

from __future__ import annotations

import sys
from typing import Any

import yaml

ALLOWED_VERBS = ("get", "list", "watch")
ROLE_KINDS = ("Role", "ClusterRole")


def check_documents(documents: list[Any]) -> list[str]:
    """Return one message per violation in the rendered documents."""
    violations: list[str] = []
    roles = 0
    for document in documents:
        if not isinstance(document, dict):
            continue
        kind = document.get("kind")
        name = (document.get("metadata") or {}).get("name", "?")
        if kind == "ClusterRoleBinding":
            violations.append(f"ClusterRoleBinding {name}: grants a role in every namespace")
        if kind not in ROLE_KINDS:
            continue
        roles += 1
        for index, rule in enumerate(document.get("rules") or [], start=1):
            where = f"{kind} {name} rule {index}"
            violations.extend(f"{where}: {problem}" for problem in _rule_problems(rule))
    if roles == 0:
        violations.append("no Role or ClusterRole rendered")
    return violations


def _rule_problems(rule: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    verbs = rule.get("verbs") or []
    if not verbs:
        problems.append("no verbs")
    for field in ("verbs", "resources", "apiGroups"):
        if "*" in (rule.get(field) or []):
            problems.append(f"wildcard in {field}")
    problems.extend(
        f"verb {verb!r} is not one of {', '.join(ALLOWED_VERBS)}"
        for verb in verbs
        if verb != "*" and verb not in ALLOWED_VERBS
    )
    if rule.get("nonResourceURLs"):
        problems.append("nonResourceURLs are not resources of a namespace")
    return problems


def main() -> int:
    """Check the rendering on standard input."""
    violations = check_documents(list(yaml.safe_load_all(sys.stdin)))
    for violation in violations:
        print(violation, file=sys.stderr)
    if violations:
        print(
            f"{len(violations)} violation(s): Bianque may only get, list and watch", file=sys.stderr
        )
        return 1
    print("RBAC check passed: every rule only gets, lists and watches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
