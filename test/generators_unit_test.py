"""Unit tests for km.generators — byte-level parity contracts."""

from __future__ import annotations

import base64
import re
import uuid

from km.generators import (
    ALPHANUMERIC,
    PASSPHRASE_LENGTH,
    generate_base64_32,
    generate_hex64,
    generate_passphrase,
    generate_uuid,
)


def test_hex64_is_lowercase_64_hex_chars() -> None:
    v = generate_hex64()
    assert len(v) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", v)


def test_hex64_decodes_to_32_bytes() -> None:
    raw = bytes.fromhex(generate_hex64())
    assert len(raw) == 32


def test_passphrase_length() -> None:
    v = generate_passphrase()
    assert len(v) == PASSPHRASE_LENGTH


def test_passphrase_charset() -> None:
    for _ in range(50):
        v = generate_passphrase()
        for c in v:
            assert c in ALPHANUMERIC


def test_passphrase_rejection_sampling_rejects_high_bytes(monkeypatch) -> None:
    # Feed a sequence that starts with a rejected byte (>= 248) then
    # accepted ones, verifying the loop actually rejects the high byte.
    # The bytestring is long enough that the second call is never needed.
    fake_bytes = bytes([255] + list(range(1, 256)) + [0] * 64)

    def fake_token_bytes(n: int) -> bytes:
        return fake_bytes[:n]

    monkeypatch.setattr("km.generators.secrets.token_bytes", fake_token_bytes)
    v = generate_passphrase()
    assert len(v) == PASSPHRASE_LENGTH
    # First byte (255) should have been rejected, so output starts at byte 1.
    assert v[0] == ALPHANUMERIC[1 % 62]


def test_uuid_is_v4_canonical() -> None:
    v = generate_uuid()
    assert isinstance(v, str)
    # canonical lowercase hyphenated form
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        v,
    )
    # round-trip through uuid.UUID to prove validity
    uuid.UUID(v)


def test_base64_32_decodes_to_32_bytes() -> None:
    v = generate_base64_32()
    raw = base64.b64decode(v, validate=True)
    assert len(raw) == 32
    # standard (padded) base64 yields a 44-char string for 32 input bytes
    assert len(v) == 44
