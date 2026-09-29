from dataclasses import replace
import os
import subprocess
import sys
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voiced.focus import DesktopText, FocusError, TextField, Window, focused_window
from voiced.router import DraftWriter, InterruptedDraft


class FakeDesktop:
    def __init__(self, text="prefix SUFFIX", caret=7):
        self.focus = Window("test", "100", 42)
        self.field = TextField("42:/field", text, caret, can_delete=True)
        self.interrupted = False
        self.insertions = []
        self.deletions = []
        self.before_delete = None

    def snapshot(self, window):
        return self.field

    def delete_suffix(self, window, expected, start):
        if self.before_delete:
            self.before_delete()
        if self.field != expected:
            raise FocusError("field changed")
        self.deletions.append((start, expected.caret))
        self.field = replace(
            self.field,
            text=self.field.text[:start] + self.field.text[self.field.caret:],
            caret=start,
        )
        return self.field

    def type(self, text):
        self.insertions.append(text)
        if self.field is not None:
            self.field = replace(
                self.field,
                text=self.field.text[:self.field.caret] + text + self.field.text[self.field.caret:],
                caret=self.field.caret + len(text),
            )

    def insert_text(self, window, expected, text):
        if self.field != expected:
            raise FocusError("field changed")
        self.type(text)
        return self.field

    def replace_suffix(self, window, expected, start, text):
        if self.field != expected:
            raise FocusError('field changed')
        self.deletions.append((start, expected.caret))
        self.insertions.append(text)
        self.field=replace(self.field, text=expected.text[:start]+text+expected.text[expected.caret:],
                           caret=start+len(text))
        return self.field

    def writer(self):
        return DraftWriter(
            lambda: self.interrupted, get_focus=lambda: self.focus,
            desktop=self,
        )


class DraftWriterTests(unittest.TestCase):
    def test_browser_revision_uses_verified_range_and_preserves_surrounding_text(self):
        desktop=FakeDesktop()
        desktop.field=replace(desktop.field,can_delete=False,paste_editable=True)
        draft=desktop.writer()
        draft.update('the rigid image')
        draft.update('the original image.')
        draft.finish()
        self.assertEqual(desktop.field.text,'prefix the original image.SUFFIX')
        self.assertEqual(desktop.deletions[-1],(11,22))

    def test_browser_shortening_never_sends_empty_paste_or_delete_keys(self):
        desktop=FakeDesktop()
        desktop.field=replace(desktop.field,can_delete=False,paste_editable=True)
        draft=desktop.writer()
        draft.update('hello world')
        draft.update('hello')
        self.assertEqual(desktop.field.text,'prefix helloSUFFIX')
        self.assertEqual(desktop.insertions[-1],'o')
        self.assertEqual(desktop.deletions[-1],(11,18))

    def test_browser_escape_sequences_are_literal_clipboard_data(self):
        desktop=FakeDesktop()
        desktop.field=replace(desktop.field,can_delete=False,paste_editable=True)
        draft=desktop.writer()
        text=r'Use C:\temp\new and write \t literally.'
        draft.update(text)
        self.assertEqual(desktop.insertions,[text])
        self.assertEqual(desktop.field.text,'prefix '+text+'SUFFIX')

    def test_browser_field_change_stops_before_paste(self):
        desktop=FakeDesktop()
        desktop.field=replace(desktop.field,can_delete=False,paste_editable=True)
        draft=desktop.writer()
        desktop.field=replace(desktop.field,caret=0)
        with self.assertRaises(InterruptedDraft):draft.update('never pasted')
        self.assertEqual(desktop.insertions,[])

    def test_editable_field_insertion_uses_no_virtual_keyboard(self):
        desktop = FakeDesktop()
        draft = DraftWriter(
            lambda: False, get_focus=lambda: desktop.focus,
            desktop=desktop,
        )
        with patch("voiced.router.subprocess.run", side_effect=AssertionError("Unexpected keyboard command")):
            draft.update("hello")
        self.assertEqual(desktop.field.text, "prefix helloSUFFIX")

    def test_failed_accessible_insert_never_falls_back_to_typing(self):
        desktop = FakeDesktop()
        draft = DraftWriter(
            lambda: False, get_focus=lambda: desktop.focus,
            desktop=desktop,
        )
        with patch.object(desktop, "insert_text", side_effect=FocusError("field changed")), patch("voiced.router.subprocess.run", side_effect=AssertionError("Unexpected keyboard command")):
            with self.assertRaises(InterruptedDraft):
                draft.update("hello")
        self.assertEqual(draft.recovery_text, "hello")

    def test_append_and_finish_preserve_initial_prefix_suffix(self):
        desktop = FakeDesktop()
        draft = desktop.writer()
        draft.update("hello")
        draft.update("hello world")
        self.assertEqual(draft.finish(), "hello world")
        self.assertEqual(desktop.field.text, "prefix hello worldSUFFIX")
        self.assertEqual(desktop.field.caret, len("prefix hello world"))
        self.assertEqual(desktop.deletions, [])
        self.assertEqual(desktop.insertions, ["hello", " world"])

    def test_revision_deletes_only_owned_suffix(self):
        desktop = FakeDesktop()
        draft = desktop.writer()
        draft.update("hello word")
        draft.update("hello world.")
        self.assertEqual(desktop.deletions, [(16, 17)])
        self.assertEqual(desktop.field.text, "prefix hello world.SUFFIX")
        self.assertEqual(draft.typed_text, "hello world.")

    def test_whole_draft_correction_never_deletes_original_prefix(self):
        desktop = FakeDesktop()
        draft = desktop.writer()
        draft.update("hello world")
        draft.update("Hello, world!")
        self.assertEqual(desktop.deletions, [(7, 18)])
        self.assertEqual(desktop.field.text, "prefix Hello, world!SUFFIX")

    def test_focus_switch_stops_append_and_correction(self):
        for new_text in ("hello there", "Hello"):
            desktop = FakeDesktop()
            draft = desktop.writer()
            draft.update("hello")
            desktop.focus = Window("test", "101", 42)
            with self.assertRaises(InterruptedDraft):
                draft.update(new_text)
            self.assertEqual(desktop.insertions, ["hello"])
            self.assertEqual(desktop.deletions, [])
            self.assertEqual(draft.recovery_text, new_text)

    def test_physical_input_stops_edits_even_if_field_still_matches(self):
        desktop = FakeDesktop()
        draft = desktop.writer()
        draft.update("hello")
        desktop.interrupted = True
        with self.assertRaises(InterruptedDraft):
            draft.update("Hello!")
        self.assertEqual(desktop.deletions, [])

    def test_manual_text_change_stops_edits_without_input_signal(self):
        desktop = FakeDesktop()
        draft = desktop.writer()
        draft.update("hello")
        desktop.field = replace(desktop.field, text="prefix user's textSUFFIX")
        with self.assertRaises(InterruptedDraft):
            draft.update("Hello!")
        self.assertEqual(desktop.deletions, [])

    def test_autocorrection_is_not_mistaken_for_owned_text(self):
        desktop = FakeDesktop()
        draft = desktop.writer()
        draft.update("helo")
        desktop.field = replace(desktop.field, text="prefix helloSUFFIX", caret=12)
        with self.assertRaises(InterruptedDraft):
            draft.update("Hello.")
        self.assertEqual(desktop.field.text, "prefix helloSUFFIX")
        self.assertEqual(desktop.deletions, [])

    def test_caret_selection_or_field_identity_change_stops_edits(self):
        for changes in ({"caret": 7}, {"selection": True}, {"identity": "42:/other"}):
            desktop = FakeDesktop()
            draft = desktop.writer()
            draft.update("hello")
            desktop.field = replace(desktop.field, **changes)
            with self.assertRaises(InterruptedDraft):
                draft.update("hello there")
            self.assertEqual(desktop.insertions, ["hello"])

    def test_existing_selection_is_never_overwritten(self):
        desktop = FakeDesktop()
        desktop.field = replace(desktop.field, selection=True)
        with self.assertRaises(InterruptedDraft):
            desktop.writer()
        self.assertEqual(desktop.insertions, [])

    def test_unreadable_field_is_refused_before_any_input(self):
        desktop = FakeDesktop()
        desktop.field = None
        with self.assertRaisesRegex(InterruptedDraft, "editable text field"):
            desktop.writer()
        self.assertEqual(desktop.insertions, [])
        self.assertEqual(desktop.deletions, [])

    def test_readable_field_cannot_downgrade_to_unguarded_typing(self):
        desktop = FakeDesktop()
        draft = desktop.writer()
        draft.update("hello")
        desktop.field = None
        with self.assertRaises(InterruptedDraft):
            draft.update("hello world")
        self.assertEqual(desktop.insertions, ["hello"])

    def test_noneditable_field_is_refused_before_any_input(self):
        desktop = FakeDesktop()
        desktop.field = replace(desktop.field, can_delete=False)
        with self.assertRaises(InterruptedDraft):
            desktop.writer()
        self.assertEqual(desktop.insertions, [])
        self.assertEqual(desktop.deletions, [])

    def test_halted_writer_retains_latest_final_text_without_more_input(self):
        desktop = FakeDesktop()
        draft = desktop.writer()
        draft.update("hello")
        desktop.interrupted = True
        with self.assertRaises(InterruptedDraft):
            draft.update("hello world")
        desktop.interrupted = False
        with self.assertRaises(InterruptedDraft) as error:
            draft.update("Hello, world.")
        self.assertEqual(error.exception.recovery_text, "Hello, world.")
        self.assertEqual(draft.recovery_text, "Hello, world.")
        self.assertEqual(desktop.insertions, ["hello"])

    def test_delete_readback_mismatch_never_triggers_fallback(self):
        desktop = FakeDesktop()
        draft = desktop.writer()
        draft.update("hello")
        desktop.before_delete = lambda: setattr(
            desktop, "field", replace(desktop.field, text="changed"),
        )
        with self.assertRaises(InterruptedDraft):
            draft.update("Hello!")
        self.assertEqual(desktop.deletions, [])
        self.assertEqual(desktop.insertions, ["hello"])

    def test_newlines_and_tabs_cannot_submit_commands(self):
        desktop = FakeDesktop()
        draft = desktop.writer()
        draft.update("line one\nline two\tthree\rfour")
        draft.finish()
        self.assertEqual(draft.typed_text, "line one line two three four")
        self.assertNotIn("\n", "".join(desktop.insertions))

    def test_unicode_range_deletes_codepoints_without_backspace(self):
        desktop = FakeDesktop()
        draft = desktop.writer()
        draft.update("cafe\u0301!")
        draft.update("coffee.")
        self.assertEqual(desktop.field.text, "prefix coffee.SUFFIX")
        self.assertEqual(desktop.deletions[0], (8, 13))

    def test_long_insert_checks_interruption_between_bursts(self):
        desktop = FakeDesktop()
        original = desktop.type
        def type_then_interrupt(text):
            original(text)
            desktop.interrupted = True
        desktop.type = type_then_interrupt
        draft = DraftWriter(
            lambda: desktop.interrupted, get_focus=lambda: desktop.focus,
            desktop=desktop,
        )
        with self.assertRaises(InterruptedDraft):
            draft.update("a" * 600)
        self.assertEqual(desktop.insertions, ["a" * 256])
        self.assertEqual(draft.recovery_text, "a" * 600)

    def test_unknown_focus_prevents_draft(self):
        def unknown():
            raise FocusError("unknown focus")
        with self.assertRaises(InterruptedDraft):
            DraftWriter(lambda: False, get_focus=unknown, desktop=FakeDesktop())

    def test_finish_detects_application_rewriting_last_insertion(self):
        desktop = FakeDesktop()
        draft = desktop.writer()
        draft.update("hello")
        desktop.field = replace(desktop.field, text="prefix HELLOSUFFIX")
        with self.assertRaises(InterruptedDraft):
            draft.finish()


class FocusTests(unittest.TestCase):
    def test_atspi_text_dispatch_avoids_accessible_method_collision(self):
        from voiced.focus import _read_field

        class Accessible:
            path = "/field"

            def get_state_set(self):
                return SimpleNamespace(contains=lambda state: state == "editable")

            def get_role(self):
                return "entry"

            def get_text_iface(self):
                return self

            def get_text(self):
                raise AssertionError("Accessible.get_text is not Text.get_text")

            def get_editable_text_iface(self):
                return self

            def get_process_id(self):
                return 42

        node = Accessible()
        calls = []

        class TextInterface:
            @staticmethod
            def get_character_count(target):
                calls.append(target)
                return 5

            @staticmethod
            def get_text(target, start, end):
                calls.append(target)
                self.assertEqual((start, end), (0, 5))
                return "hello"

            @staticmethod
            def get_caret_offset(target):
                calls.append(target)
                return 5

            @staticmethod
            def get_n_selections(target):
                calls.append(target)
                return 1

            @staticmethod
            def get_selection(target, index):
                calls.append(target)
                return SimpleNamespace(start_offset=2, end_offset=4)

        atspi = SimpleNamespace(
            Text=TextInterface, Role=SimpleNamespace(PASSWORD_TEXT="password", TERMINAL="terminal"),
            StateType=SimpleNamespace(EDITABLE="editable"),
        )
        self.assertEqual(
            _read_field(node, atspi),
            TextField("42:/field", "hello", 5, selection=True, can_delete=True),
        )
        self.assertEqual(calls, [node] * 5)

    def test_atspi_delete_uses_explicit_editable_text_interface(self):
        from dataclasses import asdict
        from unittest.mock import Mock
        from voiced.focus import _accessibility_request

        node = SimpleNamespace()
        before = TextField("42:/field", "old hello tail", 9, can_delete=True)
        after = replace(before, text="old  tail", caret=4)
        deletion = Mock(return_value=True)
        atspi = SimpleNamespace(
            init=lambda: None, set_timeout=lambda *args: None,
            EditableText=SimpleNamespace(delete_text=deletion),
        )
        modules = {
            "gi": SimpleNamespace(require_version=lambda *args: None),
            "gi.repository": SimpleNamespace(Atspi=atspi),
        }
        window = Window("test", "123", 42)
        with patch.dict(sys.modules, modules), \
                patch("voiced.focus.focused_window", return_value=window), \
                patch("voiced.focus._find_field", return_value=node), \
                patch("voiced.focus._read_field", side_effect=[before, before, after]):
            result = _accessibility_request({
                "window": asdict(window), "action": "delete",
                "expected": asdict(before), "start": 4,
            })
        deletion.assert_called_once_with(node, 4, 9)
        self.assertEqual(result, {"status": "ok", "field": asdict(after)})

    def test_atspi_insert_unicode_and_bounded_caret_movement(self):
        from dataclasses import asdict
        from unittest.mock import Mock
        from voiced.focus import _accessibility_request

        for inserted_caret in (3, 4, 5):
            with self.subTest(inserted_caret=inserted_caret):
                node = SimpleNamespace()
                before = TextField("42:/field", "pre tail", 3, can_delete=True)
                insertion = Mock(return_value=True)
                move_caret = Mock(return_value=True)
                inserted = replace(before, text="pre🙂é tail", caret=inserted_caret)
                final = replace(inserted, caret=5)
                reads = [before, before, inserted]
                if inserted_caret == 3:
                    reads += [inserted, final]
                atspi = SimpleNamespace(
                    init=lambda: None, set_timeout=lambda *args: None,
                    EditableText=SimpleNamespace(insert_text=insertion),
                    Text=SimpleNamespace(set_caret_offset=move_caret),
                )
                modules = {
                    "gi": SimpleNamespace(require_version=lambda *args: None),
                    "gi.repository": SimpleNamespace(Atspi=atspi),
                }
                window = Window("test", "123", 42)
                with patch.dict(sys.modules, modules), \
                        patch("voiced.focus.focused_window", return_value=window), \
                        patch("voiced.focus._find_field", return_value=node), \
                        patch("voiced.focus._read_field", side_effect=reads):
                    request = {
                        "window": asdict(window), "action": "insert",
                        "expected": asdict(before), "text": "🙂é",
                    }
                    if inserted_caret == 4:
                        with self.assertRaises(FocusError):
                            _accessibility_request(request)
                    else:
                        result = _accessibility_request(request)
                        self.assertEqual(result, {"status": "ok", "field": asdict(final)})
                insertion.assert_called_once_with(node, 3, "🙂é", 6)
                if inserted_caret == 3:
                    move_caret.assert_called_once_with(node, 5)
                else:
                    move_caret.assert_not_called()

    def test_terminal_scrollback_is_not_a_draft_field(self):
        from voiced.focus import _read_field
        node = SimpleNamespace(
            get_state_set=lambda: None, get_role=lambda: "terminal",
        )
        atspi = SimpleNamespace(
            Role=SimpleNamespace(PASSWORD_TEXT="password", TERMINAL="terminal"),
        )
        with self.assertRaises(ValueError):
            _read_field(node, atspi)

    @patch("voiced.focus._command")
    @patch.dict(os.environ, {"VOICED_DESKTOP_BACKEND": "x11"})
    def test_x11_override_uses_supplied_x_server(self, command):
        command.side_effect = ["123", "42"]
        self.assertEqual(focused_window(), Window("x11", "123", 42))
        self.assertEqual(command.call_args_list[0].args[0], ["xdotool", "getactivewindow"])

    @patch("voiced.focus._command")
    @patch.dict(os.environ, {"VOICED_DESKTOP_BACKEND": "gnome"})
    def test_gnome_extension_identity(self, command):
        import json
        windows = [{"window_id": 8, "pid": 42, "focused": True}]
        command.return_value = repr((json.dumps(windows),))
        self.assertEqual(focused_window(), Window("gnome", "8", 42))

    def test_inaccessible_bus_is_readonly_fallback_not_deletion_fallback(self):
        backend = DesktopText()
        with patch.object(backend, "_request", side_effect=FocusError("bus gone")):
            self.assertIsNone(backend.snapshot(Window("test", "1", 42)))
            with self.assertRaises(FocusError):
                backend.delete_suffix(Window("test", "1", 42), TextField("f", "a", 1), 0)



if __name__ == "__main__":
    unittest.main()
