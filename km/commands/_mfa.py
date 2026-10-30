"""`km mfa` implementation.

Reads ``mfa`` from the effective login (F-56 D2) of
``<project>/<directories.manifests>/<slug>.yaml``, computes the current
TOTP in-process, writes only the code to stdout and copies it to the
clipboard. Status lines go to stderr. The secret is never printed or put
in an error message.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable

import yaml

from km import clipboard, logins, otp
from km.config import load as load_config
from km.config import resolve_config_path
from km.manifest_io import effective_login
from km.project import resolve_project

_HAS_MFA = logins.has_string("mfa")
_KIND = "logins with mfa"


def _error(msg: str) -> int:
    sys.stderr.write(f"error: {msg}\n")
    return 1


def run(
    args: argparse.Namespace,
    *,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    copy: Callable[[str], bool] = clipboard.copy,
) -> int:
    cfg = load_config(resolve_config_path(args.config))
    project = resolve_project()
    manifests_dir = logins.manifests_dir(cfg, project)

    slug = args.slug
    path = logins.manifest_path(manifests_dir, slug)
    if path is None:
        return _error(logins.unknown_message(slug, manifests_dir, _HAS_MFA, _KIND))

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
    if login is None or not _HAS_MFA(login):
        return _error(logins.unknown_message(slug, manifests_dir, _HAS_MFA, _KIND))

    try:
        code, seconds_left = otp.fresh_totp(
            login["mfa"],
            now=now,
            sleep=sleep,
            on_wait=lambda remaining: sys.stderr.write(f"waiting {remaining}s for the next code\n"),
        )
    except otp.OTPSecretError as e:
        return _error(f"{path}: {e}")

    sys.stdout.write(f"{code}\n")
    sys.stdout.flush()
    status = f"{slug}: {seconds_left}s left"
    if args.no_copy:
        status += ", copy skipped (--no-copy)"
    elif copy(code):
        status += ", copied to clipboard"
    else:
        status += ", not copied (no clipboard available)"
    sys.stderr.write(f"{status}\n")
    return 0
