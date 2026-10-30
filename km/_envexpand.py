"""Recursive ``$VAR`` / ``${VAR}`` expansion for parsed yaml structures.

Used by both the config loader and the manifest reader so the
expansion contract is identical: every string value is expanded; an
undefined variable is a hard error (not a silent empty string).

Callers can pass ``skip_keys`` to leave certain top-level keys
untouched — useful when reading manifests, where the ``secrets:``
block holds operator-chosen values that must not be reinterpreted.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

_ENV_VAR_PATTERN = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def expand(value: Any, location: str = "", *, skip_keys: frozenset[str] = frozenset()) -> Any:
    """Return ``value`` with every nested string env-expanded.

    ``location`` is used only to build the error message ("config",
    "manifest <path>", etc. — anchors errors to a useful breadcrumb).
    Keys in ``skip_keys`` are honored only at the *top level*: their
    sub-trees are returned as-is.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k in skip_keys:
                out[k] = v
                continue
            out[k] = _expand(v, f"{location}.{k}" if location else f".{k}")
        return out
    return _expand(value, location)


def undefined(value: Any) -> list[str]:
    """The variables ``value``'s strings reference that are not set, in order."""
    if isinstance(value, str):
        names = (m.group(1) or m.group(2) for m in _ENV_VAR_PATTERN.finditer(value))
        return [n for n in dict.fromkeys(names) if n not in os.environ]
    if isinstance(value, dict):
        children = list(value.values())
    elif isinstance(value, list):
        children = value
    else:
        return []
    return list(dict.fromkeys(n for v in children for n in undefined(v)))


def _expand(value: Any, location: str) -> Any:
    if isinstance(value, str):
        missing: list[str] = []

        def _sub(m: re.Match[str]) -> str:
            name = m.group(1) or m.group(2)
            if name not in os.environ:
                missing.append(name)
                return ""
            return os.environ[name]

        expanded = _ENV_VAR_PATTERN.sub(_sub, value)
        if missing:
            names = ", ".join(f"${n}" for n in dict.fromkeys(missing))
            sys.stderr.write(f"{location}: undefined env var(s): {names}\n")
            raise SystemExit(2)
        return expanded
    if isinstance(value, dict):
        return {k: _expand(v, f"{location}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, f"{location}[{i}]") for i, v in enumerate(value)]
    return value


__all__ = ["expand", "undefined"]
