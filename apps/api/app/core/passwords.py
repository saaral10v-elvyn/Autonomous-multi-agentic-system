"""Argon2 password hashing helpers."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("not-a-real-user-password")


def hash_password(password: str) -> str:
    """Hash a password using Argon2id with library defaults."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password without exposing malformed-hash details."""
    try:
        return _hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def burn_dummy_hash(password: str) -> None:
    """Perform an Argon2 verification for unknown emails to reduce timing differences."""
    verify_password(password, _DUMMY_HASH)
