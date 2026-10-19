"""The default plan: what a plan falls back to when the gate rejects the model's plan.

It is fixed text, built without a model. The plan text enters later prompts and with
them the request key of recorded responses, so it holds no time and no investigation ID.
"""

from __future__ import annotations

from bianque.agents.domains import DOMAINS
from bianque.gate import Plan, PlanStep

_RATIONALE = {
    True: "Default plan: the model's plan was rejected by the evidence gate. Read the current "
    "state of the alerted pods and their logs.",
    False: "Default plan: the model's plan was rejected by the evidence gate. Read the recent "
    "changes of the alerted workload.",
}


def default_plan(*, first_round: bool) -> Plan:
    """Return the default plan of the first round or of a supplementary round.

    Args:
        first_round: True for the first plan: the steps of the first-round domains.
            False for a replan: the steps of the other domains.
    """
    steps = [
        PlanStep(
            goal=template.goal,
            domain=item.name,
            suggested_tools=list(template.suggested_tools),
            expected_evidence=template.expected_evidence,
        )
        for item in DOMAINS
        if item.first_round is first_round
        for template in item.default_steps
    ]
    return Plan(rationale=_RATIONALE[first_round], steps=steps, knowledge_refs=[])
