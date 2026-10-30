"""Unit tests for km.producers.uuid."""

from __future__ import annotations

import re
from pathlib import Path

from km.producers.contract import ManifestEntry, ProduceContext
from km.producers.uuid import Uuid

UUID_V4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _entry(name: str) -> ManifestEntry:
    return ManifestEntry(name=name, format="uuid", group_name="g", manifest_path=Path("/m.yaml"))


def _ctx() -> ProduceContext:
    return ProduceContext(manifest={}, current_secrets={}, group={})


def test_generates_uuid_v4() -> None:
    [r] = Uuid({}).produce([_entry("token_secret")], _ctx())
    assert r.name == "token_secret"
    assert UUID_V4_RE.match(r.value)


def test_returns_in_input_order() -> None:
    out = Uuid({}).produce([_entry("a"), _entry("b")], _ctx())
    assert [r.name for r in out] == ["a", "b"]


def test_each_call_unique() -> None:
    r1 = Uuid({}).produce([_entry("s")], _ctx())
    r2 = Uuid({}).produce([_entry("s")], _ctx())
    assert r1[0].value != r2[0].value
