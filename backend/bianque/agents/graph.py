"""The investigation graph.

```text
intake → retrieve_knowledge → plan → dispatch ──Send──▶ evidence_agent (one per domain) ┐
                                        ▲                                                │
                                        └──── replan ◀── insufficient ── assess ◀────────┘
                                                                   │ sufficient, budget or rounds
                                                                   ▼
                              degrade ◀── retries used up ── gate ◀── report ◀── rejected
                                 │                           │ passed
                                 ▼                           ▼
                              publish ◀──────────────────────┘ → END
```

The sub-agents of one round run in the same superstep, so a round lasts as long as its
slowest domain. ``assess`` runs once, after all of them.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from bianque.agents.nodes import InvestigationNodes
from bianque.agents.state import AgentTask, InvestigationState, Runtime
from bianque.agents.subagent import EvidenceAgent

#: Supersteps one run may take. Three rounds and three report attempts need about 25;
#: the limit only guards against a routing defect.
RECURSION_LIMIT = 100


def build_graph(runtime: Runtime) -> StateGraph[Any]:
    """Return the uncompiled investigation graph bound to ``runtime``."""
    nodes = InvestigationNodes(runtime)
    agent = EvidenceAgent(runtime)
    graph = StateGraph(InvestigationState)
    graph.add_node("intake", nodes.intake)
    graph.add_node("retrieve_knowledge", nodes.retrieve_knowledge)
    graph.add_node("plan", nodes.plan)
    graph.add_node("dispatch", nodes.dispatch)
    graph.add_node("evidence_agent", agent.run, input_schema=AgentTask)
    graph.add_node("assess", nodes.assess)
    graph.add_node("replan", nodes.replan)
    graph.add_node("report", nodes.report)
    graph.add_node("gate", nodes.gate)
    graph.add_node("degrade", nodes.degrade)
    graph.add_node("publish", nodes.publish)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "retrieve_knowledge")
    graph.add_edge("retrieve_knowledge", "plan")
    graph.add_edge("plan", "dispatch")
    graph.add_conditional_edges("dispatch", nodes.route_steps, ["evidence_agent", "assess"])
    graph.add_edge("evidence_agent", "assess")
    graph.add_conditional_edges("assess", nodes.after_assess, ["replan", "report"])
    graph.add_edge("replan", "dispatch")
    graph.add_conditional_edges("report", nodes.after_report, ["gate", "degrade"])
    graph.add_conditional_edges("gate", nodes.after_gate, ["publish", "report", "degrade"])
    graph.add_edge("degrade", "publish")
    graph.add_edge("publish", END)
    return graph
