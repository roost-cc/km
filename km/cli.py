"""argparse dispatcher for the `km` CLI."""

from __future__ import annotations

import argparse
import sys

from km import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="km",
        description="Manifest-driven key-management CLI: rotate secrets and publish them to their stores.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "path to config.yaml "
            "(default: walk up from cwd, fallback to <km install dir>/config.yaml)"
        ),
    )
    parser.add_argument(
        "-C",
        "--dir",
        dest="project_dir",
        default=None,
        metavar="DIR",
        help=(
            "credentials project directory to start from (walks up to config.yaml); "
            "overrides $KM_PROJECT_DIR, which overrides the cwd"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    from km.commands import (
        login,
        mfa,
        publish,
        rotate,
        validate,
    )

    validate.add_parser(subparsers)
    publish.add_parser(subparsers)
    rotate.add_parser(subparsers)
    mfa.add_parser(subparsers)
    login.add_parser(subparsers)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    from km.location import set_cli_dir

    set_cli_dir(getattr(args, "project_dir", None))
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return 2
    try:
        return int(func(args) or 0)
    except NotImplementedError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("\naborted\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
