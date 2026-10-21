"""The A2A service: the only package that imports the A2A SDK.

Other agents hand Bianque an alert, or a case to replay, as an A2A task and receive the
investigation's progress and its report. The Agent Card is served without a token at the
well-known path; ``/a2a`` is the JSON-RPC binding and takes a bearer token per client.
"""

from __future__ import annotations

from bianque.a2a.card import card_routes
from bianque.a2a.server import A2AEndpoint, open_a2a

__all__ = ["A2AEndpoint", "card_routes", "open_a2a"]
