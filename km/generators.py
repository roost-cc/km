"""Secret value generators."""

from __future__ import annotations

import base64
import os
import secrets
import uuid

ALPHANUMERIC = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
PASSPHRASE_LENGTH = 32


def random_string(length: int, chars: str) -> str:
    """Uniform random string of ``length`` from ``chars``.

    Rejection sampling on bytes from ``secrets.token_bytes`` guarantees
    a uniform distribution over the alphabet regardless of its size.
    """
    if length < 1:
        raise ValueError("length must be a positive integer")
    n = len(chars)
    if n < 2:
        raise ValueError("alphabet must have at least 2 characters")
    threshold = (256 // n) * n  # largest multiple of n that fits in a byte
    out: list[str] = []
    while len(out) < length:
        for b in secrets.token_bytes(max(length, 32) * 2):
            if len(out) >= length:
                break
            if b < threshold:
                out.append(chars[b % n])
    return "".join(out)


def generate_hex64() -> str:
    """64-char lowercase hex string (32 random bytes)."""
    return os.urandom(32).hex()


def generate_passphrase() -> str:
    """32-char alphanumeric (compat shim — see :func:`random_string`)."""
    return random_string(PASSPHRASE_LENGTH, ALPHANUMERIC)


def generate_uuid() -> str:
    """UUIDv4 string (lowercase, hyphenated, canonical form)."""
    return str(uuid.uuid4())


def generate_base64_32() -> str:
    """Standard base64 (with padding) of 32 random bytes."""
    return base64.b64encode(os.urandom(32)).decode("ascii")
