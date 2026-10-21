"""Deliberate violation: imports the A2A SDK outside the A2A package.

Breaks the negative mirror of the contract "Only the A2A package imports the A2A SDK".
Not product code.
"""

import a2a  # noqa: F401  deliberate violation; the negative check depends on it
