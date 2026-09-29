"""Window identity and bounded, read-before-delete accessibility operations.

The accessibility subprocess uses the system Python, where PyGObject is installed,
so the Whisper virtual environment does not need a second GTK installation.
"""
from __future__ import annotations

import ast
from collections import deque
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import time


class FocusError(RuntimeError):
    pass


@dataclass(frozen=True)
class Window:
    backend: str
    identity: str
    pid: int = 0


@dataclass(frozen=True)
class TextField:
    identity: str
    text: str
    caret: int
    selection: bool = False
    can_delete: bool = False


def _command(argv: list[str], timeout: float = 1.0) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FocusError("Cannot check the focused window") from exc
    if result.returncode:
        raise FocusError("Cannot check the focused window")
    return result.stdout.strip()


def focused_window() -> Window:
    """Read focus without activating any window. Unknown focus fails closed."""
    backend = os.environ.get("VOICED_DESKTOP_BACKEND", "").lower()
    if backend == "x11" or (backend != "gnome" and (
        os.environ.get("XDG_SESSION_TYPE") == "x11" or (
        os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
        )
    )):
        identity = _command(["xdotool", "getactivewindow"])
        if not identity.isdecimal() or int(identity) == 0:
            raise FocusError("No focused window")
        try:
            pid = int(_command(["xdotool", "getwindowpid", identity]))
        except (FocusError, ValueError):
            pid = 0
        return Window("x11", identity, pid)

    for service, path in (
        ("dev.avifenesh.ComputerUseLinux.WindowControl",
         "/dev/avifenesh/ComputerUseLinux/WindowControl"),
        ("com.openai.Codex.WindowControl", "/com/openai/Codex/WindowControl"),
    ):
        try:
            raw = _command([
                "gdbus", "call", "--session", "--dest", service,
                "--object-path", path, "--method", f"{service}.ListWindows",
            ])
            windows = json.loads(ast.literal_eval(raw)[0])
            matches = [window for window in windows if window.get("focused")]
            if len(matches) != 1:
                raise FocusError("No unique focused window")
            window = matches[0]
            identity = str(window["window_id"])
            if not identity.isdecimal():
                raise FocusError("Invalid focused window")
            return Window("gnome", identity, int(window.get("pid") or 0))
        except (FocusError, SyntaxError, ValueError, TypeError, KeyError):
            continue
    raise FocusError("Cannot identify focus; enable the GNOME window-control extension")


class DesktopText:
    """A missing accessibility interface allows append-only drafts, never deletion."""

    def _request(self, window: Window, **request: object) -> dict:
        payload = {"window": asdict(window), **request}
        try:
            result = subprocess.run(
                ["/usr/bin/python3", str(Path(__file__).resolve()), "--accessibility"],
                input=json.dumps(payload), capture_output=True, text=True, timeout=2.5,
            )
            if result.returncode:
                raise FocusError("The focused field cannot be read safely")
            return json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise FocusError("The focused field cannot be read safely") from exc

    def snapshot(self, window: Window) -> TextField | None:
        try:
            result = self._request(window, action="snapshot")
        except FocusError:
            return None
        if result.get("status") == "blocked":
            raise FocusError(result.get("reason", "The focused field is protected"))
        if result.get("status") != "ok":
            return None
        return TextField(**result["field"])

    def delete_suffix(self, window: Window, expected: TextField, start: int) -> TextField:
        """Delete an exact verified character range, never issue Backspace keys."""
        result = self._request(
            window, action="delete", expected=asdict(expected), start=start,
        )
        if result.get("status") != "ok":
            raise FocusError(result.get("reason", "Correction stopped; the field changed"))
        return TextField(**result["field"])

    def insert_text(self, window: Window, expected: TextField, text: str) -> TextField:
        """Insert into an exact verified editable field and confirm its readback."""
        result = self._request(
            window, action="insert", expected=asdict(expected), text=text,
        )
        if result.get("status") != "ok":
            raise FocusError(result.get("reason", "Insertion stopped; the field changed"))
        return TextField(**result["field"])


def _read_field(node, Atspi) -> TextField:
    state = node.get_state_set()
    if node.get_role() == Atspi.Role.PASSWORD_TEXT:
        raise FocusError("Dictation is unavailable in password fields")
    if node.get_role() == Atspi.Role.TERMINAL:
        raise ValueError("Terminal scrollback is not an editable draft field")
    text = node.get_text_iface()
    if text is None:
        raise ValueError("No readable text interface")
    # GI returns an Accessible implementing Text; Accessible.get_text() is an
    # interface getter, so instance dispatch would call the wrong overload.
    count = Atspi.Text.get_character_count(text)
    if count < 0 or count > 65536:
        raise ValueError("Field exceeds the readback limit")
    value = Atspi.Text.get_text(text, 0, count)
    caret = Atspi.Text.get_caret_offset(text)
    if len(value) != count or not 0 <= caret <= count:
        raise ValueError("Cannot establish exact text offsets")
    selected = False
    for index in range(Atspi.Text.get_n_selections(text)):
        selection = Atspi.Text.get_selection(text, index)
        if selection.start_offset != selection.end_offset:
            selected = True
    return TextField(
        identity=f"{node.get_process_id()}:{node.path}",
        text=value, caret=caret, selection=selected,
        can_delete=(state.contains(Atspi.StateType.EDITABLE)
                    and node.get_editable_text_iface() is not None),
    )


def _find_field(Atspi, pid: int):
    """Find a focused field only inside the compositor's focused application."""
    if pid <= 0:
        return None
    deadline = time.monotonic() + 1.25
    desktop = Atspi.get_desktop(0)
    queue = deque()
    for index in range(min(desktop.get_child_count(), 128)):
        app = desktop.get_child_at_index(index)
        try:
            if app is not None and app.get_process_id() == pid:
                queue.append(app)
        except Exception:
            continue
    visited = 0
    while queue and visited < 1200 and time.monotonic() < deadline:
        node = queue.popleft()
        visited += 1
        try:
            states = node.get_state_set()
            if states.contains(Atspi.StateType.DEFUNCT):
                continue
            if states.contains(Atspi.StateType.FOCUSED):
                if (node.get_role() == Atspi.Role.PASSWORD_TEXT
                        or node.get_text_iface() is not None):
                    return node
            # Inactive top-level windows sometimes retain a descendant's FOCUSED
            # state. Do not accept that stale field from another window.
            if node.get_role() in (Atspi.Role.FRAME, Atspi.Role.DIALOG):
                if not states.contains(Atspi.StateType.ACTIVE):
                    continue
            for index in range(min(node.get_child_count(), 1200 - visited)):
                child = node.get_child_at_index(index)
                if child is not None:
                    queue.append(child)
        except Exception:
            continue
    return None


def _accessibility_request(request: dict) -> dict:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    window = Window(**request["window"])
    if focused_window() != window:
        raise FocusError("Focus changed; dictation stopped")
    Atspi.init()
    Atspi.set_timeout(150, 300)
    node = _find_field(Atspi, window.pid)
    if node is None:
        return {"status": "unavailable"}
    field = _read_field(node, Atspi)
    if request["action"] == "snapshot":
        return {"status": "ok", "field": asdict(field)}
    expected = TextField(**request["expected"])
    if field != expected or field.selection or not field.can_delete:
        raise FocusError("Correction stopped; the field or caret changed")
    action = request["action"]
    if action == "delete":
        start = request["start"]
        if type(start) is not int or not 0 <= start < field.caret:
            raise FocusError("Invalid correction range")
        wanted = field.text[:start] + field.text[field.caret:]
        target_caret = start
    elif action == "insert":
        addition = request["text"]
        if not isinstance(addition, str) or not addition:
            raise FocusError("Invalid insertion")
        wanted = field.text[:field.caret] + addition + field.text[field.caret:]
        target_caret = field.caret + len(addition)
    else:
        raise FocusError("Unknown field operation")
    if focused_window() != window:
        raise FocusError("Focus changed; dictation stopped")
    # Re-read immediately before editing. AT-SPI ranges avoid application
    # keybindings, ignored synthetic keystrokes, and backspace/grapheme behavior.
    if _read_field(node, Atspi) != expected:
        raise FocusError("Correction stopped; the field or caret changed")
    if action == "delete":
        if not Atspi.EditableText.delete_text(node, start, field.caret):
            raise FocusError("This field cannot apply a safe correction")
    else:
        if not Atspi.EditableText.insert_text(
            node, field.caret, addition, len(addition.encode("utf-8")),
        ):
            raise FocusError("This field cannot accept verified text")
    changed = _read_field(node, Atspi)
    if (changed.identity != field.identity or changed.text != wanted
            or changed.selection):
        raise FocusError("Correction paused; check the draft before pasting the final text")
    # Some EditableText implementations insert characters without advancing the
    # caret. Move only from the unchanged insertion point after exact readback;
    # an independently moved caret is a disturbance, never something to undo.
    if action == "insert" and changed.caret == field.caret:
        if focused_window() != window or _read_field(node, Atspi) != changed:
            raise FocusError("Focus or caret changed; insertion stopped")
        if not Atspi.Text.set_caret_offset(node, target_caret):
            raise FocusError("Text inserted; the caret could not advance safely")
        changed = _read_field(node, Atspi)
    if (changed.identity != field.identity or changed.text != wanted
            or changed.caret != target_caret or changed.selection):
        raise FocusError("Correction paused; check the draft before pasting the final text")
    return {"status": "ok", "field": asdict(changed)}


if __name__ == "__main__":
    try:
        response = _accessibility_request(json.load(sys.stdin))
    except FocusError as exc:
        response = {"status": "blocked", "reason": str(exc)}
    except Exception:
        response = {"status": "unavailable"}
    print(json.dumps(response))
