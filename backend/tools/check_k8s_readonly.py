"""Static check: Bianque calls only ``read_`` and ``list_`` methods of the Kubernetes client.

The allowed set is a whitelist. The method names of every API class in
``kubernetes_asyncio.client`` are taken from the installed library, so the check
follows the library version. Every name outside the whitelist (``create_``, ``patch_``,
``replace_``, ``delete_``, ``connect_`` and the rest) is forbidden. The check parses the
scanned paths and reports:

1. any attribute access to a forbidden API method name, anywhere;
2. any string constant equal to a forbidden API method name (``getattr`` by name);
3. ``call_api``, the client's generic request method, anywhere;
4. in modules that import ``kubernetes_asyncio``: the attribute ``request`` (the HTTP
   layer below the API classes) and ``getattr`` with a computed name.

Usage, from ``backend/``::

    python -m tools.check_k8s_readonly [PATH ...]

Without arguments it scans the ``bianque`` package. Exit code 0 means compliant; 1
means at least one violation, each printed to stderr.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

ALLOWED_PREFIXES = ("read_", "list_")


def forbidden_api_methods() -> frozenset[str]:
    """Return every public API method name of the Kubernetes client outside the whitelist."""
    from kubernetes_asyncio import client

    names: set[str] = set()
    for class_name, cls in inspect.getmembers(client, inspect.isclass):
        if not class_name.endswith("Api") or not cls.__module__.startswith(
            "kubernetes_asyncio.client.api"
        ):
            continue
        names.update(
            name
            for name, _ in inspect.getmembers(cls, inspect.isfunction)
            if not name.startswith("_")
        )
    return frozenset(name for name in names if not name.startswith(ALLOWED_PREFIXES))


def _imports_client(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name.split(".")[0] == "kubernetes_asyncio" for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == (
            "kubernetes_asyncio"
        ):
            return True
    return False


def check_tree(tree: ast.AST, forbidden: frozenset[str]) -> list[tuple[int, str]]:
    """Return ``(line, message)`` for every violation in one module."""
    client_module = _imports_client(tree)
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in forbidden:
                violations.append((node.lineno, f"calls Kubernetes API method {node.attr!r}"))
            elif node.attr == "call_api":
                violations.append((node.lineno, "calls the generic client method 'call_api'"))
            elif client_module and node.attr == "request":
                violations.append((node.lineno, "uses the client's HTTP layer 'request'"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in forbidden:
                violations.append((node.lineno, f"names Kubernetes API method {node.value!r}"))
        elif (
            client_module
            and isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and not isinstance(node.args[1], ast.Constant)
        ):
            violations.append((node.lineno, "uses getattr with a computed name"))
    return violations


def _iter_py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return files


def main(argv: list[str]) -> int:
    """Scan the given paths (default: the ``bianque`` package) and report violations."""
    roots = [Path(arg) for arg in argv] or [Path("bianque")]
    forbidden = forbidden_api_methods()
    failures = 0
    for path in _iter_py_files(roots):
        tree = ast.parse(path.read_text(), filename=str(path))
        for line, message in check_tree(tree, forbidden):
            print(f"{path}:{line}: {message}", file=sys.stderr)
            failures += 1
    if failures:
        print(
            f"{failures} violation(s): Bianque may only call read_ and list_ methods of the "
            "Kubernetes client",
            file=sys.stderr,
        )
        return 1
    print(f"Kubernetes client read-only check passed ({len(forbidden)} methods forbidden)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
