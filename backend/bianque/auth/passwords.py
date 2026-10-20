"""Demo account passwords: bcrypt hashes, and checking a password against one."""

from __future__ import annotations

import re

import bcrypt

#: The cost of new hashes. One check at this cost takes about 0.2 seconds.
ROUNDS = 12

#: bcrypt reads at most this many bytes of a password (UTF-8).
MAX_PASSWORD_BYTES = 72

#: A bcrypt hash in modular crypt format: version, two-digit cost, 22 characters of salt
#: and 31 of hash.
BCRYPT_HASH = re.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}")


def hash_password(password: str, *, rounds: int = ROUNDS) -> str:
    """Return the bcrypt hash of a password.

    Raises:
        ValueError: The password is longer than 72 bytes in UTF-8.
    """
    encoded = password.encode()
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(f"a password is at most {MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=rounds)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Whether the password matches the hash.

    A password longer than 72 bytes never matches: bcrypt would compare only a prefix
    of it. A malformed hash never matches either.
    """
    encoded = password.encode()
    if len(encoded) > MAX_PASSWORD_BYTES or not BCRYPT_HASH.fullmatch(password_hash):
        return False
    return bcrypt.checkpw(encoded, password_hash.encode())
