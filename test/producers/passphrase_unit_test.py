"""Unit tests for km.producers.passphrase."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from km.producers.contract import (
    ManifestEntry,
    ProduceContext,
    ProducerContractError,
)
from km.producers.passphrase import Passphrase

ALPHANUMERIC_RE = re.compile(r"^[A-Za-z0-9]+$")
HEX_RE = re.compile(r"^[0-9a-f]+$")
NUMERIC_RE = re.compile(r"^[0-9]+$")


def _entry(name: str, fmt=None) -> ManifestEntry:
    return ManifestEntry(
        name=name,
        format=fmt if fmt is not None else "passphrase",
        group_name="g",
        manifest_path=Path("/m.yaml"),
    )


def _ctx() -> ProduceContext:
    return ProduceContext(manifest={}, current_secrets={}, group={})


def test_32_char_alnum_default() -> None:
    [r] = Passphrase({}).produce([_entry("key_provider_passphrase")], _ctx())
    assert r.name == "key_provider_passphrase"
    assert len(r.value) == 32
    assert ALPHANUMERIC_RE.match(r.value)


def test_returns_in_input_order() -> None:
    out = Passphrase({}).produce([_entry("a"), _entry("b")], _ctx())
    assert [r.name for r in out] == ["a", "b"]


def test_each_call_unique() -> None:
    r1 = Passphrase({}).produce([_entry("s")], _ctx())
    r2 = Passphrase({}).produce([_entry("s")], _ctx())
    assert r1[0].value != r2[0].value


def test_format_mapping_overrides_length() -> None:
    [r] = Passphrase({}).produce([_entry("p", {"length": 64})], _ctx())
    assert len(r.value) == 64
    assert ALPHANUMERIC_RE.match(r.value)


def test_format_mapping_named_alphabet_numeric() -> None:
    [r] = Passphrase({}).produce([_entry("p", {"length": 6, "alphabet": "numeric"})], _ctx())
    assert len(r.value) == 6
    assert NUMERIC_RE.match(r.value)


def test_format_mapping_named_alphabet_hex() -> None:
    [r] = Passphrase({}).produce([_entry("p", {"length": 40, "alphabet": "hex"})], _ctx())
    assert len(r.value) == 40
    assert HEX_RE.match(r.value)


def test_format_mapping_explicit_alphabet() -> None:
    [r] = Passphrase({}).produce([_entry("p", {"length": 20, "alphabet": "ab"})], _ctx())
    assert len(r.value) == 20
    assert all(c in "ab" for c in r.value)


def test_format_mapping_bad_alphabet_rejected() -> None:
    with pytest.raises(ProducerContractError):
        Passphrase({}).produce([_entry("p", {"alphabet": "x"})], _ctx())


def test_format_mapping_bad_length_rejected() -> None:
    with pytest.raises(ProducerContractError):
        Passphrase({}).produce([_entry("p", {"length": 0})], _ctx())


def test_format_must_be_string_or_mapping() -> None:
    with pytest.raises(ProducerContractError):
        Passphrase({}).produce([_entry("p", 42)], _ctx())
