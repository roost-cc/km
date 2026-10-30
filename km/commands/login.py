"""km login — open a login's page and copy its fields on keypress."""

from __future__ import annotations

import argparse


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "login",
        help=(
            "open the login url of manifest <slug> and copy username, password "
            "or MFA token on keypress"
        ),
    )
    p.add_argument(
        "slug",
        help="slug of a manifest with a `login:` block (manifests/<slug>.yaml)",
    )
    p.add_argument(
        "--no-open",
        action="store_true",
        help="do not open the login url in the browser",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from km.commands._login import run as impl

    return impl(args)
