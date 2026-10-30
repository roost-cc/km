"""Unit tests for km.producers.ed25519."""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives import serialization

from km.producers.contract import ManifestEntry, ProduceContext
from km.producers.ed25519 import Ed25519


def _entry(name: str, group: str = "g") -> ManifestEntry:
    return ManifestEntry(
        name=name, format="ed25519", group_name=group, manifest_path=Path("/m.yaml")
    )


def _ctx() -> ProduceContext:
    return ProduceContext(manifest={}, current_secrets={})


def test_emits_one_secret_per_entry() -> None:
    p = Ed25519({})
    out = p.produce([_entry("a"), _entry("b")], _ctx())
    assert [s.name for s in out] == ["a", "b"]


def test_value_is_pkcs8_pem_loadable_as_ed25519() -> None:
    p = Ed25519({})
    [s] = p.produce([_entry("x")], _ctx())
    assert s.value.startswith("-----BEGIN PRIVATE KEY-----")
    key = serialization.load_pem_private_key(s.value.encode("ascii"), password=None)
    assert key.__class__.__name__ == "Ed25519PrivateKey"


def test_each_call_unique() -> None:
    p = Ed25519({})
    [a] = p.produce([_entry("k")], _ctx())
    [b] = p.produce([_entry("k")], _ctx())
    assert a.value != b.value


def test_default_output_names_one_per_entry() -> None:
    # No paired public-key derivation anymore — Producer's default
    # output_names returns one name per entry.
    p = Ed25519({})
    names = p.output_names([_entry("a"), _entry("b")], _ctx())
    assert names == ["a", "b"]
