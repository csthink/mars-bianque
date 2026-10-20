"""The ``bianque`` command line.

Subcommands:

- ``bianque pack record``: record an evidence pack from the demo environment.
- ``bianque pack derive``: derive an evidence pack from a recorded one.
- ``bianque gate export-schemas``: write the JSON Schema of every model output type.
- ``bianque investigate``: investigate the alert of an evidence pack, or an alert file.
- ``bianque knowledge download-model``: download and check the local embedding model.
- ``bianque knowledge ingest``: import a directory of runbooks.
- ``bianque knowledge embed-fixtures``: write or check the embedding fixture.
- ``bianque eval retrieval``: evaluate the fused retrieval on annotated queries.
- ``bianque auth hash-password``: print the bcrypt hash of a password read from stdin.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bianque.config import get_settings
from bianque.gate import export_json_schemas
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


def _export_schemas(args: argparse.Namespace) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, schema in export_json_schemas().items():
        (out / name).write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(export_json_schemas())} schemas to {out}")


async def _investigate(args: argparse.Namespace) -> int:
    from bianque.agents.runner import run_investigation
    from bianque.api.alertmanager import parse_webhook
    from bianque.storage.db import create_engine, session_factory
    from bianque.tools.backends import open_backends

    settings = get_settings()
    if args.case and settings.tool_backend != "pack":
        raise SystemExit("--case replays an evidence pack: set BIANQUE_TOOL_BACKEND=pack")
    if args.alert:
        alert_file = Path(args.alert)
    else:
        alert_file = settings.cases_dir / args.case / "pack" / "alert.json"
    alert = parse_webhook(json.loads(alert_file.read_text()))[0]
    engine = create_engine(settings)
    try:
        sessions = session_factory(engine)
        async with open_backends(settings, case=args.case) as backends:
            outcome = await run_investigation(
                settings,
                alert,
                sessions=sessions,
                backends=backends,
                trigger="replay" if args.case else "manual",
                case_id=args.case,
            )
        print(await _summary(sessions, outcome.investigation_id))
    finally:
        await engine.dispose()
    if outcome.status == "degraded":
        print("warning: no model report passed the evidence gate", file=sys.stderr)
    return 1 if outcome.status == "failed" else 0


async def _summary(sessions: Any, investigation_id: str) -> str:
    from bianque.storage.repositories import (
        AssessmentRepository,
        InvestigationRepository,
        PlanRepository,
        ReportRepository,
    )

    async with sessions() as session:
        investigation = await InvestigationRepository(session).get(investigation_id)
        plans = await PlanRepository(session).list_plans(investigation_id)
        assessments = await AssessmentRepository(session).list(investigation_id)
        report = await ReportRepository(session).get_final(investigation_id)
    lines = [
        f"investigation {investigation.id}: {investigation.status}"
        + (f" ({investigation.error})" if investigation.error else ""),
        f"tokens: {investigation.tokens_used} of {investigation.token_budget}",
        "plans: " + ", ".join(f"v{p.version} ({p.source})" for p in plans),
    ]
    lines += [
        f"round {a.round}: {'sufficient' if a.sufficient else 'insufficient'}"
        f", hypothesis {a.hypothesis_category or 'none'}"
        + (", overridden by the rule" if a.rule_override else "")
        for a in assessments
    ]
    if report is not None:
        cause = report.body["root_cause"]
        target = cause.get("object") or {}
        lines.append(f"report: {report.status}, attempt {report.attempt}")
        lines.append(
            f"root cause: {cause['status']} {cause.get('category') or ''} "
            f"{target.get('kind', '')}/{target.get('name', '')} "
            f"{target.get('field') or ''} confidence {cause['confidence']} "
            f"citations {', '.join(cause['citations']) or 'none'}".replace("  ", " ")
        )
        lines += [
            f"missing: {item['what']} {item['reason']}" for item in report.body["missing_evidence"]
        ]
    return "\n".join(lines)


def _download_model(args: argparse.Namespace) -> int:
    from bianque.knowledge.embedding import download_model

    path = download_model(get_settings().embedding_model_dir)
    print(f"embedding model ready: {path}")
    return 0


async def _ingest(args: argparse.Namespace) -> int:
    from bianque.knowledge.embedding import build_embedder
    from bianque.knowledge.ingest import ingest
    from bianque.storage.db import create_engine, session_factory

    settings = get_settings()
    engine = create_engine(settings)
    try:
        summary = await ingest(
            Path(args.directory),
            sessions=session_factory(engine),
            embedder=build_embedder(settings),
        )
    finally:
        await engine.dispose()
    print(
        f"ingested {args.directory}: {summary.added} added, {summary.updated} updated, "
        f"{summary.skipped} unchanged, {summary.deleted} deleted; {summary.chunks} chunks"
    )
    return 0


async def _embed_fixtures(args: argparse.Namespace) -> int:
    from bianque.evals.retrieval import load_queries
    from bianque.knowledge.embedding import (
        MODEL_NAME,
        MODEL_SOURCE,
        LocalEmbedder,
        embedder_version,
    )
    from bianque.knowledge.fixtures import coverage, fixture_texts, load_fixture, write_fixture
    from bianque.storage.models import EMBEDDING_DIMENSION

    queries = [query.query for query in load_queries(Path(args.queries))]
    texts = fixture_texts(Path(args.runbooks), Path(args.cases), queries)
    out = Path(args.out)
    if args.check:
        fixture = load_fixture(out, model=MODEL_NAME, dimension=EMBEDDING_DIMENSION)
        missing, extra = coverage(fixture, texts)
        for text in missing:
            print(f"missing: {text[:80]!r}", file=sys.stderr)
        if extra:
            print(f"{len(extra)} vectors no text needs", file=sys.stderr)
        if missing or extra:
            print(f"error: {out} is out of date; run make knowledge-fixtures", file=sys.stderr)
            return 1
        print(f"{out} covers all {len(texts)} texts")
        return 0
    embedder = LocalEmbedder(get_settings().embedding_model_dir)
    keys = sorted(texts)
    vectors = await embedder.embed([texts[key] for key in keys])
    write_fixture(
        out,
        dict(zip(keys, vectors, strict=True)),
        model=MODEL_NAME,
        model_source=MODEL_SOURCE,
        model_sha256=embedder.model_sha256(),
        embedder=embedder_version(),
        dimension=EMBEDDING_DIMENSION,
    )
    print(f"wrote {len(keys)} vectors to {out}")
    return 0


async def _eval_retrieval(args: argparse.Namespace) -> int:
    from bianque.evals.retrieval import (
        check_annotations,
        evaluate,
        load_queries,
        write_report,
    )
    from bianque.knowledge import KnowledgeRetriever
    from bianque.knowledge.embedding import MODEL_NAME, build_embedder, file_sha256
    from bianque.storage.db import create_engine, session_factory
    from bianque.storage.repositories import KnowledgeRepository

    settings = get_settings()
    path = Path(args.queries)
    queries = load_queries(path)
    engine = create_engine(settings)
    try:
        sessions = session_factory(engine)
        async with sessions() as session:
            known = {chunk.id for chunk, _ in await KnowledgeRepository(session).all_chunks()}
        check_annotations(queries, known, path)
        outcomes = await evaluate(queries, KnowledgeRetriever(sessions, build_embedder(settings)))
    finally:
        await engine.dispose()
    context = {
        "embedding_mode": settings.embedding_mode,
        "embedding_model": MODEL_NAME,
        "queries_file": path.name,
        "queries_sha256": file_sha256(path),
    }
    if settings.embedding_mode == "fixture":
        context["fixture_file"] = settings.embedding_fixture.name
        context["fixture_sha256"] = file_sha256(settings.embedding_fixture)
    report = write_report(Path(args.out), outcomes, context=context)
    print(", ".join(f"{name} {value}" for name, value in report["metrics"].items()))
    return 0


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

    gate = commands.add_parser("gate", help="the evidence gate")
    gate_commands = gate.add_subparsers(dest="gate_command", required=True)
    export = gate_commands.add_parser(
        "export-schemas", help="write the JSON Schema of every model output type"
    )
    export.add_argument("--out", required=True, help="directory for <type>.schema.json")

    investigate = commands.add_parser("investigate", help="run one investigation to the end")
    source = investigate.add_mutually_exclusive_group(required=True)
    source.add_argument("--case", help="replay the alert of this case's evidence pack")
    source.add_argument("--alert", help="file holding an Alertmanager webhook body")

    knowledge = commands.add_parser("knowledge", help="runbook knowledge")
    knowledge_commands = knowledge.add_subparsers(dest="knowledge_command", required=True)
    knowledge_commands.add_parser(
        "download-model", help="download the local embedding model and check its hash"
    )
    ingest = knowledge_commands.add_parser("ingest", help="import a directory of runbooks")
    ingest.add_argument("directory")
    fixtures = knowledge_commands.add_parser(
        "embed-fixtures", help="write the embedding fixture with the local model"
    )
    fixtures.add_argument("--runbooks", required=True, help="the runbook directory")
    fixtures.add_argument("--cases", required=True, help="the evaluation case directory")
    fixtures.add_argument("--queries", required=True, help="the retrieval evaluation queries")
    fixtures.add_argument("--out", required=True, help="the fixture file")
    fixtures.add_argument(
        "--check", action="store_true", help="only check that the fixture covers every text"
    )

    auth = commands.add_parser("auth", help="demo accounts")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_commands.add_parser(
        "hash-password",
        help="print the bcrypt hash of the password on the first line of standard input",
    )

    evals = commands.add_parser("eval", help="evaluation")
    eval_commands = evals.add_subparsers(dest="eval_command", required=True)
    retrieval = eval_commands.add_parser(
        "retrieval", help="hit@k and MRR of the fused retrieval on imported runbooks"
    )
    retrieval.add_argument("--queries", required=True, help="the annotated queries")
    retrieval.add_argument("--out", required=True, help="directory for retrieval.json and .md")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return the exit status."""
    args = build_parser().parse_args(argv)
    if args.command == "investigate":
        return asyncio.run(_investigate(args))
    if args.command == "gate":
        _export_schemas(args)
        return 0
    if args.command in ("knowledge", "eval"):
        return _knowledge(args)
    if args.command == "auth":
        return _hash_password()
    try:
        if args.pack_command == "record":
            asyncio.run(_record(args))
        else:
            _derive(args)
    except PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _hash_password() -> int:
    # The password comes on standard input, never as an argument: arguments show up in
    # the process list and in shell history.
    from bianque.auth.passwords import hash_password

    password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        print("error: no password on standard input", file=sys.stderr)
        return 1
    try:
        print(hash_password(password))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _knowledge(args: argparse.Namespace) -> int:
    from bianque.evals.retrieval import RetrievalEvalError
    from bianque.knowledge.chunking import ChunkingError
    from bianque.knowledge.embedding import EmbeddingError
    from bianque.knowledge.fixtures import EmbeddingFixtureError

    try:
        if args.command == "eval":
            return asyncio.run(_eval_retrieval(args))
        if args.knowledge_command == "download-model":
            return _download_model(args)
        if args.knowledge_command == "ingest":
            return asyncio.run(_ingest(args))
        return asyncio.run(_embed_fixtures(args))
    except (RetrievalEvalError, ChunkingError, EmbeddingError, EmbeddingFixtureError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
