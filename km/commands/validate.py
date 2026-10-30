"""km validate — schema + freshness + publication checks against one target."""

from __future__ import annotations

import argparse


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "validate",
        help=(
            "verify a target is in a valid state: every manifest is "
            "well-formed, every rotation group is fresh, every secret is "
            "published, and the target holds nothing no manifest declares"
        ),
    )
    p.add_argument("target", help="a target declared under `targets:` in config.yaml")
    p.add_argument(
        "envs",
        nargs="*",
        metavar="env",
        help="environments to check (default: every env the target accepts)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="machine-readable output (default: human)",
    )
    p.add_argument(
        "--schema-only",
        action="store_true",
        help="run only the schema gate; skip freshness and publication",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="include manifest/store value pairs on `differs` rows",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from km.commands._validate import run as impl

    return impl(args)
