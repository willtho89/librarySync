import base64
import hashlib

import librarysync.core.security
import pytest
from cryptography.fernet import Fernet


def _make_test_fernet():
    digest = hashlib.sha256("test-secret-key-for-testing-only".encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


@pytest.fixture(autouse=True)
def stub_fernet(monkeypatch):
    test_fernet = _make_test_fernet()
    monkeypatch.setattr(librarysync.core.security, "_get_fernet", lambda: test_fernet)
    yield


def test_encrypt_decrypt_roundtrip():
    original = "my-secret-token-12345"
    encrypted = librarysync.core.security.encrypt_value(original)
    decrypted = librarysync.core.security.decrypt_value(encrypted)
    assert decrypted == original


def test_encrypt_produces_different_output_than_input():
    value = "sensitive-data"
    encrypted = librarysync.core.security.encrypt_value(value)
    assert encrypted != value


def test_encrypt_produces_different_ciphertext_each_time():
    value = "test-secret"
    encrypted1 = librarysync.core.security.encrypt_value(value)
    encrypted2 = librarysync.core.security.encrypt_value(value)
    assert encrypted1 != encrypted2
    assert librarysync.core.security.decrypt_value(encrypted1) == value
    assert librarysync.core.security.decrypt_value(encrypted2) == value


def test_decrypt_invalid_raises_value_error():
    with pytest.raises(ValueError, match="Invalid encrypted value"):
        librarysync.core.security.decrypt_value("not-a-valid-encrypted-string")


def test_encrypt_empty_string():
    original = ""
    encrypted = librarysync.core.security.encrypt_value(original)
    decrypted = librarysync.core.security.decrypt_value(encrypted)
    assert decrypted == original


def test_encrypt_unicode():
    original = "日本語テスト"
    encrypted = librarysync.core.security.encrypt_value(original)
    decrypted = librarysync.core.security.decrypt_value(encrypted)
    assert decrypted == original


def test_encrypt_preserves_special_characters():
    original = "!@#$%^&*()_+-=[]{}|;':\",./<>?"
    encrypted = librarysync.core.security.encrypt_value(original)
    decrypted = librarysync.core.security.decrypt_value(encrypted)
    assert decrypted == original


def test_encrypt_long_value():
    original = "x" * 10000
    encrypted = librarysync.core.security.encrypt_value(original)
    decrypted = librarysync.core.security.decrypt_value(encrypted)
    assert decrypted == original
