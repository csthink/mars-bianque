"""Deliberate violation: imports the Kubernetes client outside the real backend.

Breaks the negative mirror of the contract "Only the real Kubernetes backend imports
the Kubernetes client". Not product code.
"""

import kubernetes_asyncio  # noqa: F401  deliberate violation; the negative check depends on it
