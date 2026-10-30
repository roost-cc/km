"""Clipboard copy and clear: tmux OSC 52, then kitty OSC 52, then ``wl-copy``.

The value goes to the terminal or to ``wl-copy`` on stdin, never on a
command line. km never reads the clipboard: every write is unconditional.
"""

from __future__ import annotations

import base64
import os
import subprocess

# wl-copy --clear should return at once; don't let a stuck compositor hang exit.
_WL_TIMEOUT = 1.0


def _osc52_target() -> str | None:
    """Return ``"tmux"``, ``"kitty"`` or None for the OSC 52 path this terminal takes."""
    if os.environ.get("TMUX"):
        return "tmux"
    term = os.environ.get("TERM", "")
    if os.environ.get("KITTY_WINDOW_ID") or term == "xterm-kitty":
        return "kitty"
    return None


def _osc52_seq(payload: str, target: str) -> str:
    """Return the OSC 52 sequence for ``payload``, wrapped for tmux passthrough."""
    if target == "tmux":
        return f"\033Ptmux;\033\033]52;c;{payload}\a\033\\"
    return f"\033]52;c;{payload}\a"


def _osc52_write(payload: str, target: str) -> bool:
    try:
        with open("/dev/tty", "w", encoding="utf-8") as tty_out:
            tty_out.write(_osc52_seq(payload, target))
            tty_out.flush()
        return True
    except OSError:
        return False


def copy(value: str) -> bool:
    """Copy ``value`` to the clipboard. Return True if some path accepted it."""
    if not value:
        return False
    target = _osc52_target()
    if target:
        return _osc52_write(base64.b64encode(value.encode("utf-8")).decode("ascii"), target)
    try:
        proc = subprocess.run(
            ["wl-copy"],
            input=value.encode("utf-8"),
            check=True,
            capture_output=True,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def clear() -> bool:
    """Clear the clipboard. Return True if some path accepted the clear."""
    target = _osc52_target()
    if target:
        # An empty OSC 52 payload sets the clipboard to empty text.
        return _osc52_write("", target)
    try:
        subprocess.run(
            ["wl-copy", "--clear"],
            check=True,
            capture_output=True,
            timeout=_WL_TIMEOUT,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False
