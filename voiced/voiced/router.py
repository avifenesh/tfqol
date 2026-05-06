"""Inject text via ydotool into whichever window is focused right now."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess

log = logging.getLogger(__name__)


class RouterError(RuntimeError):
    pass


def _ydotool_available() -> bool:
    return shutil.which("ydotool") is not None


def _ydotool_type(text: str) -> None:
    if not text:
        return
    if not _ydotool_available():
        raise RouterError("ydotool not installed")
    log.info("ydotool type <-- %r (%d chars)", text, len(text))
    res = subprocess.run(
        ["ydotool", "type", "--key-delay", "2", "--", text],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RouterError(
            f"ydotool type failed (rc={res.returncode}): "
            f"stderr={res.stderr.strip()!r} stdout={res.stdout.strip()!r}"
        )


def _ydotool_enter() -> None:
    if not _ydotool_available():
        raise RouterError("ydotool not installed")
    res = subprocess.run(
        ["ydotool", "key", "28:1", "28:0"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RouterError(f"ydotool enter failed: {res.stderr.strip()}")


def type_text_focused(text: str, *, send: bool = False) -> None:
    """Type text into whatever window currently has keyboard focus.

    Relies on the compositor's current focus — no window management done here.
    If send=True, presses Enter after typing.
    """
    if text:
        _ydotool_type(text)
    if send:
        _ydotool_enter()


def check_prereqs() -> list[str]:
    problems: list[str] = []
    if not _ydotool_available():
        problems.append("ydotool not in PATH — install and enable user service")
    if not os.access("/dev/uinput", os.W_OK):
        log.debug("/dev/uinput not user-writable; relying on ydotoold")
    return problems
