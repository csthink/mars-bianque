"""Deliberate violation: imports the model gateway the way the evidence gate must not.

Breaks the negative mirror of the contract "The evidence gate does not import the model
gateway". Not product code.
"""

import bianque.llm  # noqa: F401  deliberate violation; the negative check depends on it
