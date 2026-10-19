"""Static type gate: passing unredacted input to the gateway fails type checking.

Half of the "cannot be bypassed" property holds at type-check time: passing a plain
``str`` to the gateway must be an error under mypy strict. A signature that says
``RedactedPrompt`` does not prove this by itself, because the type can be silently
swallowed by ``Any`` (for example, if someone adds an overload with ``**kwargs: Any``).
So this test actually runs mypy on two temporary sources.

Three rules:

- **Both directions**: the negative probe must fail and the positive probe must pass.
  With only a negative probe, an implementation that types the parameter as ``Any``
  and adds ``# type: ignore`` could still make the negative fail while breaking
  legitimate use.
- **Assert the error code ``arg-type``**: asserting only a non-zero exit would count a
  syntax or import error in the probe as a working type gate.
- **Fail, never skip, when ``mypy.api`` is unavailable**: silently skipping would mean
  nothing was tested.

Configuration: ``follow_imports = silent`` makes followed modules parse without
reporting errors (third-party noise would otherwise pollute the positive probe). The
``openai`` package is skipped entirely; it is the largest dependency tree, and the
signature under test is in this repository.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

#: Backend root. ``mypy_path`` points here so that probes import this repository's
#: source rather than an installed copy.
BACKEND_ROOT = Path(__file__).resolve().parent.parent

_MYPY_CONFIG = """\
[mypy]
python_version = 3.12
strict = True
mypy_path = {backend_root}
follow_imports = silent
ignore_missing_imports = True

[mypy-openai]
follow_imports = skip

[mypy-openai.*]
follow_imports = skip
"""


def _run_mypy(tmp_path: Path, label: str, source: str) -> tuple[str, int]:
    """Run mypy --strict on a source snippet and return ``(stdout, exit code)``.

    Args:
        tmp_path: Directory for the probe source and its configuration.
        label: Probe name (used as the file name, to locate failures).
        source: Probe source.

    Returns:
        mypy's standard output and exit code.
    """
    try:
        from mypy import api
    except ImportError as exc:  # pragma: no cover - fail on purpose when mypy is missing
        pytest.fail(f"mypy.api is unavailable; the type gate probe cannot run: {exc}")

    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "mypy.ini"
    config.write_text(_MYPY_CONFIG.format(backend_root=BACKEND_ROOT), encoding="utf-8")
    probe = tmp_path / f"{label}.py"
    probe.write_text(textwrap.dedent(source), encoding="utf-8")

    stdout, stderr, exit_code = api.run(
        ["--config-file", str(config), "--no-incremental", "--no-error-summary", str(probe)]
    )
    assert not stderr, f"the mypy probe itself failed ({label}): {stderr}"
    return stdout, exit_code


_NEGATIVE_COMPLETE = """
    import asyncio

    from bianque.config import get_settings
    from bianque.llm.gateway import build_gateway


    async def main() -> None:
        gateway = build_gateway(get_settings())
        await gateway.complete("plain str")


    asyncio.run(main())
"""

_POSITIVE_COMPLETE = """
    import asyncio

    from bianque.config import get_settings
    from bianque.llm.gateway import build_gateway
    from bianque.llm.redacted import redact_prompt_for_llm


    async def main() -> None:
        gateway = build_gateway(get_settings())
        await gateway.complete(redact_prompt_for_llm([("user", "why did pod restart")]))


    asyncio.run(main())
"""


def test_plain_str_rejected_by_mypy(tmp_path: Path) -> None:
    negative, negative_code = _run_mypy(tmp_path / "neg", "probe", _NEGATIVE_COMPLETE)
    assert negative_code != 0, "mypy accepted a plain str passed to complete()"
    assert "arg-type" in negative, f"mypy failed, but not with a type mismatch: {negative}"

    positive, positive_code = _run_mypy(tmp_path / "pos", "probe", _POSITIVE_COMPLETE)
    assert positive_code == 0, f"mypy rejected legitimate use: {positive}"
