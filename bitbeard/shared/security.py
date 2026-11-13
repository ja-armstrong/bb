"""Password hashing for the user store.

Uses only the standard library so the API image needs no native crypto
dependency. Hashes are self-describing (``pbkdf2_sha256$iterations$salt$hash``)
so the iteration count can be raised later without invalidating old hashes.
"""

import hashlib
import hmac
import secrets

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 260_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Hash ``password`` with a fresh random salt."""
    salt = secrets.token_hex(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        _ITERATIONS,
    )
    return f"{_ALGORITHM}${_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Return True when ``password`` matches an encoded hash."""
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != _ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt),
            int(iterations),
        )
    except (AttributeError, TypeError, ValueError):
        return False

    return hmac.compare_digest(digest.hex(), expected)
