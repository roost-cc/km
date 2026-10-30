"""Unit tests for PostgresqlPublisher.ensure (role creation) with a fake connection."""

from __future__ import annotations

from pathlib import Path

import pytest

from km.publishers import ManifestRef
from km.publishers.postgresql import PostgresqlPublisher

_CONFIG = {"dev": {"host": "localhost", "database": "postgres", "user": "roost"}}


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn
        self._row = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_a) -> None:
        return None

    def execute(self, stmt, params=None) -> None:
        if params is not None:  # SELECT 1 FROM pg_roles WHERE rolname = %s
            self._row = (1,) if params[0] in self.conn.roles else None
            return
        text = stmt.as_string(None) if hasattr(stmt, "as_string") else str(stmt)
        self.conn.executed.append(text)
        self.conn.roles.add(text.split('"')[1])

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, roles: set[str]) -> None:
        self.roles = set(roles)
        self.executed: list[str] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def close(self) -> None:
        return None


def _pub(conn: _FakeConn) -> PostgresqlPublisher:
    pub = PostgresqlPublisher(_CONFIG)
    pub._admin_connect = lambda env: conn  # type: ignore[method-assign]
    return pub


_REFS = [
    ManifestRef(
        path=Path("synapse.yaml"),
        secrets={"db_password": "pw"},
        section={"dev": {"db_password": "synapse"}},
    ),
    ManifestRef(
        path=Path("infisical.yaml"),
        secrets={"db_password": "pw"},
        section={"dev": {"db_password": "infisical"}},
    ),
]


def test_ensure_creates_missing_roles_only() -> None:
    conn = _FakeConn({"infisical"})
    result = _pub(conn).ensure(env="dev", manifests=_REFS, dry_run=False)
    assert conn.executed == ['CREATE ROLE "synapse" WITH LOGIN']
    assert result.created == ("env=dev role 'synapse'",)
    assert result.failed == 0
    again = _pub(conn).ensure(env="dev", manifests=_REFS, dry_run=False)
    assert again.created == () and len(conn.executed) == 1


def test_ensure_dry_run_creates_nothing() -> None:
    conn = _FakeConn(set())
    result = _pub(conn).ensure(env="dev", manifests=_REFS, dry_run=True)
    assert conn.executed == []
    assert len(result.created) == 2


def test_publish_alters_only_the_selected_names(capsys) -> None:
    conn = _FakeConn({"synapse", "bot"})
    result = _pub(conn).publish(
        manifest_path=Path("synapse.yaml"),
        env="dev",
        secrets={"db_password": "pw", "bot_password": "bpw"},
        section={"dev": {"db_password": "synapse", "bot_password": "bot"}},
        dry_run=True,
        names={"bot_password"},
    )
    assert (result.written, result.skipped, result.failed) == (1, 0, 0)
    err = capsys.readouterr().err
    assert 'ALTER USER "bot"' in err
    assert 'ALTER USER "synapse"' not in err


def test_ensure_without_roles_for_env_does_not_connect() -> None:
    pub = PostgresqlPublisher(_CONFIG)

    def _explode(env):
        raise AssertionError("no connection needed")

    pub._admin_connect = _explode  # type: ignore[method-assign]
    assert pub.ensure(env="test", manifests=_REFS, dry_run=False).created == ()


_LOGIN = {"host": "localhost", "database": "postgres"}


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({**_LOGIN, "login_file": "relative.yaml"}, "login_file must be absolute"),
        ({**_LOGIN, "login_file": 7}, "login_file must be absolute"),
        ({**_LOGIN, "login_file": "/x.yaml", "user": "roost"}, "sets login_file and user/password"),
        (
            {**_LOGIN, "login_file": "/x.yaml", "password": "pw"},
            "sets login_file and user/password",
        ),
        (dict(_LOGIN), "user is required"),
    ],
)
def test_login_file_config_is_checked_at_load(params, message) -> None:
    with pytest.raises(ValueError, match=message):
        PostgresqlPublisher({"dev": params})


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("user: roost\npassword: pw\n", ("roost", "pw")),
        ("user: roost\n", ("roost", None)),
        (None, "does not exist .*km publish km"),
        ("user: [roost\n", "login_file"),
        ("- roost\n", "must be a mapping"),
        ("password: pw\n", "must be a mapping"),
        ("user: ''\npassword: pw\n", "must be a mapping"),
        ("user: roost\npassword: 5\n", "must be a mapping"),
    ],
)
def test_admin_login_comes_from_the_login_file(tmp_path, monkeypatch, content, expected) -> None:
    """The admin connects with the login file's user and password, read at
    connect time; a missing or malformed file fails naming it."""
    login = tmp_path / "postgresql-admin.yaml"
    if content is not None:
        login.write_text(content, encoding="utf-8")
    seen: dict = {}

    def _connect(**kwargs):
        seen.update(kwargs)
        return _FakeConn(set())

    monkeypatch.setattr("km.publishers.postgresql.psycopg.connect", _connect)
    pub = PostgresqlPublisher({"dev": {**_LOGIN, "login_file": str(login)}})
    if isinstance(expected, str):
        with pytest.raises(RuntimeError, match=expected):
            pub._admin_connect("dev")
        assert seen == {}
        return
    pub._admin_connect("dev")
    assert (seen["user"], seen.get("password")) == expected
