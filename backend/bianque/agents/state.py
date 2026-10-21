"""The state of the investigation graph and what its nodes depend on.

The state holds IDs and small control values only; plans, steps, evidence and reports
live in the database. It is written to the checkpoint after every step of the graph, so
it holds no model text except the body of the report under validation, which the
``reports`` table keeps as well.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bianque.agents.records import DatabaseCallRecorder, EventWriter
from bianque.api.alertmanager import AlertInput
from bianque.knowledge.search import KnowledgeSearch
from bianque.llm import LLMGateway
from bianque.redaction import RedactionEngine
from bianque.skills import SkillCatalog
from bianque.tools import ToolExecutor, ToolRegistry


class Failure(TypedDict):
    """A failed evidence tool call, or a failed sub-agent (``tool`` is ``None``)."""

    domain: str
    tool: str | None
    error: str


class PendingStep(TypedDict):
    """A step of the current round, to be dispatched to its domain's sub-agent."""

    id: int
    domain: str


class InvestigationState(TypedDict, total=False):
    """The state of one investigation run.

    Attributes:
        investigation_id: The investigation; also the checkpoint thread ID.
        round: The current round of evidence collection, from 1.
        pending: The steps of the current round.
        failures: Failed calls of every round; sub-agents append to it in parallel.
        knowledge: The knowledge given to the model, as ``{id, title}``; the chunk texts
            stay in the database.
        skills: The names of the skills loaded so far, in the order they were loaded;
            their bodies stay in the skill files.
        sufficient: The latest assessment found the evidence sufficient.
        budget_exhausted: The token budget was exceeded at the latest assessment.
        report_attempt: The number of reports generated so far.
        report_body: The report under validation, or the degraded report.
        rejection: The violations of the latest rejected report.
        verdict: What the gate decided about the latest report.
        report_gate: The gate result of the report to publish.
        model_failed: The model could not be called for the report.
    """

    investigation_id: str
    round: int
    pending: list[PendingStep]
    failures: Annotated[list[Failure], operator.add]
    knowledge: list[dict[str, str]]
    skills: list[str]
    sufficient: bool
    budget_exhausted: bool
    report_attempt: int
    report_body: dict[str, Any] | None
    rejection: list[dict[str, str]]
    verdict: Literal["passed", "retry", "degrade"]
    report_gate: dict[str, Any]
    model_failed: bool


class AgentTask(TypedDict):
    """What ``dispatch`` sends to one evidence sub-agent."""

    investigation_id: str
    domain: str
    step_ids: list[int]


@dataclass
class Runtime:
    """What the nodes of one investigation run use.

    Attributes:
        sessions: Database sessions; every node commits its own writes.
        gateway: The model gateway.
        recorder: The gateway's call recorder; it also stores gate results.
        executor: The tool executor.
        tools: The tool registry.
        engine: The redaction engine shared with the gateway and the executor.
        events: Progress event writer.
        knowledge: Runbook retrieval.
        alert: The alert under investigation.
        skills: The skills the planning model may load.
        max_skill_activations: ``activate_skill`` calls of one plan generation; 0 turns
            skills off.
        subagent_max_steps: Model turns of one step of a sub-agent.
        max_replan_rounds: Supplementary plans of the investigation.
        report_max_retries: Retries after a rejected report.
        token_budget: Token budget of the investigation.
        announced: Evidence IDs already announced with an ``evidence_added`` event.
    """

    sessions: async_sessionmaker[AsyncSession]
    gateway: LLMGateway
    recorder: DatabaseCallRecorder
    executor: ToolExecutor
    tools: ToolRegistry
    engine: RedactionEngine
    events: EventWriter
    knowledge: KnowledgeSearch
    alert: AlertInput
    skills: SkillCatalog
    max_skill_activations: int
    subagent_max_steps: int
    max_replan_rounds: int
    report_max_retries: int
    token_budget: int
    announced: set[str] = field(default_factory=set)

    @property
    def alert_identifiers(self) -> tuple[str, ...]:
        """The identifiers redaction keeps in the alert."""
        return self.alert.identifiers

    @property
    def plans_with_skills(self) -> bool:
        """Whether planning offers ``activate_skill``: there are skills and calls allowed."""
        return bool(self.skills) and self.max_skill_activations > 0
