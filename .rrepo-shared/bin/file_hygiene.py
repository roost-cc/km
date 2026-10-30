#!/usr/bin/env python3
"""Generic file hygiene checks for the shared-config `file-hygiene` hook.

Applied to every file pre-commit passes in:
  - large-file check (> --max-kb, default 500) — all files
  - trailing whitespace — text files (auto-fixed; Markdown keeps two-space
    hard line breaks)
  - exactly one newline at end of file — text files (auto-fixed)
  - leftover merge-conflict markers — text files
  - YAML syntax — *.yaml / *.yml (requires PyYAML)
  - JSON syntax — *.json

Exit code is non-zero when any problem is found, including problems that were
auto-fixed (pre-commit convention: a fixing hook fails so the user re-stages).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

MARKDOWN_EXTS = {".md", ".markdown"}
YAML_EXTS = {".yaml", ".yml"}
# Same patterns as pre-commit-hooks' check-merge-conflict: the ours/theirs
# markers are always followed by a ref name, the separator is a bare line.
CONFLICT_MARKER_PREFIXES = (b"<<<<<<< ", b"======= ", b">>>>>>> ")
CONFLICT_SEPARATOR = b"======="


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def check_large(path: str, max_kb: int, problems: list[str]) -> None:
    size_kb = os.path.getsize(path) / 1024
    if size_kb > max_kb:
        problems.append(f"{path}: file is {size_kb:.0f} kB, larger than the {max_kb} kB limit")


def fix_trailing_whitespace(path: str, data: bytes, problems: list[str]) -> bytes:
    is_markdown = os.path.splitext(path)[1].lower() in MARKDOWN_EXTS
    fixed_lines = []
    changed = False
    for line in data.splitlines(keepends=True):
        content = line.rstrip(b"\r\n")
        ending = line[len(content) :]
        stripped = content.rstrip(b" \t")
        if is_markdown and content != stripped and content.endswith(b"  ") and b"\t" not in content[len(stripped) :]:
            # Preserve Markdown hard line breaks, normalized to exactly two spaces.
            stripped += b"  "
        if content != stripped:
            changed = True
        fixed_lines.append(stripped + ending)
    if changed:
        problems.append(f"{path}: trailing whitespace (fixed)")
        return b"".join(fixed_lines)
    return data


def fix_end_of_file(path: str, data: bytes, problems: list[str]) -> bytes:
    if not data:
        return data
    fixed = data.rstrip(b"\r\n \t")
    if not fixed:
        problems.append(f"{path}: file contains only whitespace (fixed to empty file)")
        return b""
    fixed += b"\r\n" if data.splitlines(keepends=True)[0].endswith(b"\r\n") else b"\n"
    if fixed != data:
        problems.append(f"{path}: missing or extra newline at end of file (fixed)")
    return fixed


def check_merge_conflict(path: str, data: bytes, problems: list[str]) -> None:
    for lineno, line in enumerate(data.splitlines(), start=1):
        if line == CONFLICT_SEPARATOR or any(line.startswith(marker) for marker in CONFLICT_MARKER_PREFIXES):
            problems.append(f"{path}:{lineno}: merge conflict marker '{line[:7].decode()}'")


def check_yaml(path: str, data: bytes, problems: list[str]) -> None:
    try:
        import yaml
    except ImportError:
        problems.append(
            f"{path}: cannot validate YAML — PyYAML is not installed for python3 "
            "(install it, e.g. 'python3 -m pip install pyyaml' or 'apt install python3-yaml')"
        )
        return
    try:
        list(yaml.safe_load_all(data))
    except yaml.YAMLError as exc:
        problems.append(f"{path}: invalid YAML: {exc}")


def check_json(path: str, data: bytes, problems: list[str]) -> None:
    try:
        json.loads(data)
    except ValueError as exc:
        problems.append(f"{path}: invalid JSON: {exc}")


def process(path: str, max_kb: int, problems: list[str]) -> None:
    check_large(path, max_kb, problems)
    with open(path, "rb") as fh:
        data = fh.read()
    if is_binary(data):
        return

    fixed = fix_trailing_whitespace(path, data, problems)
    fixed = fix_end_of_file(path, fixed, problems)
    if fixed != data:
        with open(path, "wb") as fh:
            fh.write(fixed)

    check_merge_conflict(path, fixed, problems)
    ext = os.path.splitext(path)[1].lower()
    if ext in YAML_EXTS:
        check_yaml(path, fixed, problems)
    elif ext == ".json":
        check_json(path, fixed, problems)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-kb", type=int, default=500, help="large-file limit in kB (default: 500)")
    parser.add_argument("filenames", nargs="*", help="files to check (provided by pre-commit)")
    args = parser.parse_args(argv)

    problems: list[str] = []
    for path in args.filenames:
        process(path, args.max_kb, problems)

    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
