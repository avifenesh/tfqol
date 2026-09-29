"""Update only a verified editable field through accessibility operations."""
from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable

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
    ) -> None:
        self._interrupted = interrupted
        self._get_focus = get_focus
        self._desktop = desktop if desktop is not None else DesktopText()
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
            if self._field is None or not (self._field.can_delete or self._field.paste_editable):
                self._halt("This app does not expose an editable text field. Enable its accessibility support, restart it, and try again.")
            if self._field.selection:
                self._halt("Clear the text selection before dictating")
        except FocusError as exc:
            self._halt(str(exc))
        self._start = self._field.caret

    @property
    def recovery_text(self) -> str:
        return self.latest_text

    @property
    def corrections_available(self) -> bool:
        return self._field is not None and (self._field.can_delete or self._field.paste_editable)

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
        # Keep one draft in one field. The clipboard path sends only Paste;
        # dictated characters are never interpreted as keyboard events.
        self.latest_text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.latest_text):
            self._halt("Control characters cannot be dictated safely")
        current = self._guard()
        prefix = 0
        for old, new in zip(self.typed_text, self.latest_text):
            if old != new:
                break
            prefix += 1
        if current.paste_editable:
            if self.typed_text == self.latest_text:
                return
            if not self.latest_text:
                return  # a missing transcript must never erase the draft
            if prefix == len(self.latest_text):
                # An empty clipboard paste may be ignored by browsers. Include
                # one owned character so a shortened hypothesis is replaced by
                # a normal, nonempty paste without any Delete/Backspace keys.
                prefix = max(0, prefix - 1)
            start = self._start + prefix
            if (current.caret != self._start + len(self.typed_text)
                    or current.text[self._start:current.caret] != self.typed_text):
                self._halt("The draft changed; correction stopped")
            try:
                changed = self._desktop.replace_suffix(self._window, current, start,
                                                       self.latest_text[prefix:])
            except FocusError as exc:
                self._halt(str(exc))
            wanted = current.text[:self._start] + self.latest_text + current.text[current.caret:]
            same = changed.identity == current.identity or (
                current.empty_editor and current.caret == 0
                and changed.editor_identity == (current.editor_identity or current.identity)
            )
            matched = changed.text == wanted or (
                current.empty_editor and current.text == "\n"
                and current.caret == self._start == 0 and not self.typed_text
                and changed.text == self.latest_text
            )
            if (not same or not matched
                    or changed.caret != self._start + len(self.latest_text) or changed.selection):
                self._halt("The pasted text did not match the expected draft")
            self._field = changed
            self.typed_text = self.latest_text
            return
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
        # Each insertion is addressed to the exact editable object, not to the
        # application's keyboard shortcut dispatcher.
        for offset in range(0, len(addition), 256):
            piece = addition[offset:offset + 256]
            field = self._guard()
            try:
                changed = self._desktop.insert_text(self._window, field, piece)
                wanted = field.text[:field.caret] + piece + field.text[field.caret:]
                if (changed.identity != field.identity or changed.text != wanted
                        or changed.caret != field.caret + len(piece) or changed.selection):
                    self._halt("Insertion stopped; the field changed")
                self._field = changed
            except (OSError, subprocess.SubprocessError, FocusError) as exc:
                self._halt(f"Text editing stopped: {exc}")
            self.typed_text += piece

    def finish(self) -> str:
        """Validate the last insertion, leave the caret alone, never press Enter."""
        if not self._finished:
            self._guard()
            self._finished = True
        return self.latest_text



def check_prereqs() -> list[str]:
    problems = []
    for name, purpose in (("gdbus", "verify desktop focus"),
                          ("xdotool", "paste into X11 and XWayland editors"),
                          ("ydotool", "paste into native Wayland editors")):
        if shutil.which(name) is None:
            problems.append(f"{name} is not installed; it is needed to {purpose}")
    return problems
