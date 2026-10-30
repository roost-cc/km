"""Unit tests for km.selection (`km publish --only` selectors)."""

from __future__ import annotations

import pytest

from km.selection import SelectionError, parse_selector, resolve

PARSED = {
    "secrets": {"a": "1", "b": "2", "c": "3"},
    "rotation": {
        "groups": {
            "pair": {"secrets": [{"name": "a"}, {"name": "b"}]},
            # A group named like a secret wins, as `km rotate --force` looks up groups.
            "c": {"secrets": [{"name": "c"}, {"name": "b"}]},
        }
    },
}
MANIFESTS = {"svc": (PARSED, PARSED["secrets"]), "apple-user": ({}, {"password": "p"})}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("svc", ("svc", None)),
        ("svc:pair", ("svc", "pair")),
        ("apple-user:password", ("apple-user", "password")),
    ],
)
def test_parse_selector(raw, expected) -> None:
    assert parse_selector(raw) == expected


@pytest.mark.parametrize("raw", ["", ":a", "svc:", "svc:a:b"])
def test_parse_selector_rejects_malformed(raw) -> None:
    with pytest.raises(SelectionError, match="expected <slug>"):
        parse_selector(raw)


def test_resolve_group_then_secret_then_whole_manifest() -> None:
    assert resolve(["svc:pair"], MANIFESTS) == {"svc": {"a", "b"}}
    assert resolve(["svc:a"], MANIFESTS) == {"svc": {"a"}}
    assert resolve(["svc:c"], MANIFESTS) == {"svc": {"b", "c"}}
    assert resolve(["svc"], MANIFESTS) == {"svc": {"a", "b", "c"}}
    assert resolve(["svc:a", "apple-user"], MANIFESTS) == {
        "svc": {"a"},
        "apple-user": {"password"},
    }


def test_resolve_unknown_slug_names_the_manifests() -> None:
    with pytest.raises(SelectionError, match=r"no manifest 'nope' \(have: apple-user, svc\)"):
        resolve(["nope:a"], MANIFESTS)


def test_resolve_unknown_name_lists_groups_and_secrets() -> None:
    with pytest.raises(SelectionError) as exc:
        resolve(["svc:zz"], MANIFESTS)
    assert "groups: c, pair; secrets: a, b, c" in str(exc.value)
    with pytest.raises(SelectionError, match="groups: -; secrets: password"):
        resolve(["apple-user:zz"], MANIFESTS)


def test_resolve_a_slug_the_target_does_not_list_is_a_usage_error() -> None:
    """F-56 D3: a target publishes only the manifests it lists."""
    with pytest.raises(SelectionError) as exc:
        resolve(["login-only"], MANIFESTS, target="local", unlisted={"login-only"})
    assert str(exc.value) == (
        "--only 'login-only': target 'local' does not publish manifest 'login-only' "
        "(its `manifests:` lists: apple-user, svc)"
    )
    with pytest.raises(SelectionError, match="does not publish manifest 'login-only'"):
        resolve(["svc", "login-only:password"], MANIFESTS, target="local", unlisted={"login-only"})
