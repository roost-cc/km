"""One-key login menu: copy a login's username, password or MFA code on demand.

Shared by ``km login`` and the interactive producer. It takes an effective
login (``km.manifest_io.effective_login``) that is already loaded and knows nothing about files, argv or exit
codes. Status lines name a field and never its value: there is no path that
prints a secret, even when the clipboard is unavailable.
"""

from __future__ import annotations

import enum
import os
import select
import sys
import termios
import threading
import time
import tty
from atexit import register as _at_exit
from collections.abc import Callable

from km import browser, clipboard, otp

# (key, login field, label) in menu order.
_FIELDS = (
    ("u", "id", "username"),
    ("p", "password", "password"),
    ("t", "mfa", "MFA token"),
)
_DONE_KEYS = ("x", "q", "\x1b")
_ABORT_KEYS = ("\x03", "\x04")  # Ctrl-C and Ctrl-D arrive as bytes with ISIG and ICANON off.
# How long to wait for the rest of an escape sequence (an arrow key, say)
# before treating a lone ESC as the Esc key.
_ESC_SEQ_TIMEOUT = 0.05


class MenuExit(enum.Enum):
    DONE = "done"  # x, q or Esc
    ABORT = "abort"  # Ctrl-C or Ctrl-D


class NotATTY(Exception):
    """stdin is not a terminal, so the menu cannot read single keypresses."""


def usable_fields(login: dict) -> list[tuple[str, str, str]]:
    """Return the ``(key, field, label)`` entries whose field is a non-empty string."""
    return [
        (key, field, label)
        for key, field, label in _FIELDS
        if isinstance(login.get(field), str) and login[field].strip()
    ]


def read_key(fd: int | None = None) -> str:
    """Read one keypress from ``fd`` (default stdin) without waiting for Enter.

    The terminal is in cbreak mode with ISIG off only while this call reads,
    and its previous mode is restored on every exit path. EOF reads as Ctrl-D.
    An escape sequence comes back whole, so an arrow key is not taken for Esc.
    """
    if fd is None:
        fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        # cbreak by hand: tty.setcbreak applies it with TCSAFLUSH, which
        # would discard a key typed ahead of this read.
        attrs = termios.tcgetattr(fd)
        attrs[tty.LFLAG] &= ~(termios.ICANON | termios.ECHO | termios.ISIG)
        attrs[tty.CC][termios.VMIN] = 1
        attrs[tty.CC][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        data = os.read(fd, 1)
        if data == b"\x1b" and select.select([fd], [], [], _ESC_SEQ_TIMEOUT)[0]:
            data += os.read(fd, 16)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    if not data:
        return "\x04"
    return data.decode("utf-8", errors="replace")


def _say(line: str) -> None:
    sys.stderr.write(f"{line}\n")
    sys.stderr.flush()


def run_login_menu(
    login: dict,
    label: str,
    *,
    open_browser: bool,
    exit_label: str = "exit",
    read_key: Callable[[], str] = read_key,
    copy: Callable[[str], bool] = clipboard.copy,
    open_url: Callable[[str], bool] = browser.open_url,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    isatty: Callable[[], bool] = lambda: sys.stdin.isatty(),
) -> MenuExit:
    """Open the login page, then copy fields on keypress until an exit key.

    Raises ``NotATTY`` before doing anything when stdin is not a terminal.
    Each key overwrites the clipboard, and the clipboard is cleared when the
    km process exits (see ``clear_clipboard_at_exit``), so a rotation's
    paste-back still has what the operator copied from the site.
    """
    if not isatty():
        raise NotATTY("stdin is not a terminal")
    clear_clipboard_at_exit()

    url = login.get("url")
    if isinstance(url, str) and url.strip():
        if open_browser:
            open_url(url)
    else:
        _say("no login url; open the site yourself")

    fields = {key: (field, name) for key, field, name in usable_fields(login)}
    _say(f"{label} — press a key:")
    for key, (_, name) in fields.items():
        _say(f"  {key}  copy {name}")
    _say(f"  x  {exit_label}")

    result = MenuExit.ABORT
    try:
        while True:
            key = read_key()
            if key in _DONE_KEYS:
                result = MenuExit.DONE
                break
            if key in _ABORT_KEYS:
                break
            if key not in fields:
                continue
            field, name = fields[key]
            if field == "mfa":
                try:
                    value, seconds_left = otp.fresh_totp(
                        login["mfa"],
                        now=now,
                        sleep=sleep,
                        on_wait=lambda remaining: _say(f"waiting {remaining}s for the next code"),
                    )
                except otp.OTPSecretError as e:
                    _say(f"error: {label}: {e}")
                    continue
                copied_line = f"{name} copied ({seconds_left}s left)"
            else:
                value = login[field]
                copied_line = f"{name} copied"
            if copy(value):
                _say(copied_line)
            else:
                _say(f"{name} not copied (no clipboard available)")
    except KeyboardInterrupt:
        result = MenuExit.ABORT
    return result


_exit_clear_registered = False
_exit_clear_lock = threading.Lock()


def clear_clipboard_at_exit() -> None:
    """Register ``clear_clipboard`` to run once when the km process exits.

    km never reads the clipboard; running a command that shows the menu
    means handing it over until km exits. The check and the registration
    happen under one lock, so concurrent callers register once; a failed
    registration leaves the flag unset and the next call retries.
    """
    global _exit_clear_registered
    with _exit_clear_lock:
        if not _exit_clear_registered:
            _at_exit(clear_clipboard)
            _exit_clear_registered = True


def clear_clipboard(clear: Callable[[], bool] = clipboard.clear) -> None:
    """Clear the clipboard unconditionally. A failure here never raises."""
    try:
        cleared = clear()
    except Exception:  # noqa: BLE001
        cleared = False
    _say("clipboard cleared" if cleared else "clipboard not cleared (no clipboard available)")
