"""Unit tests for km.producers.base64_32."""

from __future__ import annotations

import base64
from pathlib import Path

from km.producers.base64_32 import Base6432
from km.producers.contract import ManifestEntry, ProduceContext


def _entry(name: str) -> ManifestEntry:
    return ManifestEntry(
        name=name, format="base64-32", group_name="g", manifest_path=Path("/m.yaml")
    )


def _ctx() -> ProduceContext:
    return ProduceContext(manifest={}, current_secrets={}, group={})


def test_decodes_to_32_bytes() -> None:
    [r] = Base6432({}).produce([_entry("account_secret")], _ctx())
    assert r.name == "account_secret"
    assert len(base64.b64decode(r.value)) == 32


def test_returns_in_input_order() -> None:
    out = Base6432({}).produce([_entry("a"), _entry("b")], _ctx())
    assert [r.name for r in out] == ["a", "b"]


def test_each_call_unique() -> None:
    r1 = Base6432({}).produce([_entry("s")], _ctx())
    r2 = Base6432({}).produce([_entry("s")], _ctx())
    assert r1[0].value != r2[0].value
