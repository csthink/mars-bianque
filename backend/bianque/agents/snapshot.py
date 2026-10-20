"""What the investigation agent reads back from the database before a model call.

``load_snapshot`` reads the evidence and the findings of an investigation. The gate
context and the preserved identifiers are built from it in one place, so the prompt and
the gate see the same identifiers.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bianque.agents.domains import domain
from bianque.agents.prompting import EvidenceView, FailureView, FindingsView
from bianque.agents.state import Failure, Runtime
from bianque.gate import EvidenceItem, FailedCall, GateContext
from bianque.storage.repositories import EvidenceRepository, PlanRepository
from bianque.tools import ToolRegistry


@dataclass(frozen=True)
class Snapshot:
    """The evidence and findings of an investigation at one moment.

    Attributes:
        evidence: All evidence, sorted by ID.
        items: The same evidence with collection times, for the degraded report.
        findings: The findings of every finished step, in plan order.
    """

    evidence: tuple[EvidenceView, ...]
    items: tuple[EvidenceItem, ...]
    findings: tuple[FindingsView, ...]

    @property
    def evidence_ids(self) -> frozenset[str]:
        """The evidence IDs of the investigation."""
        return frozenset(item.id for item in self.evidence)

    def tools_used(self) -> set[str]:
        """The tools that produced evidence."""
        return {item.tool for item in self.evidence if item.tool}


async def load_snapshot(
    sessions: async_sessionmaker[AsyncSession], investigation_id: str
) -> Snapshot:
    """Read the evidence and findings of an investigation."""
    async with sessions() as session:
        rows = await EvidenceRepository(session).list(investigation_id)
        plans = {
            plan.id: plan.version
            for plan in await PlanRepository(session).list_plans(investigation_id)
        }
        steps = await PlanRepository(session).list_steps(investigation_id)
    evidence = tuple(
        EvidenceView(row.id, row.source, row.tool, row.redacted_content, tuple(row.identifiers))
        for row in rows
    )
    items = tuple(
        EvidenceItem(row.id, row.source, row.tool, row.collected_at, row.redacted_content)
        for row in rows
    )
    findings = tuple(
        FindingsView(plans[step.plan_id], step.seq, step.domain, step.goal, tuple(step.findings))
        for step in steps
        if step.findings is not None
    )
    return Snapshot(evidence, items, findings)


def preserved_identifiers(
    evidence: Iterable[EvidenceView], alert_identifiers: Sequence[str], tools: ToolRegistry
) -> frozenset[str]:
    """Return the identifiers redaction keeps and the gate does not count as residue.

    They are the identifiers of the evidence, those of the alert, and every tool name:
    alert names and tool names are often longer than the entropy check's threshold.
    """
    return frozenset(
        {
            *(ident for item in evidence for ident in item.identifiers),
            *alert_identifiers,
            *(definition.name for definition in tools),
        }
    )


def failure_views(failures: Iterable[Failure]) -> list[FailureView]:
    """Return the failures in the form prompts show them."""
    return [FailureView(f["domain"], f["tool"], f["error"]) for f in failures]


def failed_calls(failures: Iterable[Failure]) -> tuple[FailedCall, ...]:
    """Return the failures in the form the gate checks them, sorted."""
    calls = {
        FailedCall(
            tool=f["tool"] or f"{f['domain']} evidence agent",
            domain=f["domain"],
            error=f["error"],
            caps_confidence=domain(f["domain"]).failure_caps_confidence,
        )
        for f in failures
    }
    return tuple(sorted(calls, key=lambda c: (c.domain, c.tool, c.error)))


def gate_context(
    snapshot: Snapshot,
    runtime: Runtime,
    *,
    failures: Iterable[Failure] = (),
    knowledge_ids: Iterable[str] = (),
) -> GateContext:
    """Return the gate context of an output of this investigation."""
    return GateContext(
        evidence_ids=snapshot.evidence_ids,
        knowledge_ids=frozenset(knowledge_ids),
        preserved=preserved_identifiers(
            snapshot.evidence, runtime.alert_identifiers, runtime.tools
        ),
        failed_calls=failed_calls(failures),
        engine=runtime.engine,
    )
