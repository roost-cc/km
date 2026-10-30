"""Unit tests for km.producers.static."""

from __future__ import annotations

from pathlib import Path

import pytest

from km.producers.contract import (
    ManifestEntry,
    ProduceContext,
    ProducerContractError,
    Secret,
)
from km.producers.static import Static


def _entry(name: str, group: str = "g") -> ManifestEntry:
    return ManifestEntry(name=name, format=None, group_name=group, manifest_path=Path("/m.yaml"))


def _ctx(current_secrets: dict[str, str] | None = None) -> ProduceContext:
    return ProduceContext(manifest={}, current_secrets=current_secrets or {}, group={})


def test_static_passes_through_current_values() -> None:
    out = Static({}).produce([_entry("a"), _entry("b")], _ctx({"a": "AAA", "b": "BBB"}))
    assert out == [Secret(name="a", value="AAA"), Secret(name="b", value="BBB")]


def test_static_raises_on_missing_current_value() -> None:
    with pytest.raises(ProducerContractError, match="no current value"):
        Static({}).produce([_entry("a")], _ctx())


def test_static_raises_on_empty_current_value() -> None:
    with pytest.raises(ProducerContractError, match="no current value"):
        Static({}).produce([_entry("a")], _ctx({"a": ""}))


def test_static_revoke_old_is_a_no_op() -> None:
    out = Static({}).produce(
        [_entry("a")],
        ProduceContext(manifest={}, current_secrets={"a": "X"}, group={}, revoke_old=True),
    )
    assert out == [Secret(name="a", value="X")]
