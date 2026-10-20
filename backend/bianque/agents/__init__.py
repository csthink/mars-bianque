"""Agents: the investigation graph, the evidence sub-agents and what they share.

``runner.run_investigation`` runs one investigation; ``graph`` wires the nodes
(``nodes``, ``subagent``); ``domains`` declares the evidence domains and
``default_plan`` the plan a rejected plan falls back to. Prompts live in ``prompts/``;
``scripted_rules`` plays the agent roles in scripted mode. The follow-up flow is added
here as well.
"""
