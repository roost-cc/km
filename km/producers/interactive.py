"""Interactive producer — login menu + paste-back.

Operator-driven rotation. For each rotation-group invocation:

1. **Login menu** (only when the manifest's effective login has a
   ``url``; ``ctx.login``, F-56 D2): the ``km login`` menu
   (``km.login_menu``) opens the URL and copies its ``id``, ``password``
   or a fresh TOTP from its ``mfa`` on keypress until the operator
   continues. Ctrl-C or
   Ctrl-D in the menu aborts the group (``ProducerContractError``).
2. **Paste-back**: prompt ``Enter new <name>:`` once per secret in the
   group; collect new values. Empty input on any secret aborts the
   group (``ProducerContractError``).
3. **Return** a list of Secrets for the rotate command to write into
   the manifest's ``secrets:`` block.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

from km.login_menu import MenuExit, NotATTY, run_login_menu
from km.producers.contract import Producer, ProducerContractError, Secret


def _extract_anchor_section(text: str, anchor: str) -> str | None:
    target = anchor.strip().lower()
    lines = text.splitlines()
    out: list[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("### ") and stripped[4:].strip().lower() == target:
            in_section = True
            out.append(line)
            continue
        if in_section:
            if line.startswith(("### ", "## ", "# ")):
                break
            out.append(line)
    return "\n".join(out) if out else None


def _print_runbook(manifest_path: Path, runbook_ref: str) -> None:
    rb_path, _, anchor = runbook_ref.partition("#")
    project_dir: Path | None = None
    for d in (manifest_path.parent, *manifest_path.parents):
        if (d / "config.yaml").is_file():
            project_dir = d
            break
    candidates: list[Path] = []
    if project_dir is not None:
        candidates.append(project_dir / rb_path)
        candidates.append(project_dir.parent / rb_path)
    candidates.append(manifest_path.parent / rb_path)
    rb_file = next((p for p in candidates if p.is_file()), None)
    if rb_file is None:
        sys.stderr.write(f"warn: runbook not found: {runbook_ref}\n")
        return
    text = rb_file.read_text(encoding="utf-8")
    if anchor:
        section = _extract_anchor_section(text, anchor)
        if section:
            sys.stdout.write(f"\n--- runbook section #{anchor} ({rb_path}) ---\n{section}\n\n")
            return
        sys.stderr.write(f"warn: anchor '#{anchor}' not found in {rb_path}; printing whole file\n")
    sys.stdout.write(f"\n--- runbook {rb_path} ---\n{text}\n")


class Interactive(Producer):
    def produce(self, entries, ctx):
        if not entries:
            return []
        manifest_path = entries[0].manifest_path

        runbook = (ctx.manifest.get("rotation") or {}).get("runbook")
        if isinstance(runbook, str) and runbook:
            _print_runbook(manifest_path, runbook)

        login = ctx.login
        url = login.get("url") if isinstance(login, Mapping) else None
        if isinstance(url, str) and url.strip():
            # The menu clears the clipboard when km exits, after paste-back.
            try:
                result = run_login_menu(
                    dict(login),
                    manifest_path.stem,
                    open_browser=True,
                    exit_label="continue to new values",
                )
            except NotATTY:
                raise ProducerContractError(
                    "interactive producer: the login step needs an interactive terminal"
                ) from None
            if result is MenuExit.ABORT:
                raise ProducerContractError("interactive producer: login aborted")

        results: list[Secret] = []
        sys.stdout.write(
            f"\nPaste new values for group '{entries[0].group_name}' ({manifest_path.name}):\n"
        )
        for e in entries:
            try:
                value = input(f"  Enter new {e.name}: ").strip()
            except EOFError as exc:
                raise ProducerContractError(
                    f"interactive producer: stdin closed before {e.name!r} entered"
                ) from exc
            if not value:
                raise ProducerContractError(
                    f"interactive producer: empty value for {e.name!r} — "
                    "aborting group (no partial-rotate writes)"
                )
            results.append(Secret(name=e.name, value=value))
        return results
