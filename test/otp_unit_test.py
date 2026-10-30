"""Unit tests for km.otp (in-process RFC 6238 TOTP)."""

from __future__ import annotations

import base64

import pytest

from km import otp

# RFC 6238 Appendix B SHA-1 seed "12345678901234567890", base32-encoded.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode("ascii")


@pytest.mark.parametrize(
    ("at", "expected"),
    [(59, "287082"), (1111111109, "081804"), (1234567890, "005924")],
)
def test_rfc6238_sha1_vectors(at: float, expected: str) -> None:
    assert otp.totp(RFC_SECRET, at=at) == expected


@pytest.mark.parametrize(
    "form",
    [
        RFC_SECRET.lower(),
        "gezd gnbv gy3t qojq gezd gnbv gy3t qojq",
        " GEZDGNBVGY3TQOJQ\tGEZDGNBVGY3TQOJQ\n",
        "JBSWY3DPEHPK3PXP",  # 16 chars, needs no padding
    ],
)
def test_base32_normalization(form: str) -> None:
    canonical = "".join(form.split()).upper()
    assert otp.totp(form, at=59) == otp.totp(canonical, at=59)


def test_unpadded_secret_decodes_like_padded() -> None:
    # 10 chars of base32 → needs 6 padding chars.
    assert otp.decode_base32("MFRGGZDFMY") == otp.decode_base32("MFRGGZDFMY======")


@pytest.mark.parametrize("bad", ["not-base32!", "", "   ", "A"])
def test_invalid_secret_raises_without_leaking(bad: str) -> None:
    with pytest.raises(otp.OTPSecretError) as exc:
        otp.totp(bad, at=0)
    if bad.strip():
        assert bad not in str(exc.value)


def test_error_message_omits_secret() -> None:
    secret = "ZZZZZZZZ1111"  # '1' is not in the base32 alphabet
    with pytest.raises(otp.OTPSecretError) as exc:
        otp.decode_base32(secret)
    assert secret not in str(exc.value)
    assert exc.value.__cause__ is None


@pytest.mark.parametrize(
    ("at", "expected"),
    [(0, 30), (29.9, 1), (30, 30), (59.0, 1), (45.5, 15)],
)
def test_seconds_remaining(at: float, expected: int) -> None:
    assert otp.seconds_remaining(at) == expected


class _Recorder:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, value) -> None:
        self.calls.append(value)


def test_fresh_totp_no_wait_with_time_left() -> None:
    sleep, on_wait = _Recorder(), _Recorder()
    times = iter([55.0])
    code, left = otp.fresh_totp(RFC_SECRET, now=lambda: next(times), sleep=sleep, on_wait=on_wait)
    assert (code, left) == ("287082", 5)
    assert sleep.calls == [] and on_wait.calls == []


def test_fresh_totp_waits_for_next_window() -> None:
    events: list = []
    times = iter([27.0, 30.0])
    code, left = otp.fresh_totp(
        RFC_SECRET,
        now=lambda: next(times),
        sleep=lambda s: events.append(("sleep", s)),
        on_wait=lambda r: events.append(("on_wait", r)),
    )
    assert events == [("on_wait", 3), ("sleep", 3)]
    assert code == otp.totp(RFC_SECRET, at=30.0)
    assert code != otp.totp(RFC_SECRET, at=27.0)
    assert left == 30


@pytest.mark.parametrize(
    ("times", "waits", "at"),
    [
        ([25.0], [], 25.0),  # 5 s left: no wait
        ([27.0, 30.0], [3], 30.0),  # slept the full wait
        ([27.0, 29.0, 30.0], [3, 1], 30.0),  # scheduler undersleep
        ([27.0, 27.0, 30.0], [3, 3], 30.0),  # sleep returned at once
        ([27.0, 56.0, 60.0], [3, 4], 60.0),  # overslept into the next window's tail
        ([27.0, 26.0, 31.0], [3, 4], 31.0),  # wall clock stepped back, then on
    ],
)
def test_fresh_totp_rechecks_until_code_has_at_least_five_seconds_left(
    times: list[float], waits: list[int], at: float
) -> None:
    sleep, on_wait = _Recorder(), _Recorder()
    it = iter(times)
    code, left = otp.fresh_totp(RFC_SECRET, now=lambda: next(it), sleep=sleep, on_wait=on_wait)
    assert left >= 5
    assert (code, left) == (otp.totp(RFC_SECRET, at=at), otp.seconds_remaining(at))
    assert on_wait.calls == waits and sleep.calls == waits


def test_fresh_totp_bad_secret_raises() -> None:
    with pytest.raises(otp.OTPSecretError):
        otp.fresh_totp("!!", now=lambda: 10.0, sleep=_Recorder(), on_wait=_Recorder())
