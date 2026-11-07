"""Envelope encryption for provider credentials."""

import base64
import binascii
import secrets
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

from app.core.config import get_settings

_MAGIC = b"FGK1"
_NONCE_BYTES = 12
_DATA_KEY_BYTES = 32
_WRAPPED_KEY_BYTES = _DATA_KEY_BYTES + 16


class CryptoConfigurationError(ValueError):
    """Raised when the master encryption key is missing or malformed."""


class SecretDecryptionError(ValueError):
    """Raised when an encrypted credential cannot be authenticated."""


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    """Ciphertext and its per-record data nonce."""

    ciphertext: bytes
    nonce: bytes


def decode_master_key(value: SecretStr | str | None = None) -> bytes:
    """Decode a URL-safe Base64 encoded 32-byte AES-256 master key."""
    configured: SecretStr | str | None = value if value is not None else get_settings().master_key
    encoded: str | None
    if isinstance(configured, SecretStr):
        encoded = configured.get_secret_value()
    else:
        encoded = configured
    if not encoded:
        raise CryptoConfigurationError(
            "MASTER_KEY must contain a URL-safe Base64 encoded 32-byte key."
        )
    try:
        key = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise CryptoConfigurationError(
            "MASTER_KEY must contain a URL-safe Base64 encoded 32-byte key."
        ) from None
    if len(key) != _DATA_KEY_BYTES:
        raise CryptoConfigurationError("MASTER_KEY must decode to exactly 32 bytes.")
    return key


def derive_subkey(master_key: bytes, purpose: str) -> bytes:
    """Derive a purpose-separated key for session and CSRF signing."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_DATA_KEY_BYTES,
        salt=b"forge-key-derivation-v1",
        info=f"forge:{purpose}:v1".encode(),
    ).derive(master_key)


def _associated_data(user_id: UUID, record_id: UUID, purpose: bytes) -> bytes:
    return (
        b"forge:provider-key:v1:"
        + str(user_id).encode()
        + b":"
        + str(record_id).encode()
        + b":"
        + purpose
    )


def encrypt_secret(
    plaintext: str,
    *,
    user_id: UUID,
    record_id: UUID,
    master_key: SecretStr | str | bytes | None = None,
) -> EncryptedSecret:
    """Encrypt a credential with a random DEK and wrap that DEK with the master key."""
    if isinstance(master_key, bytes):
        wrapping_key = master_key
        if len(wrapping_key) != _DATA_KEY_BYTES:
            raise CryptoConfigurationError("MASTER_KEY must decode to exactly 32 bytes.")
    else:
        wrapping_key = decode_master_key(master_key)
    data_key = AESGCM.generate_key(bit_length=256)
    data_nonce = secrets.token_bytes(_NONCE_BYTES)
    wrap_nonce = secrets.token_bytes(_NONCE_BYTES)
    data_ciphertext = AESGCM(data_key).encrypt(
        data_nonce,
        plaintext.encode("utf-8"),
        _associated_data(user_id, record_id, b"data"),
    )
    wrapped_key = AESGCM(wrapping_key).encrypt(
        wrap_nonce,
        data_key,
        _associated_data(user_id, record_id, b"dek"),
    )
    return EncryptedSecret(_MAGIC + wrap_nonce + wrapped_key + data_ciphertext, data_nonce)


def decrypt_secret(
    encrypted: EncryptedSecret,
    *,
    user_id: UUID,
    record_id: UUID,
    master_key: SecretStr | str | bytes | None = None,
) -> str:
    """Authenticate and decrypt one provider credential without exposing crypto errors."""
    if isinstance(master_key, bytes):
        wrapping_key = master_key
        if len(wrapping_key) != _DATA_KEY_BYTES:
            raise CryptoConfigurationError("MASTER_KEY must decode to exactly 32 bytes.")
    else:
        wrapping_key = decode_master_key(master_key)
    try:
        if len(encrypted.nonce) != _NONCE_BYTES or not encrypted.ciphertext.startswith(_MAGIC):
            raise InvalidTag
        offset = len(_MAGIC)
        wrap_nonce = encrypted.ciphertext[offset : offset + _NONCE_BYTES]
        wrapped_key = encrypted.ciphertext[
            offset + _NONCE_BYTES : offset + _NONCE_BYTES + _WRAPPED_KEY_BYTES
        ]
        data_ciphertext = encrypted.ciphertext[offset + _NONCE_BYTES + _WRAPPED_KEY_BYTES :]
        data_key = AESGCM(wrapping_key).decrypt(
            wrap_nonce,
            wrapped_key,
            _associated_data(user_id, record_id, b"dek"),
        )
        plaintext = AESGCM(data_key).decrypt(
            encrypted.nonce,
            data_ciphertext,
            _associated_data(user_id, record_id, b"data"),
        )
        return plaintext.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError, ValueError):
        raise SecretDecryptionError("Unable to decrypt provider key.") from None
