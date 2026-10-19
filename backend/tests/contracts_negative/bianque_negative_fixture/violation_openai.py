"""Deliberate violation: imports the model SDK outside the provider layer.

Breaks the negative mirror of the contract "The model SDK is imported only by the
provider layer". Not product code.
"""

import openai  # noqa: F401  deliberate violation; the negative check depends on it
