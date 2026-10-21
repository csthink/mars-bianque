"""Deliberate violation: imports the storage layer the way the skills package must not.

Breaks the negative mirror of the contract "The skills package imports no other Bianque
package". Not product code.
"""

import bianque.storage  # noqa: F401  deliberate violation; the negative check depends on it
