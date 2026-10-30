"""km mfa — print and copy a login's current TOTP code."""

from __future__ import annotations

import argparse


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "mfa",
        help="print the current TOTP of manifest <slug>'s login and copy it to the clipboard",
    )
    p.add_argument(
        "slug",
        help="slug of a manifest whose login has `mfa` (manifests/<slug>.yaml)",
    )
    p.add_argument(
        "--no-copy",
        action="store_true",
        help="print the code without copying it to the clipboard",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from km.commands._mfa import run as impl

    return impl(args)
