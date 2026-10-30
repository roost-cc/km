"""`km login` implementation.

Resolves ``<project>/<directories.manifests>/<slug>.yaml``'s effective
login (F-56 D2) the way ``km mfa`` does, then hands it to the login menu.
No field's value is ever printed.
"""

from __future__ import annotations

import argparse
import sys

import yaml

from km import login_menu, logins
from km.config import load as load_config
from km.config import resolve_config_path
from km.manifest_io import effective_login
from km.project import resolve_project


def _error(msg: str) -> int:
    sys.stderr.write(f"error: {msg}\n")
    return 1


def run(args: argparse.Namespace, **menu_io) -> int:
    """Run ``km login``. ``menu_io`` overrides the menu's injected I/O (tests)."""
    cfg = load_config(resolve_config_path(args.config))
    project = resolve_project()
    manifests_dir = logins.manifests_dir(cfg, project)

    slug = args.slug
    path = logins.manifest_path(manifests_dir, slug)
    if path is None:
        return _error(logins.unknown_message(slug, manifests_dir, None, "logins"))

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return _error(f"{path}: cannot read file")
    except yaml.YAMLError:
        return _error(f"{path}: yaml parse error")
    if not isinstance(data, dict):
        return _error(f"{path}: not a YAML mapping")
    try:
        login = effective_login(data, manifests_dir)
    except ValueError as e:
        return _error(f"{path}: {e}")
    if login is None:
        return _error(logins.unknown_message(slug, manifests_dir, None, "logins"))
    if not login_menu.usable_fields(login):
        return _error(f"{path}: no non-empty string `login.id`, `login.mfa` or `secrets.password`")

    try:
        login_menu.run_login_menu(login, slug, open_browser=not args.no_open, **menu_io)
    except login_menu.NotATTY:
        return _error("km login needs an interactive terminal")
    return 0
