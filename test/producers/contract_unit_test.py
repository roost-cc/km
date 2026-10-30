"""Unit tests for km.producers.contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from km.producers.contract import ManifestEntry, ProducerContractError


def test_manifest_entry_is_frozen() -> None:
    e = ManifestEntry(
        name="api_key",
        format=None,
        group_name="acme",
        manifest_path=Path("/tmp/acme.yaml"),
    )
    with pytest.raises((AttributeError, Exception)):
        e.name = "other"  # type: ignore[misc]


def test_manifest_entry_fields() -> None:
    e = ManifestEntry(
        name="private_key",
        format="ed25519",
        group_name="account-private-key",
        manifest_path=Path("/projects/proj/jwt-keypairs.yaml"),
    )
    assert e.name == "private_key"
    assert e.format == "ed25519"
    assert e.group_name == "account-private-key"
    assert e.manifest_path == Path("/projects/proj/jwt-keypairs.yaml")


def test_manifest_entry_equality() -> None:
    a = ManifestEntry(name="x", format=None, group_name="g", manifest_path=Path("/m.yaml"))
    b = ManifestEntry(name="x", format=None, group_name="g", manifest_path=Path("/m.yaml"))
    assert a == b


def test_producer_contract_error_is_exception() -> None:
    err = ProducerContractError("bad return")
    assert isinstance(err, Exception)
    assert str(err) == "bad return"
