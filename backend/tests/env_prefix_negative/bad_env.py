"""Negative fixture for the environment variable prefix check.

This file deliberately reads an environment variable without the ``BIANQUE_`` prefix,
bypassing the settings module. It lives under ``tests/``, outside the ``bianque/config``
directory that the check scans by default, so the positive check still passes. Pointing
the check at this directory must make it fail with a non-zero exit code.
"""

import os

# Deliberate violation: a direct read of an unprefixed variable. The negative check
# depends on this line.
DATABASE_URL = os.getenv("DATABASE_URL")
