"""Open a URL in the operator's browser with ``xdg-open``."""

from __future__ import annotations

import subprocess
import sys


def open_url(url: str) -> bool:
    """Open ``url``. Return False, after a warning, when ``xdg-open`` is missing."""
    try:
        subprocess.Popen(  # noqa: S603
            ["xdg-open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        sys.stderr.write(f"warn: xdg-open not found; open this URL manually: {url}\n")
        return False
    return True
