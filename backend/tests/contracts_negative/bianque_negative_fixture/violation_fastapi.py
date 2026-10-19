"""Deliberate violation: imports FastAPI outside the API package.

Breaks the negative mirror of the contract "Only the API package imports FastAPI".
Not product code.
"""

import fastapi  # noqa: F401  deliberate violation; the negative check depends on it
