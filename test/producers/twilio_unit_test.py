"""Unit tests for km.producers.twilio (HTTP-mocked via pytest-httpx)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from km.producers.contract import (
    ManifestEntry,
    ProduceContext,
    ProducerContractError,
    Secret,
)
from km.producers.twilio import Twilio

SID = "AC0000000000000000000000000000000a"


def _entry(name: str = "text_password") -> ManifestEntry:
    return ManifestEntry(
        name=name,
        format=None,
        group_name="twilio-token",
        manifest_path=Path("/m.yaml"),
    )


def _current(token: str = "old_token") -> dict[str, str]:
    return {"text_userid": SID, "text_password": token}


def _ctx(current_secrets: dict[str, str] | None = None) -> ProduceContext:
    return ProduceContext(manifest={}, current_secrets=current_secrets or {}, group={})


def test_happy_path_secondary_then_promote(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://accounts.twilio.com/v1/AuthTokens/Secondary",
        json={"secondary_auth_token": "secondary_value"},
        status_code=201,
    )
    httpx_mock.add_response(
        method="POST",
        url="https://accounts.twilio.com/v1/AuthTokens/Promote",
        json={"auth_token": "new_primary_value", "sid": SID},
        status_code=200,
    )
    out = Twilio({}).produce([_entry()], _ctx(_current()))
    assert out == [Secret(name="text_password", value="new_primary_value")]


def test_missing_sid_raises() -> None:
    with pytest.raises(ProducerContractError, match="SID not found"):
        Twilio({}).produce([_entry()], _ctx({"text_password": "x"}))


def test_missing_current_token_raises() -> None:
    with pytest.raises(ProducerContractError, match="current auth token"):
        Twilio({}).produce([_entry()], _ctx({"text_userid": SID}))


def test_secondary_failure_aborts_before_promote(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://accounts.twilio.com/v1/AuthTokens/Secondary",
        json={"code": 20003, "message": "Authentication Error"},
        status_code=401,
    )
    with pytest.raises(ProducerContractError, match="secondary-create failed"):
        Twilio({}).produce([_entry()], _ctx(_current()))
    # Only one request fired; promote was not called.
    assert len(httpx_mock.get_requests()) == 1


def test_promote_failure_propagates(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://accounts.twilio.com/v1/AuthTokens/Secondary",
        json={"secondary_auth_token": "x"},
        status_code=201,
    )
    httpx_mock.add_response(
        method="POST",
        url="https://accounts.twilio.com/v1/AuthTokens/Promote",
        json={"code": 20404, "message": "no secondary"},
        status_code=404,
    )
    with pytest.raises(ProducerContractError, match="promote failed"):
        Twilio({}).produce([_entry()], _ctx(_current()))


def test_too_many_entries_raises() -> None:
    with pytest.raises(ProducerContractError, match="exactly one secret"):
        Twilio({}).produce([_entry("a"), _entry("b")], _ctx(_current()))


def test_malformed_sid_rejected_before_any_request(httpx_mock: HTTPXMock) -> None:
    # Defense against path traversal: an attacker-controlled SID must not
    # be interpolated into the API URL. Any deviation from `AC<32-hex>`
    # aborts before httpx fires.
    bad_sids = [
        "../../other-account",
        "AC" + "0" * 31,  # one short
        "AC" + "0" * 33,  # one long
        "BC" + "0" * 32,  # wrong prefix
        "AC0000000000000000000000000000000g",  # non-hex char
    ]
    for bad in bad_sids:
        with pytest.raises(ProducerContractError, match="does not match"):
            Twilio({}).produce(
                [_entry()],
                _ctx({"text_userid": bad, "text_password": "tok"}),
            )
    assert httpx_mock.get_requests() == []


def test_secondary_response_missing_token_raises(httpx_mock: HTTPXMock) -> None:
    # Twilio returns 200/201 but the response body lacks the field that
    # holds the new secondary value. Pin the contract failure so a
    # silently-changed schema can't make us return a sentinel value.
    httpx_mock.add_response(
        method="POST",
        url="https://accounts.twilio.com/v1/AuthTokens/Secondary",
        json={"account_sid": SID},  # no `secondary_auth_token`
        status_code=201,
    )
    with pytest.raises(ProducerContractError, match="missing `secondary_auth_token`"):
        Twilio({}).produce([_entry()], _ctx(_current()))


def test_promote_response_missing_auth_token_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://accounts.twilio.com/v1/AuthTokens/Secondary",
        json={"secondary_auth_token": "x"},
        status_code=201,
    )
    httpx_mock.add_response(
        method="POST",
        url="https://accounts.twilio.com/v1/AuthTokens/Promote",
        json={"account_sid": SID},  # no `auth_token`
        status_code=200,
    )
    with pytest.raises(ProducerContractError, match="missing `auth_token`"):
        Twilio({}).produce([_entry()], _ctx(_current()))


def test_config_overrides_sid_and_token_secret_names(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://accounts.twilio.com/v1/AuthTokens/Secondary",
        json={"secondary_auth_token": "x"},
        status_code=201,
    )
    httpx_mock.add_response(
        method="POST",
        url="https://accounts.twilio.com/v1/AuthTokens/Promote",
        json={"auth_token": "y"},
        status_code=200,
    )
    out = Twilio({"sid_secret": "account_sid", "token_secret": "primary_token"}).produce(
        [_entry("primary_token")],
        _ctx({"account_sid": SID, "primary_token": "old"}),
    )
    assert out == [Secret(name="primary_token", value="y")]


def test_invalid_timeout_rejected_at_construction() -> None:
    with pytest.raises(ProducerContractError, match="timeout_s"):
        Twilio({"timeout_s": "not-a-number"})
