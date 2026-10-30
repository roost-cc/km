"""km rotate — mint stale or forced groups into the manifest; writes no store."""

from __future__ import annotations

import argparse


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "rotate",
        help="rotate stale (or forced) groups; with <slug>, one manifest",
    )
    p.add_argument(
        "slug",
        nargs="?",
        help="slug of the manifest to rotate (manifests/<slug>.yaml); omit to sweep every manifest",
    )
    p.add_argument(
        "--project",
        default=None,
        help="project name (subdir of credentials_root); only used for batch mode (default: walk up from cwd)",
    )
    p.add_argument(
        "--env",
        default=None,
        help="environment to rotate against (default: project's defaultEnvironment)",
    )
    p.add_argument(
        "--force",
        help="force-rotate the named group (or single-secret matching <name>)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the rotation plan without minting/writing new values",
    )
    revoke = p.add_mutually_exclusive_group()
    revoke.add_argument(
        "--revoke-old",
        dest="revoke_old",
        action="store_true",
        help="ask each producer to invalidate the old value upstream after minting "
        "(use for breach-response posture)",
    )
    revoke.add_argument(
        "--no-revoke-old",
        dest="revoke_old",
        action="store_false",
        help="leave the old upstream value valid (the routine-rotation default)",
    )
    p.set_defaults(func=run, revoke_old=False)


def run(args: argparse.Namespace) -> int:
    from km.commands._rotate import run as impl

    return impl(args)
