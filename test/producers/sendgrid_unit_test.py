"""Unit tests for km.producers.sendgrid (HTTP-mocked via pytest-httpx)."""

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
from km.producers.sendgrid import Sendgrid


def _entry(name: str = "email_password") -> ManifestEntry:
    return ManifestEntry(
        name=name,
        format=None,
        group_name="sendgrid-key",
        manifest_path=Path("/m.yaml"),
    )


def _ctx(
    *, current_secrets: dict[str, str] | None = None, revoke_old: bool = False
) -> ProduceContext:
    return ProduceContext(
        manifest={},
        current_secrets=current_secrets or {},
        group={},
        revoke_old=revoke_old,
    )


def _list_keys(*, prior_name: str | None = "roost-rotated-2026-01-01T00-00-00Z") -> dict:
    if prior_name is None:
        return {"result": []}
    return {
        "result": [
            {"api_key_id": "K_OLD", "name": prior_name},
        ]
    }


def test_happy_path_creates_with_prior_scopes(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys",
        json=_list_keys(),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys/K_OLD",
        json={"api_key_id": "K_OLD", "scopes": ["mail.send", "stats.read"]},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.sendgrid.com/v3/api_keys",
        json={"api_key": "SG.NEW", "api_key_id": "K_NEW"},
        status_code=201,
    )
    out = Sendgrid({}).produce([_entry()], _ctx(current_secrets={"email_password": "SG.OLD"}))
    assert out == [Secret(name="email_password", value="SG.NEW")]
    create_req = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "POST" and str(r.url).endswith("/api_keys")
    )
    body = create_req.read().decode("utf-8")
    assert "mail.send" in body
    assert "stats.read" in body


def test_missing_current_key_raises() -> None:
    with pytest.raises(ProducerContractError, match="current API key"):
        Sendgrid({}).produce([_entry()], _ctx())


def test_no_prior_key_refuses_to_mint(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys",
        json=_list_keys(prior_name=None),
    )
    with pytest.raises(ProducerContractError, match="cannot infer scopes"):
        Sendgrid({}).produce([_entry()], _ctx(current_secrets={"email_password": "SG.OLD"}))


def test_full_access_prior_key_emits_specific_error(httpx_mock: HTTPXMock) -> None:
    # SendGrid represents Full Access keys with `scopes: []`. The producer
    # must distinguish empty from missing and emit a Full-Access-specific
    # error so the operator knows to replace it with a Restricted key.
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys",
        json=_list_keys(),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys/K_OLD",
        json={"api_key_id": "K_OLD", "scopes": []},
    )
    with pytest.raises(ProducerContractError, match="Full Access"):
        Sendgrid({}).produce([_entry()], _ctx(current_secrets={"email_password": "SG.OLD"}))


def test_revoke_old_deletes_prior_key(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys",
        json=_list_keys(),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys/K_OLD",
        json={"api_key_id": "K_OLD", "scopes": ["mail.send"]},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.sendgrid.com/v3/api_keys",
        json={"api_key": "SG.NEW", "api_key_id": "K_NEW"},
        status_code=201,
    )
    httpx_mock.add_response(
        method="DELETE",
        url="https://api.sendgrid.com/v3/api_keys/K_OLD",
        status_code=204,
    )
    Sendgrid({}).produce(
        [_entry()],
        _ctx(current_secrets={"email_password": "SG.OLD"}, revoke_old=True),
    )
    assert any(
        r.method == "DELETE" and str(r.url).endswith("/api_keys/K_OLD")
        for r in httpx_mock.get_requests()
    )


def test_default_no_revoke_keeps_prior_key(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys",
        json=_list_keys(),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys/K_OLD",
        json={"api_key_id": "K_OLD", "scopes": ["mail.send"]},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.sendgrid.com/v3/api_keys",
        json={"api_key": "SG.NEW", "api_key_id": "K_NEW"},
        status_code=201,
    )
    Sendgrid({}).produce([_entry()], _ctx(current_secrets={"email_password": "SG.OLD"}))
    assert not any(r.method == "DELETE" for r in httpx_mock.get_requests())


def test_picks_most_recent_dated_prior_key(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys",
        json={
            "result": [
                {"api_key_id": "K_OLDER", "name": "roost-rotated-2025-01-01T00-00-00Z"},
                {"api_key_id": "K_NEWER", "name": "roost-rotated-2026-01-01T00-00-00Z"},
            ]
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys/K_NEWER",
        json={"api_key_id": "K_NEWER", "scopes": ["mail.send"]},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.sendgrid.com/v3/api_keys",
        json={"api_key": "SG.NEW", "api_key_id": "K_NEW"},
        status_code=201,
    )
    Sendgrid({}).produce([_entry()], _ctx(current_secrets={"email_password": "SG.OLD"}))


def test_picks_most_recent_same_day_prior_key(httpx_mock: HTTPXMock) -> None:
    # Two keys minted on the same day. With seconds in the suffix the
    # later one sorts higher and gets picked deterministically.
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys",
        json={
            "result": [
                {"api_key_id": "K_AM", "name": "roost-rotated-2026-04-27T09-00-00Z"},
                {"api_key_id": "K_PM", "name": "roost-rotated-2026-04-27T17-30-15Z"},
            ]
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys/K_PM",
        json={"api_key_id": "K_PM", "scopes": ["mail.send"]},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.sendgrid.com/v3/api_keys",
        json={"api_key": "SG.NEW", "api_key_id": "K_NEW"},
        status_code=201,
    )
    Sendgrid({}).produce([_entry()], _ctx(current_secrets={"email_password": "SG.OLD"}))


def test_too_many_entries_raises() -> None:
    with pytest.raises(ProducerContractError, match="exactly one secret"):
        Sendgrid({}).produce(
            [_entry("a"), _entry("b")],
            _ctx(current_secrets={"email_password": "x"}),
        )


def test_create_response_missing_api_key_raises(httpx_mock: HTTPXMock) -> None:
    # SendGrid returns 201 but the body omits `api_key` (the full key is
    # only returned once at creation; if it's missing we cannot recover).
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys",
        json=_list_keys(),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys/K_OLD",
        json={"api_key_id": "K_OLD", "scopes": ["mail.send"]},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.sendgrid.com/v3/api_keys",
        json={"api_key_id": "K_NEW"},  # no `api_key`
        status_code=201,
    )
    with pytest.raises(ProducerContractError, match="missing `api_key`"):
        Sendgrid({}).produce([_entry()], _ctx(current_secrets={"email_password": "SG.OLD"}))


def test_create_failure_propagates(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys",
        json=_list_keys(),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys/K_OLD",
        json={"api_key_id": "K_OLD", "scopes": ["mail.send"]},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.sendgrid.com/v3/api_keys",
        status_code=403,
        text="Forbidden",
    )
    with pytest.raises(ProducerContractError, match="create api_key failed"):
        Sendgrid({}).produce([_entry()], _ctx(current_secrets={"email_password": "SG.OLD"}))


def test_config_key_secret_override(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys",
        json=_list_keys(),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://api.sendgrid.com/v3/api_keys/K_OLD",
        json={"api_key_id": "K_OLD", "scopes": ["mail.send"]},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.sendgrid.com/v3/api_keys",
        json={"api_key": "SG.NEW", "api_key_id": "K_NEW"},
        status_code=201,
    )
    out = Sendgrid({"key_secret": "sg_api_key"}).produce(
        [_entry()], _ctx(current_secrets={"sg_api_key": "SG.OLD"})
    )
    assert out == [Secret(name="email_password", value="SG.NEW")]


def test_invalid_timeout_rejected_at_construction() -> None:
    with pytest.raises(ProducerContractError, match="timeout_s"):
        Sendgrid({"timeout_s": "not-a-number"})
