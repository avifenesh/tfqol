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
import selectors


class FocusError(RuntimeError):
    pass


@dataclass(frozen=True)
class Window:
    backend: str
    identity: str
    pid: int = 0
    client_type: str = ""


@dataclass(frozen=True)
class TextField:
    identity: str
    text: str
    caret: int
    selection: bool = False
    can_delete: bool = False
    paste_editable: bool = False
    editor_identity: str = ""
    empty_editor: bool = False


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
            return Window("gnome", identity, int(window.get("pid") or 0),
                          str(window.get("client_type") or ""))
        except (FocusError, SyntaxError, ValueError, TypeError, KeyError):
            continue
    raise FocusError("Cannot identify focus; enable the GNOME window-control extension")


class DesktopText:
    """Read the focused field and edit only through verified accessibility APIs."""

    def _request(self, window: Window, **request: object) -> dict:
        payload = {"window": asdict(window), **request}
        if request.get("action") == "replace":
            return self._paste_request(payload)
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

    def _paste_request(self, payload: dict) -> dict:
        # The helper can remain the clipboard owner after returning the result,
        # preserving every original format until the next copy or logout.
        process = subprocess.Popen(
            ["/usr/bin/python3", str(Path(__file__).resolve()), "--accessibility"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, start_new_session=True,
        )
        try:
            process.stdin.write(json.dumps(payload))
            process.stdin.close()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                if not selector.select(timeout=15):
                    raise FocusError("Text editing timed out; check the draft before retrying")
                line = process.stdout.readline()
            return json.loads(line)
        except (OSError, ValueError) as exc:
            raise FocusError("The paste helper could not edit the field") from exc
        finally:
            process.stdout.close()

    def replace_suffix(self, window: Window, expected: TextField, start: int, text: str) -> TextField:
        result = self._request(window, action="replace", expected=asdict(expected), start=start, text=text)
        if result.get("status") != "ok":
            raise FocusError(result.get("reason", "Correction stopped; the field changed"))
        return TextField(**result["field"])

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
    selections = Atspi.Text.get_n_selections(text)
    if not 0 <= selections <= 16:
        raise ValueError("Cannot establish exact text selections")
    for index in range(selections):
        selection = Atspi.Text.get_selection(text, index)
        if not (0 <= selection.start_offset <= count
                and 0 <= selection.end_offset <= count):
            raise ValueError("Cannot establish exact text selections")
        if selection.start_offset != selection.end_offset:
            selected = True
    return TextField(
        identity=_node_identity(node),
        text=value, caret=caret, selection=selected,
        can_delete=(state.contains(Atspi.StateType.EDITABLE)
                    and node.get_editable_text_iface() is not None),
        paste_editable=(state.contains(Atspi.StateType.EDITABLE)
                        and node.get_editable_text_iface() is None),
        editor_identity=_editor_identity(node, Atspi),
        empty_editor=_is_empty_editor(node, Atspi, value),
    )


def _is_empty_editor(node, Atspi, value):
    if value not in ("", "\n"):
        return False
    roles = {getattr(Atspi.Role, name, None) for name in ("ENTRY", "TEXT")}
    try:
        for _ in range(32):
            if node is None:
                return False
            if node.get_role() in roles:
                return Atspi.Text.get_text(node, 0, -1) in ("", "\n", "\ufffc")
            node = node.get_parent()
    except Exception:
        pass
    return False


def _editor_identity(node, Atspi):
    """Keep the stable textbox anchor when Chromium exposes a paragraph leaf."""
    identity = _node_identity(node)
    current = node
    roles = {getattr(Atspi.Role, name, None) for name in ("ENTRY", "TEXT")}
    try:
        for _ in range(32):
            if current is None:
                break
            if current.get_role() in roles:
                anchor = _node_identity(current)
                return anchor if anchor != identity else ""
            current = current.get_parent()
    except Exception:
        pass
    return ""


def _node_identity(node) -> str:
    # An application may register several AT-SPI connections with identical
    # object paths. Include the unique bus owner when libatspi exposes it.
    owner = getattr(getattr(node, "app", None), "bus_name", None)
    prefix = f"{node.get_process_id()}:{owner}" if owner else str(node.get_process_id())
    return f"{prefix}:{node.path}"


def _same_application(app_pid: int, window_pid: int) -> bool:
    """Accept only one process or a verified parent/child of the same binary.

    Chromium can expose a renderer through AT-SPI while the compositor names
    the browser process. Shared names or executable paths alone do not establish
    ownership: two independently launched instances must stay separate.
    """
    if app_pid <= 0 or window_pid <= 0:
        return False
    if app_pid == window_pid:
        return True
    try:
        if os.readlink(f"/proc/{app_pid}/exe") != os.readlink(f"/proc/{window_pid}/exe"):
            return False
        for child, ancestor in ((app_pid, window_pid), (window_pid, app_pid)):
            seen = set()
            for _ in range(8):
                if child <= 1 or child in seen:
                    break
                seen.add(child)
                status = Path(f"/proc/{child}/status").read_text()
                parent = next(line for line in status.splitlines() if line.startswith("PPid:"))
                child = int(parent.split()[1])
                if child == ancestor:
                    return True
    except (OSError, StopIteration, ValueError, IndexError):
        pass
    return False


def _collection_fields(Atspi, app, deadline: float):
    """Ask the provider for focused nodes without reading an entire long chat."""
    try:
        collection = app.get_collection_iface()
        if collection is None:
            return None
        match = Atspi.CollectionMatchType
        rule = Atspi.MatchRule.new(
            Atspi.StateSet.new([Atspi.StateType.FOCUSED]), match.ALL,
            {}, match.ALL, [], match.ALL, [], match.ALL, False,
        )
        # Query FOCUSED alone: a readonly focused text child may belong to an
        # editable contenteditable parent. Requiring EDITABLE here would miss it.
        focused = Atspi.Collection.get_matches(
            collection, rule, Atspi.CollectionSortOrder.CANONICAL, 32, True,
        )
        if len(focused) >= 32:
            return None
        root_identity = _node_identity(app)
        candidates = {}
        for node in focused:
            if not node.get_state_set().contains(Atspi.StateType.FOCUSED):
                continue
            candidate = None
            seen = set()
            # Every candidate must still reach this exact root through active
            # windows. A stale FOCUSED flag in another window is insufficient.
            for _ in range(64):
                if node is None or time.monotonic() >= deadline:
                    return None
                identity = _node_identity(node)
                if identity in seen:
                    return None
                seen.add(identity)
                if identity == root_identity:
                    if candidate is not None:
                        candidates[_node_identity(candidate)] = candidate
                    break
                states = node.get_state_set()
                role = node.get_role()
                if states.contains(Atspi.StateType.DEFUNCT):
                    break
                if role in (Atspi.Role.FRAME, Atspi.Role.DIALOG):
                    if not states.contains(Atspi.StateType.ACTIVE):
                        break
                if candidate is None and (role == Atspi.Role.PASSWORD_TEXT or (
                        role != Atspi.Role.TERMINAL
                        and states.contains(Atspi.StateType.EDITABLE)
                        and node.get_text_iface() is not None)):
                    # Chromium exposes editable Text without EditableText.
                    # Return the readable target; _read_field reports that
                    # direct mutation is unavailable with can_delete=False.
                    candidate = node
                node = node.get_parent()
            else:
                return None
        return candidates
    except Exception:
        # Collection is optional and some providers advertise but do not
        # implement it. The bounded tree traversal remains a safe fallback.
        return None


def _find_editor(Atspi, pid: int):
    """Find a focused field only inside the compositor's focused application."""
    if pid <= 0:
        return None
    deadline = time.monotonic() + 1.25
    desktop = Atspi.get_desktop(0)
    roots = []
    for index in range(min(desktop.get_child_count(), 128)):
        if time.monotonic() >= deadline:
            return None
        app = desktop.get_child_at_index(index)
        try:
            if app is not None and _same_application(app.get_process_id(), pid):
                roots.append(app)
        except Exception:
            continue
    matched = {}
    for app in roots:
        fields = _collection_fields(Atspi, app, deadline)
        if fields is None:
            break
        matched.update(fields)
    else:
        return next(iter(matched.values())) if len(matched) == 1 else None
    queue = deque((app, None) for app in roots)
    seen = set()
    candidates = {}
    while queue and len(seen) < 1200 and time.monotonic() < deadline:
        node, editable_parent = queue.popleft()
        try:
            identity = _node_identity(node)
            if identity in seen:
                continue
            seen.add(identity)
            states = node.get_state_set()
            if states.contains(Atspi.StateType.DEFUNCT):
                continue
            # Inactive top-level windows sometimes retain a descendant's FOCUSED
            # state. Do not accept that stale field from another window.
            role = node.get_role()
            if role in (Atspi.Role.FRAME, Atspi.Role.DIALOG):
                if not states.contains(Atspi.StateType.ACTIVE):
                    continue
                editable_parent = None
            if (role == Atspi.Role.PASSWORD_TEXT
                    or (role != Atspi.Role.TERMINAL
                        and states.contains(Atspi.StateType.EDITABLE)
                        and node.get_text_iface() is not None)):
                editable_parent = node
            if states.contains(Atspi.StateType.FOCUSED) and editable_parent is not None:
                # Electron may focus a text child of a contenteditable composer.
                # A focused readonly document is not itself a text-entry target;
                # continue through its children to find the focused editor.
                candidates[_node_identity(editable_parent)] = editable_parent
                if len(candidates) > 1:
                    return None
            count = node.get_child_count()
            if count > 1200 - len(seen) - len(queue):
                return None
            for index in range(count):
                child = node.get_child_at_index(index)
                if child is not None:
                    queue.append((child, editable_parent))
        except Exception:
            continue
    # A truncated traversal cannot prove that the target is unique.
    if queue or len(candidates) != 1:
        return None
    return next(iter(candidates.values()))


def _find_field(Atspi, pid: int):
    node = _find_editor(Atspi, pid)
    if node is None or not hasattr(Atspi, "Hypertext"):
        return node
    try:
        for _ in range(8):
            if node.get_role() == Atspi.Role.PASSWORD_TEXT:
                return node
            text = Atspi.Text.get_text(node, 0, -1)
            if "\ufffc" not in text:
                return node
            caret = Atspi.Text.get_caret_offset(node)
            offset = caret if caret < len(text) and text[caret] == "\ufffc" else caret - 1
            if offset < 0 or offset >= len(text) or text[offset] != "\ufffc":
                return None
            index = Atspi.Hypertext.get_link_index(node, offset)
            if index < 0:
                return None
            link = Atspi.Hypertext.get_link(node, index)
            child = Atspi.Hyperlink.get_object(link, 0)
            if (child is None or not child.get_state_set().contains(Atspi.StateType.EDITABLE)
                    or child.get_text_iface() is None or Atspi.Text.get_caret_offset(child) < 0):
                return None
            node = child
    except Exception:
        return None
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
    if request["action"] == "replace":
        return _verified_paste(Atspi, window, node, field, expected, request)
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
    node = _find_field(Atspi, window.pid)
    if node is None:
        raise FocusError("The focused field changed; dictation stopped")
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
        if focused_window() != window:
            raise FocusError("Focus or caret changed; insertion stopped")
        node = _find_field(Atspi, window.pid)
        if node is None or _read_field(node, Atspi) != changed:
            raise FocusError("Focus or caret changed; insertion stopped")
        if not Atspi.Text.set_caret_offset(node, target_caret):
            raise FocusError("Text inserted; the caret could not advance safely")
        changed = _read_field(node, Atspi)
    if (changed.identity != field.identity or changed.text != wanted
            or changed.caret != target_caret or changed.selection):
        raise FocusError("Correction paused; check the draft before pasting the final text")
    return {"status": "ok", "field": asdict(changed)}


_clipboard_keeper = None
_operation_deadline = None


def _verified_paste(Atspi, window, node, field, expected, request):
    global _clipboard_keeper
    # The helper is also executed directly by system Python outside the venv.
    if __package__:
        from .clipboard import Clipboard, ClipboardError, pump_until
    else:
        from clipboard import Clipboard, ClipboardError, pump_until

    start, addition = request["start"], request["text"]
    if (field != expected or field.selection or not field.paste_editable
            or type(start) is not int or not 0 <= start <= field.caret
            or not isinstance(addition, str)
            or any(ord(char) < 32 or ord(char) == 127 for char in addition)):
        raise FocusError("The field or replacement range is no longer valid")
    wanted = field.text[:start] + addition + field.text[field.caret:]
    target_caret = start + len(addition)
    # Codex currently runs through XWayland on GNOME. Use the same X11 paste
    # transport as the verified Chromium path for that exact client type.
    try:
        clip = Clipboard("x11" if window.client_type == "x11" else window.backend)
    except ClipboardError as exc:
        raise FocusError(str(exc)) from exc
    selected = False
    paste_started = False
    confirmed = False

    def check_deadline():
        if _operation_deadline is not None and time.monotonic() >= _operation_deadline:
            raise FocusError("Editing timed out before paste; no input was sent")

    def current_node():
        if focused_window() != window:
            raise FocusError("Focus changed; dictation stopped")
        current = _find_field(Atspi, window.pid)
        same = current is not None and _node_identity(current) == field.identity
        if current is not None and not same and field.empty_editor and field.caret == 0:
            same = _editor_identity(current, Atspi) == (field.editor_identity or field.identity)
        if not same:
            raise FocusError("The focused field changed; dictation stopped")
        return current

    def selection_matches(current):
        value = _read_field(current, Atspi)
        if value.text != field.text:
            return False
        if start == field.caret:
            return value == field
        if not value.selection or Atspi.Text.get_n_selections(current) != 1:
            return False
        bounds = Atspi.Text.get_selection(current, 0)
        return sorted((bounds.start_offset, bounds.end_offset)) == [start, field.caret]

    try:
        check_deadline()
        clip.snapshot()
        node = current_node()
        if _read_field(node, Atspi) != field:
            raise FocusError("The field or caret changed before editing")
        check_deadline()
        clip.set_text(addition)
        if start != field.caret:
            if not Atspi.Text.add_selection(node, start, field.caret):
                raise FocusError("This field cannot select a verified correction range")
            selected = True
        if not selection_matches(current_node()):
            raise FocusError("The correction range could not be verified")
        check_deadline()
        paste_started = True
        clip.paste()
        result = None

        def applied():
            nonlocal result
            result = _read_field(current_node(), Atspi)
            # Chromium removes the sole <br> placeholder when first pasting
            # into an empty rich editor. It is not pre-existing user text.
            matched = result.text == wanted or (
                field.empty_editor and field.text == "\n" and start == field.caret == 0
                and result.text == addition
            )
            return (matched and result.caret == target_caret
                    and not result.selection)

        if not pump_until(applied, timeout=1.0):
            raise FocusError("Paste completion could not be verified. Editing stopped; the clipboard keeps this draft for a late paste.")
        confirmed = True
        return {"status": "ok", "field": asdict(result)}
    except ClipboardError as exc:
        raise FocusError(str(exc)) from exc
    finally:
        # Never undo an independent clipboard change or move a user's new caret.
        try:
            if selected and not paste_started and selection_matches(current_node()):
                Atspi.Text.set_caret_offset(node, field.caret)
        except Exception:
            pass
        if paste_started and not confirmed:
            # A busy renderer may consume Paste after the timeout. Keep the
            # submitted text available instead of letting it paste unrelated
            # clipboard data after restoration.
            if clip.keep_pending():
                _clipboard_keeper = clip
        elif clip.restore():
            _clipboard_keeper = clip


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        _operation_deadline = time.monotonic() + 8
        response = _accessibility_request(request)
    except FocusError as exc:
        response = {"status": "blocked", "reason": str(exc)}
    except Exception:
        response = {"status": "unavailable"}
    try:
        print(json.dumps(response), flush=True)
    except BrokenPipeError:
        pass
    if _clipboard_keeper is not None:
        _clipboard_keeper.serve_restored()
