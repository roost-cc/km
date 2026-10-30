"""Shared test fixtures and integration-marker wiring."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

TEST_DIR = Path(__file__).resolve().parent
FIXTURES = TEST_DIR / "fixtures" / "manifests"
DOTENV_PATH = TEST_DIR / ".env"


def _load_dotenv(path: Path) -> None:
    """Populate ``os.environ`` from a ``KEY=VALUE`` file.

    Tiny KEY=VALUE parser — no shell expansion, no multi-line values.
    Lines beginning with ``#`` (after optional whitespace) and blank lines
    are skipped; surrounding single or double quotes on the value are
    stripped. Existing env vars are **not** overridden, so an explicit
    ``export FOO=...`` in the caller's shell (or a CI secret) always wins
    over the local file.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        os.environ.setdefault(key, value)


# Load before any test module is imported — module-level skip gates
# (e.g. `pytest.skip("INFISICAL_TEST_TOKEN not set", allow_module_level=True)`)
# evaluate at import time, so the file has to be in os.environ before
# collection runs.
_load_dotenv(DOTENV_PATH)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


def pytest_collection_modifyitems(config, items):
    """Auto-mark *_int_test.py files as integration so `-m 'not integration'` skips them."""
    for item in items:
        filename = Path(item.fspath).name
        if filename.endswith("_int_test.py"):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def _isolate_project_location(monkeypatch):
    """Keep every test independent of the developer's $KM_PROJECT_DIR and -C/--dir."""
    from km import location

    monkeypatch.delenv(location.ENV_VAR, raising=False)
    location.set_cli_dir(None)
    yield
    location.set_cli_dir(None)


@pytest.fixture(autouse=True)
def exit_hooks(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record the login menu's exit hook instead of clearing the real clipboard at exit."""
    from km import login_menu

    registered: list = []
    monkeypatch.setattr(login_menu, "_exit_clear_registered", False)
    monkeypatch.setattr(login_menu, "_at_exit", registered.append)
    return registered
