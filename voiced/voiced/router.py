"""Inject text via ydotool into whichever window is focused right now."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import replace

from .focus import DesktopText, FocusError, TextField, Window, focused_window

log = logging.getLogger(__name__)


class RouterError(RuntimeError):
    pass


class InterruptedDraft(RouterError):
    """No more automatic edits are safe; the latest full transcript is retained."""

    def __init__(self, reason: str, recovery_text: str = "") -> None:
        super().__init__(reason)
        self.recovery_text = recovery_text


class DraftWriter:
    """Own only the text this utterance inserted at the original caret.

    ``interrupted`` must report physical keyboard/mouse activity since capture
    started (excluding the trigger key and this application's virtual keyboard).
    A halted writer still accepts hypotheses into ``latest_text`` for recovery,
    but every update raises InterruptedDraft and performs no further input.
    """

    def __init__(
        self, interrupted: Callable[[], bool], *,
        get_focus: Callable[[], Window] = focused_window,
        desktop: DesktopText | None = None,
        type_text: Callable[[str], None] | None = None,
    ) -> None:
        self._interrupted = interrupted
        self._get_focus = get_focus
        self._desktop = desktop if desktop is not None else DesktopText()
        self._type = type_text
        self.latest_text = ""
        self.typed_text = ""
        self.halted = False
        self.reason = ""
        self._finished = False
        try:
            self._window = self._get_focus()
            self._field = self._desktop.snapshot(self._window)
            if self._get_focus() != self._window or self._interrupted():
                self._halt("Focus or input changed; dictation stopped")
            if self._field is not None and self._field.selection:
                self._halt("Clear the text selection before dictating")
        except FocusError as exc:
            self._halt(str(exc))
        self._start = self._field.caret if self._field is not None else None
        if self._type is None:
            self._type = (
                lambda text: _xdotool_type(self._window, text)
            ) if self._window.backend == "x11" else _ydotool_type

    @property
    def recovery_text(self) -> str:
        return self.latest_text

    @property
    def corrections_available(self) -> bool:
        return self._field is not None and self._field.can_delete

    def _halt(self, reason: str) -> None:
        self.halted = True
        self.reason = reason
        raise InterruptedDraft(reason, self.latest_text)

    def _guard(self) -> TextField | None:
        if self.halted:
            raise InterruptedDraft(self.reason, self.latest_text)
        if self._finished:
            self._halt("This dictation is already finished")
        if self._interrupted():
            self._halt("Keyboard or mouse activity; dictation stopped")
        try:
            if self._get_focus() != self._window:
                self._halt("Focus changed; dictation stopped")
            current = self._desktop.snapshot(self._window)
            # Once a readable field is established, its disappearance is also a
            # disturbance. Never downgrade a verified draft to unguarded input.
            if self._field is not None and current != self._field:
                self._halt("The field or caret changed; dictation stopped")
            if self._field is None and current is not None:
                self._halt("The focused field changed; dictation stopped")
            if self._interrupted() or self._get_focus() != self._window:
                self._halt("Focus or input changed; dictation stopped")
            return current
        except FocusError as exc:
            self._halt(str(exc))

    def update(self, text: str) -> None:
        # Speech is a single draft, never a command submission. Literal newlines
        # would become Enter keypresses in ydotool and can execute terminal input.
        self.latest_text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.latest_text):
            self._halt("Control characters cannot be dictated safely")
        current = self._guard()
        prefix = 0
        for old, new in zip(self.typed_text, self.latest_text):
            if old != new:
                break
            prefix += 1
        if prefix < len(self.typed_text):
            if current is None or not current.can_delete or self._start is None:
                self._halt("This field cannot verify corrections; final text is available to copy")
            start = self._start + prefix
            if (current.caret != self._start + len(self.typed_text)
                    or current.text[self._start:current.caret] != self.typed_text):
                self._halt("The draft changed; correction stopped")
            # The helper repeats the identity/text/caret check immediately before
            # deleting only this draft's suffix. There is no Backspace fallback.
            self._guard()
            try:
                changed = self._desktop.delete_suffix(self._window, current, start)
            except FocusError as exc:
                self._halt(str(exc))
            wanted = current.text[:start] + current.text[current.caret:]
            if (changed.identity != current.identity or changed.text != wanted
                    or changed.caret != start or changed.selection):
                self._halt("Correction stopped; the field changed")
            self._field = changed
            self.typed_text = self.typed_text[:prefix]
        addition = self.latest_text[len(self.typed_text):]
        # Bound virtual-key bursts so physical input can stop a long insertion.
        for offset in range(0, len(addition), 32):
            piece = addition[offset:offset + 32]
            field = self._guard()
            try:
                if field is not None and field.can_delete:
                    changed = self._desktop.insert_text(self._window, field, piece)
                    wanted = field.text[:field.caret] + piece + field.text[field.caret:]
                    if (changed.identity != field.identity or changed.text != wanted
                            or changed.caret != field.caret + len(piece) or changed.selection):
                        self._halt("Insertion stopped; the field changed")
                    self._field = changed
                else:
                    self._type(piece)
                    if field is not None:
                        self._field = replace(
                            field, text=field.text[:field.caret] + piece + field.text[field.caret:],
                            caret=field.caret + len(piece),
                        )
            except (OSError, subprocess.SubprocessError, RouterError, FocusError) as exc:
                self._halt(f"Typing stopped: {exc}")
            self.typed_text += piece

    def finish(self) -> str:
        """Validate the last insertion, leave the caret alone, never press Enter."""
        if not self._finished:
            self._guard()
            self._finished = True
        return self.latest_text


def _xdotool_type(window: Window, text: str) -> None:
    """Use only the selected X server; never inject into host uinput in X11 QA."""
    if not os.environ.get("DISPLAY") or focused_window() != window:
        raise RouterError("X11 focus changed; text insertion stopped")
    result = subprocess.run(
        ["xdotool", "type", "--clearmodifiers", "--delay", "2", "--", text],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode:
        raise RouterError("X11 text insertion failed")


def _ydotool_available() -> bool:
    return shutil.which("ydotool") is not None


def _ydotool_type(text: str) -> None:
    if not text:
        return
    if not _ydotool_available():
        raise RouterError("ydotool not installed")
    log.debug("ydotool type (%d chars)", len(text))
    res = subprocess.run(
        ["ydotool", "type", "--key-delay", "2", "--", text],
        capture_output=True, text=True, timeout=10,
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

    Relies on the compositor's current focus: no window management done here.
    If send=True, presses Enter after typing.
    """
    if text:
        _ydotool_type(text)
    if send:
        _ydotool_enter()


def check_prereqs() -> list[str]:
    problems: list[str] = []
    if not _ydotool_available():
        problems.append("ydotool not in PATH: install and enable user service")
    if not os.access("/dev/uinput", os.W_OK):
        log.debug("/dev/uinput not user-writable; relying on ydotoold")
    return problems
