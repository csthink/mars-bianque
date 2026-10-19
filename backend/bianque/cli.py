"""The ``bianque`` command line.

Subcommands:

- ``bianque pack record``: record an evidence pack from the demo environment.
- ``bianque pack derive``: derive an evidence pack from a recorded one.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from bianque.config import get_settings
from bianque.tools import registry
from bianque.tools.backends.base import FaultMode
from bianque.tools.pack.derive import KEEPABLE, derive_pack
from bianque.tools.pack.format import PackError
from bianque.tools.pack.record import record_pack


def _pairs(values: Sequence[str], option: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for value in values:
        key, sep, item = value.partition("=")
        if not sep or not key:
            raise SystemExit(f"{option} expects key=value, got {value!r}")
        pairs[key] = item
    return pairs


def _faults(values: Sequence[str]) -> dict[str, FaultMode]:
    faults: dict[str, FaultMode] = {}
    for value in values:
        tool, sep, mode = value.partition(":")
        if not sep or mode != "timeout":
            raise SystemExit(f"--fault expects <tool>:timeout, got {value!r}")
        faults[tool] = "timeout"
    return faults


async def _record(args: argparse.Namespace) -> None:
    from bianque.tools.backends.kubernetes_live import LiveKubernetes

    settings = get_settings()
    kubernetes = await LiveKubernetes.connect(context=args.context or settings.kube_context)
    try:
        summary = await record_pack(
            kubernetes,
            namespace=args.namespace,
            alert_text=Path(args.alert).read_text(),
            repo=Path(args.repo) if args.repo else None,
            case=args.case,
            out=Path(args.out),
            recorded_at=datetime.now(UTC).replace(microsecond=0),
            versions=_pairs(args.meta, "--meta"),
        )
    finally:
        await kubernetes.close()
    objects = ", ".join(f"{count} {kind}" for kind, count in summary.objects.items())
    print(
        f"recorded {args.out}: {summary.files} files; {objects}; {summary.logs} logs; "
        f"{summary.high_entropy_hits} high-entropy strings (UIDs, hashes), no rule hits"
    )


def _derive(args: argparse.Namespace) -> None:
    derive_pack(
        Path(args.source),
        Path(args.out),
        case=args.case,
        faults=_faults(args.fault),
        only=args.only.split(",") if args.only else None,
        known_tools={item.name for item in registry},
    )
    print(f"derived {args.out} from {args.source}")


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser of the ``bianque`` command."""
    parser = argparse.ArgumentParser(prog="bianque")
    commands = parser.add_subparsers(dest="command", required=True)

    pack = commands.add_parser("pack", help="record and derive evidence packs")
    pack_commands = pack.add_subparsers(dest="pack_command", required=True)

    record = pack_commands.add_parser("record", help="record an evidence pack from a cluster")
    record.add_argument("--namespace", required=True)
    record.add_argument("--alert", required=True, help="file holding the webhook body")
    record.add_argument("--repo", help="the manifest Git repository")
    record.add_argument("--case", required=True)
    record.add_argument("--out", required=True)
    record.add_argument("--context", help="kubeconfig context; BIANQUE_KUBE_CONTEXT by default")
    record.add_argument("--meta", action="append", default=[], help="version key=value")

    derive = pack_commands.add_parser("derive", help="derive an evidence pack from another")
    derive.add_argument("--from", dest="source", required=True)
    derive.add_argument("--out", required=True)
    derive.add_argument("--case", required=True)
    derive.add_argument("--fault", action="append", default=[], help="<tool>:timeout")
    derive.add_argument("--only", help=f"comma-separated parts to keep: {','.join(KEEPABLE)}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return the exit status."""
    args = build_parser().parse_args(argv)
    try:
        if args.pack_command == "record":
            asyncio.run(_record(args))
        else:
            _derive(args)
    except PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
