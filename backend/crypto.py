"""Envelope encryption for user API keys.

Each secret gets its own random data key (DEK). The DEK encrypts the secret with AES-256-GCM, bound to
(user_id, provider_id) as associated data so a ciphertext copied to another row will not decrypt.
The DEK itself is wrapped by a key-encryption key (KEK).

KMS seam: wrap_dek / unwrap_dek are the only functions that touch the KEK. To move to a managed KMS,
replace their bodies with the KMS Encrypt/Decrypt (or GenerateDataKey) calls; nothing else changes.
"""
import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend import config


def _keks():
    keks = {}
    for entry in filter(None, config.KEKS.split(",")):
        kid, _, b64 = entry.strip().partition(":")
        key = base64.b64decode(b64)
        if len(key) != 32:
            raise ValueError(f"KEK {kid} must be 32 bytes")
        keks[kid] = key
    return keks


def current_kek_id():
    return config.KEKS.split(",")[0].split(":")[0].strip()


def _seal(key, plaintext, aad):
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def _open(key, blob, aad):
    return AESGCM(key).decrypt(blob[:12], blob[12:], aad)


def wrap_dek(dek):
    kid = current_kek_id()
    return kid, _seal(_keks()[kid], dek, kid.encode())


def unwrap_dek(kek_id, wrapped):
    return _open(_keks()[kek_id], wrapped, kek_id.encode())


def _aad(user_id, provider_id):
    return f"{user_id}\x00{provider_id}".encode()


def encrypt(secret, user_id, provider_id):
    """Returns (ciphertext, wrapped_dek, kek_id)."""
    dek = AESGCM.generate_key(bit_length=256)
    kek_id, wrapped = wrap_dek(dek)
    return _seal(dek, secret.encode(), _aad(user_id, provider_id)), wrapped, kek_id


def decrypt(ciphertext, wrapped_dek, kek_id, user_id, provider_id):
    return _open(unwrap_dek(kek_id, wrapped_dek), ciphertext, _aad(user_id, provider_id)).decode()


def new_kek():
    return base64.b64encode(os.urandom(32)).decode()
