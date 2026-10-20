"""Deliberate violation: imports the MCP SDK outside the MCP package.

Breaks the negative mirror of the contract "Only the MCP package imports the MCP SDK".
Not product code.
"""

import mcp  # noqa: F401  deliberate violation; the negative check depends on it
import mcp_types  # noqa: F401  deliberate violation; the negative check depends on it
