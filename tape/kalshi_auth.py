"""Kalshi request signing.

Message = timestamp_ms + METHOD + path (from the API root, no query string).
RSA keys sign with RSA-PSS over SHA-256 (salt = digest length); Ed25519 keys sign directly.
Source: docs.kalshi.com/getting_started/quick_start_authenticated_requests (checked 2026-10-06).
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def load_private_key(path: Path | str):
    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


def signing_message(timestamp_ms: str, method: str, path: str) -> bytes:
    return f"{timestamp_ms}{method.upper()}{path.split('?')[0]}".encode("utf-8")


def sign(private_key, timestamp_ms: str, method: str, path: str) -> str:
    message = signing_message(timestamp_ms, method, path)
    if isinstance(private_key, Ed25519PrivateKey):
        signature = private_key.sign(message)
    else:
        signature = private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return base64.b64encode(signature).decode("utf-8")


def auth_headers(key_id: str, private_key, method: str, path: str, now_ms: int | None = None) -> dict[str, str]:
    timestamp = str(now_ms if now_ms is not None else int(time.time() * 1000))
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": sign(private_key, timestamp, method, path),
    }
