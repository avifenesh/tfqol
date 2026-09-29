"""A temporary plain-text clipboard owner for verified accessibility edits.

The caller verifies the exact focused field and selection before sending the
fixed paste chord, and verifies the edited text afterwards. Run this module in
the system-Python accessibility helper, where Gdk 4 is available. Text is never
passed to a keyboard command or logged.
"""
from __future__ import annotations

import ctypes
import math
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Callable


class ClipboardError(RuntimeError):
    pass


def _x11_selection_owner(display_name: str) -> int:
    """Read the X server's CLIPBOARD owner, independently of Gdk metadata.

    Only a local display is allowed. The accessibility helper's process deadline
    also bounds an unresponsive X server, before any clipboard mutation.
    """
    if not re.fullmatch(r"(?:unix/|unix)?:\d+(?:\.\d+)?", display_name):
        raise ClipboardError("Clipboard ownership requires a local X11 display")
    try:
        x11 = ctypes.CDLL("libX11.so.6")
    except OSError as exc:
        raise ClipboardError("Cannot check the original clipboard owner") from exc
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x11.XInternAtom.restype = ctypes.c_ulong
    x11.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XGetSelectionOwner.restype = ctypes.c_ulong
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.restype = ctypes.c_int
    display = x11.XOpenDisplay(display_name.encode("ascii"))
    if not display:
        raise ClipboardError("Cannot check the original clipboard owner")
    try:
        atom = x11.XInternAtom(display, b"CLIPBOARD", False)
        if not atom:
            raise ClipboardError("Cannot identify the clipboard selection")
        return int(x11.XGetSelectionOwner(display, atom))
    finally:
        x11.XCloseDisplay(display)


def _load_gdk():
    try:
        import gi
        gi.require_version("Gdk", "4.0")
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gdk, Gio, GLib, Gtk
        return Gdk, Gio, GLib, Gtk
    except (ImportError, ValueError) as exc:
        raise ClipboardError("Gdk 4 clipboard support is unavailable") from exc


def pump_until(predicate: Callable[[], bool], timeout: float = 1.0,
               *, _glib=None) -> bool:
    """Dispatch native events until a condition holds or the deadline expires.

    The condition runs on the event-loop thread. A timeout is false; exceptions
    from the condition propagate, instead of disappearing in a GLib callback.
    """
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("Clipboard timeout must be finite and nonnegative")
    if predicate():
        return True
    if timeout == 0:
        return False
    GLib = _glib if _glib is not None else _load_gdk()[2]
    loop = GLib.MainLoop()
    state = {"matched": False, "error": None, "tick": 0, "deadline": 0}

    def check():
        try:
            state["matched"] = bool(predicate())
        except Exception as exc:
            state["error"] = exc
        if state["matched"] or state["error"] is not None:
            state["tick"] = 0
            loop.quit()
            return False
        return True

    def expired():
        state["deadline"] = 0
        loop.quit()
        return False

    state["tick"] = GLib.timeout_add(10, check)
    state["deadline"] = GLib.timeout_add(max(1, math.ceil(timeout * 1000)), expired)
    try:
        loop.run()
    finally:
        for name in ("tick", "deadline"):
            if state[name]:
                GLib.source_remove(state[name])
    if state["error"] is not None:
        raise state["error"]
    return state["matched"]


class Clipboard:
    """Capture, temporarily replace, and conditionally restore the clipboard.

    This is a single-edit object. ``snapshot`` and ``set_text`` fail before any
    clipboard mutation if the original data cannot be preserved completely.
    After ``restore``, the helper must call ``serve_restored`` before exiting:
    X11 and Wayland need this process to answer requests for the restored data.
    """

    MAX_FORMATS = 32
    MAX_BYTES = 16 * 1024 * 1024
    READ_SIZE = 64 * 1024

    def __init__(self, window_backend: str):
        if window_backend not in ("x11", "gnome"):
            raise ClipboardError("Unsupported clipboard desktop")
        sender = "xdotool" if window_backend == "x11" else "ydotool"
        if shutil.which(sender) is None:
            raise ClipboardError(f"Install {sender} to paste into this editor")
        self._backend = window_backend
        self._Gdk, self._Gio, self._GLib, Gtk = _load_gdk()
        self._env = os.environ.copy()
        # A background Wayland client has no input serial to own the selection.
        # GNOME's XWayland bridge provides clipboard ownership without focusing
        # a new surface; the desktop backend still selects the ydotool chord.
        display_name = self._env.get("DISPLAY")
        if not display_name:
            if window_backend == "gnome":
                raise ClipboardError("GNOME clipboard editing requires the XWayland DISPLAY bridge")
            raise ClipboardError("The target desktop display is unavailable")
        self._Gdk.set_allowed_backends("x11")
        # Gdk 4 aborts if Display.open is called before GTK initializes. Limit
        # the backend first, so GTK cannot open an unrelated fallback display.
        if not Gtk.init_check():
            raise ClipboardError("Cannot initialize the target clipboard display")
        self._display = self._Gdk.Display.open(display_name)
        if self._display is None:
            raise ClipboardError("Cannot open the target clipboard display")
        self._clipboard = self._display.get_clipboard()
        self._generation = 0
        self._snapshot_generation: int | None = None
        self._snapshot_owner: int | None = None
        self._original: tuple[tuple[str, bytes], ...] | None = None
        self._provider = None
        self._restored_provider = None
        self._published = False
        self._clipboard.connect("changed", self._changed)

    def _changed(self, _clipboard):
        self._generation += 1

    def _selection_owner(self) -> int:
        return _x11_selection_owner(self._env["DISPLAY"])

    def _dispatch_events(self) -> None:
        """Process queued owner changes before relying on Gdk's local cache."""
        if self._display.is_closed():
            raise ClipboardError("The clipboard display closed")
        self._display.sync()
        pending = {"source": 0, "ready": False}

        def ready():
            pending["source"] = 0
            pending["ready"] = True
            return False

        pending["source"] = self._GLib.idle_add(ready)
        try:
            if not pump_until(lambda: pending["ready"], 0.25, _glib=self._GLib):
                raise ClipboardError("The clipboard display is not responding")
        finally:
            if pending["source"]:
                self._GLib.source_remove(pending["source"])

    def _formats(self) -> tuple[str, ...]:
        formats = self._clipboard.get_formats()
        # Snapshot only the owner's advertised formats. Gdk's serialization
        # union can add text/plain even when that X11 target is unavailable.
        mimes = tuple(formats.get_mime_types() or ())
        if len(mimes) > self.MAX_FORMATS:
            raise ClipboardError("The clipboard has too many formats to preserve")
        if any(not isinstance(mime, str) or not mime or len(mime) > 256 for mime in mimes):
            raise ClipboardError("The clipboard has an unsupported format")
        if not mimes and formats.get_gtypes():
            raise ClipboardError("The original clipboard cannot be preserved")
        return tuple(dict.fromkeys(mimes))

    def _read_mime(self, mime: str, remaining: int, timeout: float) -> bytes:
        GLib = self._GLib
        cancel = self._Gio.Cancellable()
        state = {"done": False, "error": None, "stream": None}
        output = bytearray()

        def close_stream():
            stream = state["stream"]
            if stream is not None:
                state["stream"] = None

                def closed(source, result, _data=None):
                    try:
                        source.close_finish(result)
                    except Exception:
                        pass

                stream.close_async(GLib.PRIORITY_DEFAULT, None, closed, None)

        def fail():
            state["error"] = ClipboardError("Cannot preserve the original clipboard")
            state["done"] = True
            close_stream()

        def next_chunk():
            # Read one extra byte to distinguish an exact fit from overflow.
            count = min(self.READ_SIZE, remaining - len(output) + 1)
            state["stream"].read_bytes_async(
                count, GLib.PRIORITY_DEFAULT, cancel, got_chunk, None,
            )

        def got_chunk(source, result, _data=None):
            try:
                chunk = source.read_bytes_finish(result).get_data() or b""
                if state["done"]:
                    return
                if len(output) + len(chunk) > remaining:
                    state["error"] = ClipboardError("The clipboard is too large to preserve")
                    state["done"] = True
                    close_stream()
                elif not chunk:
                    state["done"] = True
                    close_stream()
                else:
                    output.extend(chunk)
                    next_chunk()
            except Exception:
                fail()

        def opened(source, result, _data=None):
            try:
                stream, actual_mime = source.read_finish(result)
                state["stream"] = stream
                if state["done"]:
                    close_stream()
                    return
                if stream is None or actual_mime != mime:
                    fail()
                    return
                next_chunk()
            except Exception:
                fail()

        self._clipboard.read_async([mime], GLib.PRIORITY_DEFAULT, cancel, opened, None)
        if not pump_until(lambda: state["done"], timeout, _glib=GLib):
            state["done"] = True
            cancel.cancel()
            close_stream()
            raise ClipboardError("Timed out preserving the original clipboard")
        if state["error"] is not None:
            raise state["error"]
        return bytes(output)

    def snapshot(self, timeout: float = 1.5) -> bool:
        if self._published:
            raise ClipboardError("The clipboard snapshot is already in use")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Clipboard timeout must be finite and positive")
        self._original = None
        self._snapshot_owner = None
        deadline = time.monotonic() + timeout
        owner = self._selection_owner()
        self._dispatch_events()
        if owner:
            # Gdk initially reports no formats while X11 TARGETS is still in
            # flight. A selection owner proves that this is not an empty copy.
            def formats_ready():
                if self._display.is_closed():
                    raise ClipboardError("The clipboard display closed")
                return bool(self._formats())

            remaining_time = max(0.0, deadline - time.monotonic())
            if not pump_until(formats_ready, remaining_time, _glib=self._GLib):
                raise ClipboardError("Timed out reading the original clipboard formats")
            mimes = self._formats()
        else:
            mimes = ()
        if self._selection_owner() != owner:
            raise ClipboardError("The clipboard changed while preserving it")
        # Initial TARGETS arrival emits changed even when the owner is stable.
        # Begin the mutation guard after those metadata updates are complete.
        generation = self._generation
        original = []
        total = 0
        for mime in mimes:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise ClipboardError("Timed out preserving the original clipboard")
            data = self._read_mime(mime, self.MAX_BYTES - total, remaining_time)
            if (self._generation != generation or self._display.is_closed()
                    or self._selection_owner() != owner):
                raise ClipboardError("The clipboard changed while preserving it")
            original.append((mime, data))
            total += len(data)
        if (self._generation != generation or self._formats() != mimes
                or self._selection_owner() != owner):
            raise ClipboardError("The clipboard changed while preserving it")
        self._original = tuple(original)
        self._snapshot_generation = generation
        self._snapshot_owner = owner
        return True

    def _new_provider(self, entries):
        providers = [self._Gdk.ContentProvider.new_for_bytes(
            mime, self._GLib.Bytes.new(data),
        ) for mime, data in entries]
        if not providers:
            return None
        if len(providers) == 1:
            return providers[0]
        return self._Gdk.ContentProvider.new_union(providers)

    def set_text(self, text: str) -> bool:
        if self._published:
            raise ClipboardError("The temporary clipboard has already been used")
        data = text.encode("utf-8")
        if len(data) > self.MAX_BYTES:
            raise ClipboardError("The replacement text is too large")
        if self._original is None:
            self.snapshot()
        self._dispatch_events()
        if (self._generation != self._snapshot_generation or self._display.is_closed()
                or self._selection_owner() != self._snapshot_owner):
            raise ClipboardError("The clipboard changed before the edit")
        provider = self._new_provider((
            ("text/plain;charset=utf-8", data), ("text/plain", data),
        ))
        if not self._clipboard.set_content(provider):
            raise ClipboardError("Cannot publish the temporary clipboard")
        self._provider = provider
        self._published = True
        return True

    def _owns(self, provider) -> bool:
        return (provider is not None and not self._display.is_closed()
                and self._clipboard.is_local()
                and self._clipboard.get_content() == provider)

    def paste(self, timeout: float = 1.2) -> bool:
        """Send only Ctrl+V while the event loop serves clipboard requests."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Paste timeout must be finite and positive")
        self._dispatch_events()
        if not self._owns(self._provider):
            raise ClipboardError("The temporary clipboard is no longer current")
        if self._backend == "x11":
            argv = ["xdotool", "key", "--clearmodifiers", "ctrl+v"]
        else:
            argv = ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"]
            runtime = self._env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
            self._env.setdefault("YDOTOOL_SOCKET", os.path.join(runtime, ".ydotool_socket"))
        done = threading.Event()
        failed = []

        def send_chord():
            try:
                result = subprocess.run(
                    argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, env=self._env, timeout=timeout,
                )
                if result.returncode:
                    failed.append(True)
            except (OSError, subprocess.TimeoutExpired):
                failed.append(True)
            finally:
                if failed:
                    # A timed-out key sender may have pressed Ctrl without
                    # reaching its key-up events. Release only this chord's
                    # virtual keys; never replay Paste or type fallback text.
                    release = (["xdotool", "keyup", "v", "Control_L"]
                               if self._backend == "x11" else
                               ["ydotool", "key", "47:0", "29:0"])
                    try:
                        subprocess.run(release, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       env=self._env, timeout=0.4)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                done.set()

        threading.Thread(target=send_chord, daemon=True).start()
        if not pump_until(done.is_set, timeout + 0.5, _glib=self._GLib) or failed:
            raise ClipboardError("Cannot paste into the verified field")
        return True

    def restore(self) -> bool:
        """Restore original bytes only while our temporary provider still owns it."""
        if self._display.is_closed():
            return False
        self._dispatch_events()
        if not self._owns(self._provider):
            return False
        provider = self._new_provider(self._original)
        if not self._clipboard.set_content(provider):
            raise ClipboardError("Cannot restore the original clipboard")
        self._provider = None
        self._restored_provider = provider
        return True

    def keep_pending(self) -> bool:
        """Keep submitted text available when a late Paste is still possible."""
        if not self._owns(self._provider):
            return False
        self._restored_provider = self._provider
        return True

    def serve_restored(self) -> None:
        """Keep restored bytes available until another owner replaces them."""
        provider = self._restored_provider
        if not self._owns(provider):
            return
        stored = {}

        def saved(source, result, _data=None):
            try:
                stored["ok"] = source.store_finish(result)
            except Exception:
                stored["ok"] = False

        try:
            self._clipboard.store_async(self._GLib.PRIORITY_DEFAULT, None, saved, None)
            pump_until(lambda: "ok" in stored, timeout=1.0, _glib=self._GLib)
            if stored.get("ok"):
                return
        except (AttributeError, TypeError):
            pass  # Platforms without a clipboard manager need a live owner.
        loop = self._GLib.MainLoop()

        def changed(_clipboard):
            if not self._owns(provider):
                loop.quit()

        def closed(_display, _is_error):
            loop.quit()

        changed_id = self._clipboard.connect("changed", changed)
        closed_id = self._display.connect("closed", closed)
        try:
            if self._owns(provider):
                loop.run()
        finally:
            self._clipboard.disconnect(changed_id)
            self._display.disconnect(closed_id)
