"""Unit tests for provider-key envelope encryption."""

import base64
from uuid import uuid4

import pytest

from app.core.crypto import (
    CryptoConfigurationError,
    EncryptedSecret,
    SecretDecryptionError,
    decrypt_secret,
    encrypt_secret,
)
from tests.conftest import TEST_MASTER_KEY


def test_envelope_round_trip_and_per_record_nonce() -> None:
    user_id = uuid4()
    record_id = uuid4()
    secret = "sk-test-known-provider-key"

    encrypted = encrypt_secret(
        secret, user_id=user_id, record_id=record_id, master_key=TEST_MASTER_KEY
    )

    assert len(encrypted.nonce) == 12
    assert encrypted.ciphertext != secret.encode()
    assert (
        decrypt_secret(encrypted, user_id=user_id, record_id=record_id, master_key=TEST_MASTER_KEY)
        == secret
    )


def test_tampering_is_rejected_without_echoing_plaintext() -> None:
    user_id = uuid4()
    record_id = uuid4()
    secret = "sk-test-never-echo-this"
    encrypted = encrypt_secret(
        secret, user_id=user_id, record_id=record_id, master_key=TEST_MASTER_KEY
    )
    tampered_ciphertext = bytearray(encrypted.ciphertext)
    tampered_ciphertext[-1] ^= 1

    with pytest.raises(SecretDecryptionError) as error:
        decrypt_secret(
            EncryptedSecret(bytes(tampered_ciphertext), encrypted.nonce),
            user_id=user_id,
            record_id=record_id,
            master_key=TEST_MASTER_KEY,
        )
    assert secret not in str(error.value)


def test_wrong_master_key_and_wrong_record_context_fail() -> None:
    user_id = uuid4()
    record_id = uuid4()
    encrypted = encrypt_secret(
        "sk-test-never-echo-this", user_id=user_id, record_id=record_id, master_key=TEST_MASTER_KEY
    )
    wrong_key = base64.urlsafe_b64encode(b"x" * 32).decode("ascii")

    with pytest.raises(SecretDecryptionError):
        decrypt_secret(encrypted, user_id=user_id, record_id=record_id, master_key=wrong_key)
    with pytest.raises(SecretDecryptionError):
        decrypt_secret(encrypted, user_id=uuid4(), record_id=record_id, master_key=TEST_MASTER_KEY)


def test_master_key_must_decode_to_32_bytes() -> None:
    with pytest.raises(CryptoConfigurationError):
        encrypt_secret("key", user_id=uuid4(), record_id=uuid4(), master_key="not-a-master-key")
