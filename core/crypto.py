"""
Envelope encryption for everything Kagaz stores.

    master key (.env / KMS)
        └── wraps one data key per member
                └── encrypts that member's files and document details

Deleting a member's wrapped key makes all of their stored blobs unreadable
("crypto-shredding"). In production, swap `wrap_key` / `unwrap_key` for calls
to a cloud KMS so the master key never sits on the app server.

Blob format: 1 version byte | 12-byte nonce | AES-256-GCM ciphertext+tag.
"""

import base64
import binascii
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

VERSION = b"\x01"
NONCE_SIZE = 12
KEY_SIZE = 32


class DecryptionError(Exception):
    pass


def master_key() -> bytes:
    raw = settings.KAGAZ_MASTER_KEY
    if not raw:
        raise ImproperlyConfigured(
            "KAGAZ_MASTER_KEY is not set. Run `python manage.py generate_master_key`."
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except binascii.Error as exc:
        raise ImproperlyConfigured("KAGAZ_MASTER_KEY must be base64.") from exc
    if len(key) != KEY_SIZE:
        raise ImproperlyConfigured("KAGAZ_MASTER_KEY must decode to 32 bytes.")
    return key


def new_data_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def encrypt(key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    nonce = os.urandom(NONCE_SIZE)
    return VERSION + nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def decrypt(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    blob = bytes(blob)
    if blob[:1] != VERSION:
        raise DecryptionError("Unknown blob version.")
    nonce = blob[1 : 1 + NONCE_SIZE]
    try:
        return AESGCM(key).decrypt(nonce, blob[1 + NONCE_SIZE :], aad)
    except Exception as exc:  # InvalidTag: wrong key, wrong aad, or tampered data
        raise DecryptionError("Could not decrypt blob.") from exc


def wrap_key(data_key: bytes, aad: bytes) -> bytes:
    return encrypt(master_key(), data_key, aad)


def unwrap_key(wrapped: bytes, aad: bytes) -> bytes:
    return decrypt(master_key(), wrapped, aad)


def keyed_hash(key: bytes, data: bytes) -> str:
    """Per-member content fingerprint for duplicate detection.

    Keyed so the database can't be used to check whether someone holds a
    known file.
    """
    return hmac.new(key, data, hashlib.sha256).hexdigest()
