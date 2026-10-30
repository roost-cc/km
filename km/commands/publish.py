"""km publish — write the manifests' secrets to every destination of a target."""

from __future__ import annotations

import argparse


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "publish",
        help="write manifest secrets to a target's destinations (ensure, publish, apply)",
    )
    p.add_argument("target", help="a target declared under `targets:` in config.yaml")
    p.add_argument(
        "envs",
        nargs="*",
        metavar="env",
        help="environments to publish (default: every env the target accepts)",
    )
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--all",
        action="store_true",
        help="publish every secret of every manifest",
    )
    selection.add_argument(
        "--only",
        action="append",
        metavar="SLUG[:NAME]",
        help=(
            "publish one manifest (SLUG) or one of its rotation groups or secrets "
            "(SLUG:NAME, a group first, else a secret); repeatable"
        ),
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="actually mint and write (default: dry-run)",
    )
    p.epilog = "exactly one of --all or --only is required: writing everything is said out loud"
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from km.commands._publish import run as impl

    return impl(args)
