"""Deliberate violation: imports the embedding library outside the embedding module.

Breaks the negative mirror of the contract "Only the embedding module imports the
embedding library". Not product code.
"""

import fastembed  # noqa: F401  deliberate violation; the negative check depends on it
