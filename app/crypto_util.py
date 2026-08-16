from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


def _fernet(secret: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(plaintext: str, secret: str) -> str:
    if not plaintext:
        return ""
    token = _fernet(secret).encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"enc:{token}"


def decrypt_secret(stored: str, secret: str) -> str:
    """Decrypt enc:… values; pass through legacy plaintext for migration."""
    if not stored:
        return ""
    if not stored.startswith("enc:"):
        return stored
    try:
        return _fernet(secret).decrypt(stored[4:].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("could not decrypt secret (wrong SESSION_SECRET?)") from exc


def hash_audit_chain(prev_hash: str, payload: str) -> str:
    return hashlib.sha256(f"{prev_hash}|{payload}".encode("utf-8")).hexdigest()
