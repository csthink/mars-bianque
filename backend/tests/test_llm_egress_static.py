"""Static rules for the single model egress and the single settings entry point.

The test parses every module under ``bianque/`` and checks four rules, plus three tests
that prove the scanner itself detects known violations. It has to be a static scan
rather than an import-linter contract: a forbidden contract cannot fail when the
forbidden module is a descendant of the source module. ``bianque.llm.providers`` is a
descendant of ``bianque``, so a contract with source ``bianque`` or ``bianque.llm`` is
always kept. Making it work would require listing every non-ancestor module as a
source, and such a list silently goes stale as packages are added. This scan walks the
real source tree with no list, so new modules are covered automatically.

Four rules:

1. Outside ``bianque/llm/providers/**``, nothing imports the model SDK (``openai``).
2. Outside ``bianque/llm/gateway.py`` and ``bianque/llm/providers/**``, nothing imports
   ``bianque.llm.providers`` or any of its descendants (a single egress).
3. Outside ``bianque/llm/**``, nothing reads the model endpoint or credential settings.
4. Outside ``bianque/config/**``, nothing reads the process environment directly.

Two details carry the weight; without either, the most natural code bypasses a rule:

- Rule 2 resolves **relative imports** to absolute module names against the file's
  package first. ``from .providers import base`` in ``bianque/llm/redacted.py`` is the
  same import as the absolute form, and literal matching would miss it.
- Rule 4 resolves the **import forms** of ``os`` back to absolute names first
  (``import os as x``, ``from os import getenv``, ``from os import environ``, with
  ``as`` aliases). ``from os import *`` cannot be resolved and always counts as a
  violation (fail closed). Matching only the literal prefix ``os.`` would miss these.

**Scope**: the rules cover four static forms only: model SDK imports, provider layer
imports, endpoint setting reads and direct environment reads. Dynamic forms are out of
reach of the AST: a module building an endpoint URL and calling it with a generic HTTP
client, ``importlib.import_module("bianque.llm.providers...")``, or
``getattr(os, "environ")``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

#: The scanned product package and its parent (the base of relative paths).
BACKEND_ROOT = Path(__file__).resolve().parent.parent
PRODUCT_ROOT = BACKEND_ROOT / "bianque"

#: Rule 1: top-level names of the model SDK.
_SDK_ROOTS = frozenset({"openai"})
#: Rule 1 exemption: the only layer allowed to import the model SDK.
_SDK_ALLOWED_PREFIX = "bianque/llm/providers/"

#: Rule 2: the protected provider layer.
_PROVIDER_MODULE = "bianque.llm.providers"
#: Rule 2 exemption: the single egress file (plus the provider layer itself). Exempting
#: the whole ``bianque/llm`` package would allow a bypass from inside it, so the
#: exemption is a single file.
_PROVIDER_IMPORT_ALLOWED_FILE = "bianque/llm/gateway.py"

#: Rule 3: endpoint and credential settings; only the llm package may read them.
_ENDPOINT_FIELDS = frozenset({"llm_base_url", "llm_api_key"})
_ENDPOINT_ALLOWED_PREFIX = "bianque/llm/"

#: Rule 4: the two environment members of ``os``; the exemption is the config package.
_ENV_MEMBERS = frozenset({"getenv", "environ"})
_ENV_ALLOWED_PREFIX = "bianque/config/"


@dataclass(frozen=True)
class Finding:
    """One rule violation: rule name, source path, line number, detail."""

    rule: str
    path: str
    line: int
    detail: str

    def locate(self) -> str:
        """Return ``<relative path>:<line>`` for assertion messages."""
        return f"{self.path}:{self.line}"


# Module name resolution: relative imports must be resolved first, or rule 2 is bypassed.
def _package_of(rel_path: str) -> str:
    """Return the package of a file (``bianque/llm/redacted.py`` -> ``bianque.llm``)."""
    parts = rel_path.split("/")
    return ".".join(parts[:-1])


def _resolve_from_module(node: ast.ImportFrom, package: str) -> str:
    """Resolve the source of ``from ... import ...`` (level > 0 means relative)."""
    if not node.level:
        return node.module or ""
    base = package.split(".") if package else []
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    if node.module:
        base = [*base, *node.module.split(".")]
    return ".".join(base)


def _import_targets(tree: ast.AST, package: str) -> list[tuple[int, str]]:
    """Expand every import to its absolute target module names, as ``(line, module)``.

    ``from bianque.llm import providers`` yields both the source module ``bianque.llm``
    and ``bianque.llm.providers``; the latter is the edge rule 2 must catch.
    """
    targets: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_from_module(node, package)
            if not module:
                continue
            targets.append((node.lineno, module))
            for alias in node.names:
                if alias.name != "*":
                    targets.append((node.lineno, f"{module}.{alias.name}"))
    return targets


# Rule 4: resolve the import aliases of ``os`` (never match only the ``os.`` prefix).
@dataclass(frozen=True)
class _OsBindings:
    """Local names bound by imports of ``os`` in one source file."""

    module_names: frozenset[str]
    member_names: frozenset[str]
    star_lines: tuple[int, ...]


def _collect_os_bindings(tree: ast.AST) -> _OsBindings:
    """Collect aliases of ``os``, direct bindings of ``getenv``/``environ`` and star imports."""
    modules: set[str] = set()
    members: set[str] = set()
    stars: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is None:
                    # Both ``import os`` and ``import os.path`` bind the local name ``os``.
                    if alias.name == "os" or alias.name.startswith("os."):
                        modules.add("os")
                elif alias.name == "os":
                    modules.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module == "os":
            for alias in node.names:
                if alias.name == "*":
                    stars.append(node.lineno)
                elif alias.name in _ENV_MEMBERS:
                    members.add(alias.asname or alias.name)
    return _OsBindings(frozenset(modules), frozenset(members), tuple(stars))


def _env_read_findings(tree: ast.AST, rel_path: str) -> list[Finding]:
    """Find every direct environment read via the resolved bindings (literal or not)."""
    bindings = _collect_os_bindings(tree)
    findings = [
        Finding("direct-env-read", rel_path, line, "from os import * (unresolvable, fail closed)")
        for line in bindings.star_lines
    ]
    for node in ast.walk(tree):
        # Module alias form: any read of <os alias>.getenv or <os alias>.environ.
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in bindings.module_names
            and node.attr in _ENV_MEMBERS
        ):
            findings.append(
                Finding("direct-env-read", rel_path, node.lineno, f"{node.value.id}.{node.attr}")
            )
        # Direct binding form: any load of a name bound by ``from os import getenv/environ``.
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in bindings.member_names
        ):
            findings.append(Finding("direct-env-read", rel_path, node.lineno, node.id))
    return findings


def scan_source(rel_path: str, source: str) -> list[Finding]:
    """Check one source file against the four rules, applying each rule's exemption.

    Args:
        rel_path: POSIX path of the file relative to ``backend/``; exemptions use it.
        source: Source text.

    Returns:
        Every violation; empty for compliant source.
    """
    tree = ast.parse(source, filename=rel_path)
    package = _package_of(rel_path)
    findings: list[Finding] = []

    for line, module in _import_targets(tree, package):
        root = module.split(".")[0]
        if root in _SDK_ROOTS and not rel_path.startswith(_SDK_ALLOWED_PREFIX):
            findings.append(Finding("llm-sdk-import", rel_path, line, module))
        is_provider = module == _PROVIDER_MODULE or module.startswith(f"{_PROVIDER_MODULE}.")
        allowed = rel_path == _PROVIDER_IMPORT_ALLOWED_FILE or rel_path.startswith(
            _SDK_ALLOWED_PREFIX
        )
        if is_provider and not allowed:
            findings.append(Finding("provider-import", rel_path, line, module))

    if not rel_path.startswith(_ENDPOINT_ALLOWED_PREFIX):
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in _ENDPOINT_FIELDS:
                findings.append(
                    Finding("llm-endpoint-config-read", rel_path, node.lineno, node.attr)
                )

    if not rel_path.startswith(_ENV_ALLOWED_PREFIX):
        findings.extend(_env_read_findings(tree, rel_path))

    return findings


def _scan_product_package(rule: str) -> list[Finding]:
    """Scan the whole product package and return the violations of one rule."""
    hits: list[Finding] = []
    sources = sorted(PRODUCT_ROOT.rglob("*.py"))
    assert sources, f"the product package must not be empty: {PRODUCT_ROOT}"
    for path in sources:
        rel_path = path.relative_to(BACKEND_ROOT).as_posix()
        for finding in scan_source(rel_path, path.read_text(encoding="utf-8")):
            if finding.rule == rule:
                hits.append(finding)
    return hits


# Rules 1 to 4 against the real product package.
def test_no_llm_sdk_import_outside_providers() -> None:
    hits = _scan_product_package("llm-sdk-import")
    assert hits == [], (
        f"model SDK imported outside the provider layer: {[h.locate() for h in hits]}"
    )


def test_no_provider_import_outside_gateway() -> None:
    hits = _scan_product_package("provider-import")
    assert hits == [], (
        "the provider layer is imported by a module other than the gateway: "
        f"{[(h.locate(), h.detail) for h in hits]}"
    )


def test_no_llm_endpoint_config_read_outside_gateway() -> None:
    hits = _scan_product_package("llm-endpoint-config-read")
    assert hits == [], (
        "model endpoint or credential settings read outside bianque/llm/: "
        f"{[(h.locate(), h.detail) for h in hits]}"
    )


def test_no_direct_env_read_outside_config() -> None:
    hits = _scan_product_package("direct-env-read")
    assert hits == [], (
        "direct environment read outside bianque/config/ (use get_settings()): "
        f"{[(h.locate(), h.detail) for h in hits]}"
    )


# The scanner detects known violations.
def test_scanner_flags_known_violations() -> None:
    samples: tuple[tuple[str, str, str], ...] = (
        ("bianque/agent/__init__.py", "import openai\n", "llm-sdk-import"),
        ("bianque/agent/__init__.py", "from openai import AsyncOpenAI\n", "llm-sdk-import"),
        (
            "bianque/notify/__init__.py",
            "def f(settings):\n    return settings.llm_base_url\n",
            "llm-endpoint-config-read",
        ),
        (
            "bianque/notify/__init__.py",
            "def f(settings):\n    return settings.llm_api_key\n",
            "llm-endpoint-config-read",
        ),
    )
    for rel_path, source, expected in samples:
        rules = {f.rule for f in scan_source(rel_path, source)}
        assert expected in rules, f"scanner missed a known violation ({expected}): {source!r}"


def test_scanner_flags_intra_llm_bypass() -> None:
    # Rule 2 inside the llm package: three violations, plus one false positive probe
    # (the gateway's own legitimate import must not be flagged).
    cases: tuple[tuple[str, str, bool], ...] = (
        ("bianque/llm/redacted.py", "from bianque.llm.providers import base\n", True),
        ("bianque/llm/redacted.py", "from .providers import base\n", True),
        ("bianque/llm/__init__.py", "from bianque.llm import providers\n", True),
        ("bianque/llm/gateway.py", "from bianque.llm.providers import base\n", False),
    )
    for rel_path, source, should_flag in cases:
        flagged = any(f.rule == "provider-import" for f in scan_source(rel_path, source))
        assert flagged is should_flag, (
            f"rule 2 verdict differs from the expected "
            f"{'violation' if should_flag else 'no violation'}: {rel_path} / {source!r}"
        )


def test_scanner_flags_env_read_bypass() -> None:
    # Rule 4: the first eight cases are violations (module alias form, direct binding
    # form, star import fail closed); the last three are not (exempt package, a
    # same-named local function, a non-environment member of os).
    gw = "bianque/llm/gateway.py"
    cases: tuple[tuple[str, str, bool], ...] = (
        (gw, 'import os\nx = os.getenv("BIANQUE_LLM_MODE")\n', True),
        (gw, "import os\ndef f(name):\n    return os.environ[name]\n", True),
        (gw, 'from os import getenv\nx = getenv("BIANQUE_LLM_MODE")\n', True),
        (gw, 'from os import environ\nx = environ["BIANQUE_LLM_MODE"]\n', True),
        (gw, 'import os as _o\nx = _o.environ.get("BIANQUE_LLM_MODE")\n', True),
        (gw, 'from os import getenv as _g\nx = _g("BIANQUE_LLM_MODE")\n', True),
        (gw, "from os import *\n", True),
        (
            "bianque/llm/redacted.py",
            'import os\nx = os.environ.get("BIANQUE_INVESTIGATION_TOKEN_BUDGET")\n',
            True,
        ),
        (
            "bianque/config/settings.py",
            'import os\nx = os.getenv("BIANQUE_LLM_MODE")\n',
            False,
        ),
        (gw, 'def getenv(key):\n    return key\nx = getenv("k")\n', False),
        (gw, 'import os\nx = os.path.join("a", "b")\n', False),
    )
    for rel_path, source, should_flag in cases:
        flagged = any(f.rule == "direct-env-read" for f in scan_source(rel_path, source))
        assert flagged is should_flag, (
            f"rule 4 verdict differs from the expected "
            f"{'violation' if should_flag else 'no violation'}: {rel_path} / {source!r}"
        )
