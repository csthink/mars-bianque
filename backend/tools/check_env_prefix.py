"""Static check: every environment variable Bianque reads carries the ``BIANQUE_`` prefix.

The check is coupled to the conventions of ``bianque/config/settings.py``: settings are
read through pydantic-settings with ``env_prefix``, and the environment is read only in
the config package. The check therefore:

1. asserts that ``ENV_PREFIX == "BIANQUE_"`` in the config package;
2. parses the scanned paths and requires every direct read of the process environment
   with a literal name (``os.environ[...]``, ``os.environ.get(...)``, ``os.getenv(...)``)
   to use a ``BIANQUE_``-prefixed name. This catches code that bypasses ``env_prefix``
   and reads an unprefixed variable directly (the negative fixture does exactly that).

Usage, from ``backend/``::

    python -m tools.check_env_prefix [PATH ...]

Without arguments it scans ``bianque/config``. Exit code 0 means compliant; 1 means at
least one violation, each printed to stderr.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PREFIX = "BIANQUE_"


def _iter_py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return files


def _literal_env_names(tree: ast.AST) -> list[tuple[int, str]]:
    """Collect the literal names of directly read environment variables, with line numbers."""
    names: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        # os.getenv("X") / os.environ.get("X")
        if isinstance(node, ast.Call):
            func = node.func
            key: ast.expr | None = None
            if isinstance(func, ast.Attribute) and func.attr == "getenv" and node.args:
                key = node.args[0]
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
                and node.args
            ):
                key = node.args[0]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.append((node.lineno, key.value))
        # os.environ["X"]
        elif isinstance(node, ast.Subscript):
            val = node.value
            if isinstance(val, ast.Attribute) and val.attr == "environ":
                sl = node.slice
                if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                    names.append((node.lineno, sl.value))

    return names


def check_paths(roots: list[Path]) -> list[str]:
    """Return one message per violation found under ``roots`` (empty means compliant)."""
    problems: list[str] = []
    for path in _iter_py_files(roots):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - syntax errors are reported by lint
            problems.append(f"{path}: cannot parse: {exc}")
            continue
        for lineno, name in _literal_env_names(tree):
            if not name.startswith(PREFIX):
                problems.append(
                    f"{path}:{lineno}: environment variable {name!r} lacks the {PREFIX!r} prefix"
                )
    return problems


def check_prefix_constant(config_pkg: Path) -> list[str]:
    """Assert that ``ENV_PREFIX`` in the config package is ``BIANQUE_``."""
    settings_py = config_pkg / "settings.py"
    if not settings_py.exists():
        return [f"{settings_py}: missing; cannot verify the ENV_PREFIX constant"]
    tree = ast.parse(settings_py.read_text(encoding="utf-8"), filename=str(settings_py))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "ENV_PREFIX"
                    and isinstance(node.value, ast.Constant)
                ):
                    if node.value.value != PREFIX:
                        return [
                            f"{settings_py}: ENV_PREFIX = {node.value.value!r}, expected {PREFIX!r}"
                        ]
                    return []
    return [f"{settings_py}: no assignment to the ENV_PREFIX constant found"]


def main(argv: list[str]) -> int:
    """Check the ``ENV_PREFIX`` constant and the environment reads under the given paths.

    Args:
        argv: Command line; ``argv[1:]`` are the paths to scan, defaulting to the config
            package.

    Returns:
        Process exit code: 0 when compliant, 1 when a problem was found (each printed
        to stderr).
    """
    backend_root = Path(__file__).resolve().parent.parent
    config_pkg = backend_root / "bianque" / "config"
    roots = [Path(a) for a in argv[1:]] or [config_pkg]

    problems = check_prefix_constant(config_pkg)
    problems += check_paths(roots)

    if problems:
        print("check-env-prefix FAIL:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    scanned = ", ".join(
        str(r.resolve().relative_to(backend_root))
        if r.resolve().is_relative_to(backend_root)
        else str(r)
        for r in roots
    )
    print(
        f"check-env-prefix OK: ENV_PREFIX is {PREFIX!r} and no unprefixed environment "
        f"read was found in {scanned}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
