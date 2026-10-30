"""In-process RFC 6238 TOTP (HMAC-SHA1, 30-second step, 6 digits, T0 = 0).

These are the standard authenticator-app defaults, so codes match what km
generated before for existing accounts. The secret never leaves the
process and never appears in an exception message.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
from collections.abc import Callable


class OTPSecretError(ValueError):
    """The TOTP secret is unusable. The message never contains the secret."""


def decode_base32(secret: str) -> bytes:
    """Decode a base32 secret, tolerating whitespace, lowercase and missing padding."""
    normalized = "".join(secret.split()).upper().rstrip("=")
    normalized += "=" * (-len(normalized) % 8)
    try:
        key = base64.b32decode(normalized)
    except (binascii.Error, ValueError):
        raise OTPSecretError("mfa secret is not valid base32") from None
    if not key:
        raise OTPSecretError("mfa secret is not valid base32")
    return key


def totp(secret: str, *, at: float, step: int = 30, digits: int = 6) -> str:
    """Return the TOTP code for ``secret`` at Unix time ``at``."""
    key = decode_base32(secret)
    counter = int(at // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def seconds_remaining(at: float, step: int = 30) -> int:
    """Return how many whole seconds the code for time ``at`` stays valid."""
    return step - int(at % step)


# Below this many seconds left, wait for the next window rather than hand
# the operator a code that expires while they paste it.
_MIN_SECONDS_LEFT = 5


def fresh_totp(
    secret: str,
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    on_wait: Callable[[int], None],
) -> tuple[str, int]:
    """Return ``(code, seconds_left)`` for a code with at least 5 s left.

    With fewer than 5 s left in the current window, call ``on_wait(remaining)``,
    sleep until the next window and check again: a short sleep or a clock
    change can leave the code still too close to expiry.
    """
    t = now()
    remaining = seconds_remaining(t)
    while remaining < _MIN_SECONDS_LEFT:
        on_wait(remaining)
        sleep(remaining)
        t = now()
        remaining = seconds_remaining(t)
    return totp(secret, at=t), remaining
