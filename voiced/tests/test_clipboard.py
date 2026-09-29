"""Clipboard safety and native event dispatch without touching a real desktop."""
import heapq
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from voiced.clipboard import Clipboard, ClipboardError, _x11_selection_owner, pump_until


class Bytes:
    def __init__(self, data):
        self.data = data

    def get_data(self):
        return self.data


class Loop:
    def __init__(self, glib):
        self.glib = glib
        self.running = False

    def run(self):
        self.running = True
        while self.running:
            if not self.glib.queue:
                raise AssertionError("No event can end the fake main loop")
            at, source, interval, callback, args = heapq.heappop(self.glib.queue)
            if source not in self.glib.active:
                continue
            self.glib.now = at
            if callback(*args):
                heapq.heappush(self.glib.queue, (at + interval, source, interval, callback, args))
            else:
                self.glib.active.remove(source)

    def quit(self):
        self.running = False


class GLib:
    PRIORITY_DEFAULT = 0
    Bytes = SimpleNamespace(new=Bytes)

    def __init__(self):
        self.now = 0
        self.next_source = 1
        self.queue = []
        self.active = set()

    def MainLoop(self):
        return Loop(self)

    def timeout_add(self, delay, callback, *args):
        source = self.next_source
        self.next_source += 1
        self.active.add(source)
        interval = delay / 1000
        heapq.heappush(self.queue, (self.now + interval, source, interval, callback, args))
        return source

    def idle_add(self, callback, *args):
        return self.timeout_add(0, callback, *args)

    def source_remove(self, source):
        self.active.remove(source)


class Signals:
    def __init__(self):
        self.signals = {}
        self.next_handler = 1

    def connect(self, signal, callback):
        handler = self.next_handler
        self.next_handler += 1
        self.signals[handler] = (signal, callback)
        return handler

    def disconnect(self, handler):
        del self.signals[handler]

    def emit(self, signal, *args):
        for name, callback in tuple(self.signals.values()):
            if name == signal:
                callback(self, *args)


class Formats:
    def __init__(self, data):
        self.data = data

    def union_serialize_mime_types(self):
        return self

    def get_mime_types(self):
        return list(self.data)

    def get_gtypes(self):
        return []


class Stream:
    def __init__(self, glib, data):
        self.glib = glib
        self.data = data
        self.position = 0
        self.closed = False

    def read_bytes_async(self, count, priority, cancel, callback, user_data):
        data = self.data[self.position:self.position + count]
        self.position += len(data)
        self.glib.idle_add(callback, self, Bytes(data), user_data)

    def read_bytes_finish(self, result):
        return result

    def close_async(self, priority, cancel, callback, user_data):
        self.closed = True
        self.glib.idle_add(callback, self, None, user_data)

    def close_finish(self, result):
        return True


class NativeClipboard(Signals):
    def __init__(self, glib, original):
        super().__init__()
        self.glib = glib
        self.data = original
        self.provider = None
        self.writes = []
        self.streams = []
        self.stall = False
        self.last_cancel = None
        self.owner = 17 if original else 0
        self.formats_ready = True

    def get_formats(self):
        return Formats(self.data if self.formats_ready else {})

    def read_async(self, mimes, priority, cancel, callback, user_data):
        self.last_cancel = cancel
        if self.stall:
            return
        mime = mimes[0]
        stream = Stream(self.glib, self.data[mime])
        self.streams.append(stream)
        self.glib.idle_add(callback, self, (stream, mime), user_data)

    def read_finish(self, result):
        return result

    def set_content(self, provider):
        self.writes.append(provider)
        self.provider = provider
        self.data = provider.data if provider is not None else {}
        self.owner = 100 if provider is not None else 0
        self.emit("changed")
        return True

    def get_content(self):
        return self.provider

    def is_local(self):
        return self.provider is not None

    def external_change(self, data):
        self.provider = None
        self.data = data
        self.owner = self.owner + 1 if data else 0
        self.emit("changed")


class Display(Signals):
    def __init__(self, clipboard):
        super().__init__()
        self.clipboard = clipboard
        self.closed = False

    def is_closed(self):
        return self.closed

    def get_clipboard(self):
        return self.clipboard

    def sync(self):
        pass


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.original = {
            "text/plain;charset=utf-8": "original café".encode(),
            "text/html": b"<strong>original caf&#233;</strong>",
            "image/png": b"\x89PNG\r\n\x1a\n\x00\x01\xff",
        }
        self.glib = GLib()
        self.native = NativeClipboard(self.glib, self.original.copy())
        self.display = Display(self.native)
        self.gdk = SimpleNamespace(
            set_allowed_backends=Mock(),
            Display=SimpleNamespace(open=Mock(return_value=self.display)),
            ContentProvider=SimpleNamespace(
                new_for_bytes=lambda mime, data: SimpleNamespace(data={mime: data.get_data()}),
                new_union=lambda providers: SimpleNamespace(data={
                    mime: data for provider in providers for mime, data in provider.data.items()
                }),
            ),
        )
        self.gio = SimpleNamespace(Cancellable=lambda: SimpleNamespace(cancel=Mock()))
        self.gtk = SimpleNamespace(init_check=Mock(return_value=True))
        patches = [
            patch("voiced.clipboard._load_gdk", return_value=(self.gdk, self.gio, self.glib, self.gtk)),
            patch("voiced.clipboard._x11_selection_owner", side_effect=lambda _display: self.native.owner),
            patch("voiced.clipboard.time.monotonic", side_effect=lambda: self.glib.now),
            patch.dict("voiced.clipboard.os.environ", {
                "DISPLAY": ":isolated", "WAYLAND_DISPLAY": "wayland-isolated",
                "XDG_RUNTIME_DIR": "/run/user/test",
            }, clear=True),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_preserves_all_mime_bytes_exactly(self):
        clipboard = Clipboard("x11")
        self.assertTrue(clipboard.snapshot())
        self.assertTrue(all(stream.closed for stream in self.native.streams))
        clipboard.set_text("new text")
        self.assertEqual(self.native.data["text/plain;charset=utf-8"], b"new text")
        self.assertNotIn("text/html", self.native.data)
        self.assertTrue(clipboard.restore())
        self.assertEqual(self.native.data, self.original)

    def test_snapshot_does_not_invent_serialized_formats(self):
        with patch.object(Formats, "union_serialize_mime_types",
                          side_effect=AssertionError("would add unsupported text/plain")):
            clipboard = Clipboard("x11")
            clipboard.set_text("new")
            clipboard.restore()
        self.assertEqual(self.native.data, self.original)
        self.assertEqual(len(self.native.streams), len(self.original))

    def test_does_not_overwrite_external_copy(self):
        clipboard = Clipboard("x11")
        clipboard.set_text("dictation")
        external = {"text/html": b"<b>copied later</b>"}
        self.native.external_change(external)
        self.assertFalse(clipboard.restore())
        self.assertEqual(self.native.data, external)
        self.assertEqual(len(self.native.writes), 1)

    def test_dispatches_pending_external_change_before_restore(self):
        clipboard = Clipboard("x11")
        clipboard.set_text("dictation")
        external = {"text/plain": b"copied later"}
        self.glib.idle_add(self.native.external_change, external)
        self.assertFalse(clipboard.restore())
        self.assertEqual(self.native.data, external)

    def test_changed_clipboard_between_snapshot_and_set_is_preserved(self):
        clipboard = Clipboard("x11")
        clipboard.snapshot()
        self.glib.idle_add(self.native.external_change, {"text/plain": b"later"})
        with self.assertRaisesRegex(ClipboardError, "changed"):
            clipboard.set_text("dictation")
        self.assertEqual(self.native.writes, [])

    def test_changed_clipboard_during_snapshot_prevents_mutation(self):
        clipboard = Clipboard("x11")
        read = self.native.read_async

        def changed_read(*args):
            read(*args)
            self.glib.idle_add(self.native.external_change, {"text/plain": b"later"})

        self.native.read_async = changed_read
        with self.assertRaisesRegex(ClipboardError, "changed"):
            clipboard.set_text("dictation")
        self.assertEqual(self.native.writes, [])

    def test_total_byte_limit_rejects_before_mutation(self):
        self.native.data = {"text/plain": b"12345", "text/html": b"67890"}
        clipboard = Clipboard("x11")
        clipboard.MAX_BYTES = 9
        with self.assertRaisesRegex(ClipboardError, "too large"):
            clipboard.set_text("new")
        self.assertEqual(self.native.writes, [])
        self.assertTrue(all(stream.closed for stream in self.native.streams))

    def test_exact_byte_limit_is_allowed(self):
        self.native.data = {"application/octet-stream": b"12345"}
        clipboard = Clipboard("x11")
        clipboard.MAX_BYTES = 5
        clipboard.set_text("new")
        clipboard.restore()
        self.assertEqual(self.native.data, {"application/octet-stream": b"12345"})

    def test_format_limit_rejects_before_read_or_mutation(self):
        self.native.data = {f"application/x-test-{i}": b"x" for i in range(33)}
        with self.assertRaisesRegex(ClipboardError, "too many formats"):
            Clipboard("x11").set_text("new")
        self.assertEqual(self.native.streams, [])
        self.assertEqual(self.native.writes, [])

    def test_stalled_owner_times_out_and_cancels_before_mutation(self):
        self.native.stall = True
        with self.assertRaisesRegex(ClipboardError, "Timed out"):
            Clipboard("x11").set_text("new")
        self.native.last_cancel.cancel.assert_called_once()
        self.assertEqual(self.native.writes, [])

    def test_empty_original_and_empty_replacement_are_supported(self):
        self.native.data = {}
        self.native.owner = 0
        clipboard = Clipboard("x11")
        clipboard.set_text("")
        self.assertEqual(self.native.data["text/plain;charset=utf-8"], b"")
        clipboard.restore()
        self.assertEqual(self.native.data, {})
        clipboard.serve_restored()

    def test_delayed_formats_are_preserved_after_metadata_arrives(self):
        self.native.formats_ready = False

        def metadata_arrived():
            self.native.formats_ready = True
            self.native.emit("changed")

        self.glib.timeout_add(30, metadata_arrived)
        clipboard = Clipboard("x11")
        clipboard.set_text("new")
        clipboard.restore()
        self.assertEqual(self.native.data, self.original)
        self.assertEqual(len(self.native.streams), 3)

    def test_unresolved_formats_never_become_an_empty_snapshot(self):
        self.native.formats_ready = False
        clipboard = Clipboard("x11")
        with self.assertRaisesRegex(ClipboardError, "Timed out.*formats"):
            clipboard.snapshot(timeout=0.05)
        self.assertIsNone(clipboard._original)
        self.assertEqual(self.native.writes, [])
        self.assertEqual(self.native.data, self.original)

    def test_owner_change_during_metadata_load_aborts(self):
        self.native.formats_ready = False

        def replaced():
            self.native.formats_ready = True
            self.native.external_change({"text/plain": b"new owner"})

        self.glib.timeout_add(30, replaced)
        with self.assertRaisesRegex(ClipboardError, "changed"):
            Clipboard("x11").set_text("new")
        self.assertEqual(self.native.writes, [])
        self.assertEqual(self.native.streams, [])

    def test_owner_change_without_gdk_event_prevents_publication(self):
        clipboard = Clipboard("x11")
        clipboard.snapshot()
        self.native.owner += 1
        with self.assertRaisesRegex(ClipboardError, "changed"):
            clipboard.set_text("new")
        self.assertEqual(self.native.writes, [])

    def test_new_owner_after_empty_snapshot_prevents_publication(self):
        self.native.data = {}
        self.native.owner = 0
        clipboard = Clipboard("x11")
        clipboard.snapshot()
        self.native.owner = 18
        with self.assertRaisesRegex(ClipboardError, "changed"):
            clipboard.set_text("new")
        self.assertEqual(self.native.writes, [])

    def test_restored_provider_serves_until_another_copy(self):
        clipboard = Clipboard("x11")
        clipboard.set_text("new")
        clipboard.restore()
        self.glib.idle_add(self.native.external_change, {"text/plain": b"later"})
        clipboard.serve_restored()
        self.assertEqual(self.native.data, {"text/plain": b"later"})
        self.assertEqual(len(self.native.signals), 1)
        self.assertEqual(self.display.signals, {})

    def test_restored_provider_stops_on_display_close(self):
        clipboard = Clipboard("x11")
        clipboard.set_text("new")
        clipboard.restore()

        def close():
            self.display.closed = True
            self.display.emit("closed", False)

        self.glib.idle_add(close)
        clipboard.serve_restored()
        self.assertEqual(self.display.signals, {})

    def _paste(self, backend, *, error=None):
        clipboard = Clipboard(backend)
        clipboard.set_text("שלום 🙂 --key alt+2\n$(not a command)")
        worker = Mock(side_effect=lambda target, **_: SimpleNamespace(
            start=lambda: self.glib.idle_add(target),
        ))
        with patch("voiced.clipboard.threading.Thread", worker), \
                patch("voiced.clipboard.subprocess.run", return_value=SimpleNamespace(returncode=0),
                      side_effect=error) as command:
            if error:
                with self.assertRaisesRegex(ClipboardError, "Cannot paste"):
                    clipboard.paste()
            else:
                self.assertTrue(clipboard.paste())
            return command

    def test_x11_uses_only_fixed_chord_and_explicit_display(self):
        command = self._paste("x11")
        self.assertEqual(command.call_args.args, (["xdotool", "key", "--clearmodifiers", "ctrl+v"],))
        self.assertEqual(command.call_args.kwargs["env"]["DISPLAY"], ":isolated")
        self.assertEqual(command.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.gdk.set_allowed_backends.assert_called_once_with("x11")

    def test_gnome_uses_xwayland_clipboard_with_ydotool_chord(self):
        command = self._paste("gnome")
        self.assertEqual(command.call_args.args, (["ydotool", "key", "29:1", "47:1", "47:0", "29:0"],))
        self.assertEqual(command.call_args.kwargs["env"]["YDOTOOL_SOCKET"],
                         "/run/user/test/.ydotool_socket")
        self.gdk.set_allowed_backends.assert_called_once_with("x11")
        self.gdk.Display.open.assert_called_once_with(":isolated")

    def test_explicit_ydotool_socket_is_preserved(self):
        with patch.dict("voiced.clipboard.os.environ", {"YDOTOOL_SOCKET": "/run/user/test/own.sock"}):
            command = self._paste("gnome")
        self.assertEqual(command.call_args.kwargs["env"]["YDOTOOL_SOCKET"],
                         "/run/user/test/own.sock")

    def test_failed_paste_is_reported_without_keyboard_retry(self):
        command = self._paste("x11", error=subprocess.TimeoutExpired("xdotool", 1))
        self.assertEqual(command.call_count, 2)
        self.assertEqual(command.call_args_list[0].args[0],
                         ["xdotool", "key", "--clearmodifiers", "ctrl+v"])
        self.assertEqual(command.call_args_list[1].args[0],
                         ["xdotool", "keyup", "v", "Control_L"])

    def test_failed_gnome_paste_releases_only_its_virtual_keys(self):
        command = self._paste("gnome", error=subprocess.TimeoutExpired("ydotool", 1))
        self.assertEqual(command.call_count, 2)
        self.assertEqual(command.call_args_list[1].args[0],
                         ["ydotool", "key", "47:0", "29:0"])

    def test_paste_refuses_changed_clipboard_without_sending_keys(self):
        clipboard = Clipboard("x11")
        clipboard.set_text("new")
        self.native.external_change({"text/plain": b"later"})
        with patch("voiced.clipboard.subprocess.run") as command:
            with self.assertRaisesRegex(ClipboardError, "no longer current"):
                clipboard.paste()
        command.assert_not_called()

    def test_missing_display_does_not_use_default_or_mutate(self):
        with patch.dict("voiced.clipboard.os.environ", {}, clear=True):
            with self.assertRaisesRegex(ClipboardError, "display is unavailable"):
                Clipboard("x11")
        self.gdk.Display.open.assert_not_called()
        self.gtk.init_check.assert_not_called()
        self.assertEqual(self.native.writes, [])

    def test_gtk_initializes_after_backend_choice_and_before_display_open(self):
        ordered = Mock()
        ordered.attach_mock(self.gdk.set_allowed_backends, "backend")
        ordered.attach_mock(self.gtk.init_check, "initialize")
        ordered.attach_mock(self.gdk.Display.open, "open")
        Clipboard("x11")
        self.assertEqual([call[0] for call in ordered.mock_calls],
                         ["backend", "initialize", "open"])

    def test_gnome_without_display_fails_before_initialization_or_mutation(self):
        with patch.dict("voiced.clipboard.os.environ", {"WAYLAND_DISPLAY": "wayland-isolated"},
                        clear=True):
            with self.assertRaisesRegex(ClipboardError, "XWayland DISPLAY bridge"):
                Clipboard("gnome")
        self.gtk.init_check.assert_not_called()
        self.gdk.Display.open.assert_not_called()
        self.assertEqual(self.native.writes, [])

    def test_gtk_initialization_failure_does_not_open_display_or_mutate(self):
        self.gtk.init_check.return_value = False
        with self.assertRaisesRegex(ClipboardError, "Cannot initialize"):
            Clipboard("x11")
        self.gdk.Display.open.assert_not_called()
        self.assertEqual(self.native.writes, [])


class PumpTests(unittest.TestCase):
    def test_native_events_run_while_waiting_for_readback(self):
        glib = GLib()
        state = []
        glib.idle_add(lambda: state.append("readback"))
        self.assertTrue(pump_until(lambda: bool(state), 1.0, _glib=glib))
        self.assertEqual(glib.active, set())

    def test_timeout_removes_sources(self):
        glib = GLib()
        self.assertFalse(pump_until(lambda: False, 0.04, _glib=glib))
        self.assertEqual(glib.active, set())
        self.assertAlmostEqual(glib.now, 0.04)

    def test_callback_exception_is_propagated_and_sources_removed(self):
        glib = GLib()
        predicate = Mock(side_effect=[False, ValueError("field changed")])
        with self.assertRaisesRegex(ValueError, "field changed"):
            pump_until(predicate, 1.0, _glib=glib)
        self.assertEqual(glib.active, set())


class X11OwnershipTests(unittest.TestCase):
    def test_reads_only_the_clipboard_owner_and_closes_connection(self):
        x11 = SimpleNamespace(
            XOpenDisplay=Mock(return_value=1234),
            XInternAtom=Mock(return_value=50),
            XGetSelectionOwner=Mock(return_value=123456789),
            XCloseDisplay=Mock(),
        )
        with patch("voiced.clipboard.ctypes.CDLL", return_value=x11):
            self.assertEqual(_x11_selection_owner(":90"), 123456789)
        x11.XOpenDisplay.assert_called_once_with(b":90")
        x11.XInternAtom.assert_called_once_with(1234, b"CLIPBOARD", False)
        x11.XGetSelectionOwner.assert_called_once_with(1234, 50)
        x11.XCloseDisplay.assert_called_once_with(1234)

    def test_failed_connection_is_not_reported_as_empty(self):
        x11 = Mock()
        x11.XOpenDisplay.return_value = None
        with patch("voiced.clipboard.ctypes.CDLL", return_value=x11):
            with self.assertRaisesRegex(ClipboardError, "Cannot check"):
                _x11_selection_owner(":90")
        x11.XGetSelectionOwner.assert_not_called()

    def test_remote_display_is_rejected_before_loading_x11(self):
        with patch("voiced.clipboard.ctypes.CDLL") as library:
            with self.assertRaisesRegex(ClipboardError, "local X11 display"):
                _x11_selection_owner("example.com:0")
        library.assert_not_called()


if __name__ == "__main__":
    unittest.main()
