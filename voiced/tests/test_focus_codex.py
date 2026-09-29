"""Focused Chromium editors, without depending on a running desktop."""
from dataclasses import asdict
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

from voiced.focus import (
    FocusError, TextField, Window, _accessibility_request, _find_field,
    _node_identity, _read_field, _same_application,
)


class Node:
    def __init__(self, path, *, pid=42, role="panel", states=(),
                 children=(), text=False, editable=False, collection=False):
        self.path = path
        self.pid = pid
        self.role = role
        self.states = set(states)
        self.children = list(children)
        self.text = text
        self.editable = editable
        self.collection = collection
        self.parent = None
        self.child_reads = 0
        for child in self.children:
            child.parent = self

    def get_process_id(self):
        return self.pid

    def get_state_set(self):
        return SimpleNamespace(contains=self.states.__contains__)

    def get_role(self):
        return self.role

    def get_child_count(self):
        self.child_reads += 1
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_text_iface(self):
        return self if self.text else None

    def get_editable_text_iface(self):
        return self if self.editable else None

    def get_collection_iface(self):
        return self if self.collection else None

    def get_parent(self):
        return self.parent


def editor(path="/composer", *, focused=True, **kwargs):
    states = {"editable"}
    if focused:
        states.add("focused")
    return Node(path, states=states, text=True, editable=True, **kwargs)


def fixture(*fields, collection=True, focused=None, active=True):
    frame = Node("/frame", role="frame", states={"active"} if active else set(),
                 children=fields)
    app = Node("/app", role="application", children=[frame], collection=collection)
    desktop = Node("/desktop", pid=0, children=[app])
    matches = Mock(return_value=list(focused if focused is not None else fields))
    atspi = SimpleNamespace(
        get_desktop=lambda _index: desktop,
        Role=SimpleNamespace(FRAME="frame", DIALOG="dialog", TERMINAL="terminal",
                             PASSWORD_TEXT="password"),
        StateType=SimpleNamespace(FOCUSED="focused", EDITABLE="editable",
                                  DEFUNCT="defunct", ACTIVE="active"),
        StateSet=SimpleNamespace(new=lambda states: states),
        CollectionMatchType=SimpleNamespace(ALL="all"),
        CollectionSortOrder=SimpleNamespace(CANONICAL="canonical"),
        MatchRule=SimpleNamespace(new=Mock()),
        Collection=SimpleNamespace(get_matches=matches),
    )
    return atspi, app, frame, matches


class FocusedComposerTests(unittest.TestCase):
    def test_focused_document_does_not_hide_editable_composer(self):
        composer = editor()
        document = Node("/document", states={"focused"}, text=True, children=[composer])
        atspi, _, _, _ = fixture(document, focused=[document, composer])
        self.assertIs(_find_field(atspi, 42), composer)

    def test_focused_text_child_resolves_to_editable_parent(self):
        text = Node("/text", states={"focused"}, text=True)
        composer = editor(focused=False, children=[text])
        atspi, _, _, _ = fixture(composer, focused=[text])
        self.assertIs(_find_field(atspi, 42), composer)

    def test_chromium_editor_can_be_read_without_editable_text_interface(self):
        composer = editor()
        composer.editable = False
        atspi, _, _, _ = fixture(composer)
        atspi.Text = SimpleNamespace(
            get_character_count=lambda node: 3,
            get_text=lambda node, start, end: "a🙂é",
            get_caret_offset=lambda node: 2,
            get_n_selections=lambda node: 0,
        )
        self.assertIs(_find_field(atspi, 42), composer)
        self.assertEqual(_read_field(composer, atspi), TextField("42:/composer", "a🙂é", 2, paste_editable=True))

    def test_readonly_document_never_grants_focus_to_arbitrary_editor(self):
        composer = editor(focused=False)
        document = Node("/document", states={"focused"}, text=True, children=[composer])
        atspi, _, _, _ = fixture(document, focused=[document])
        self.assertIsNone(_find_field(atspi, 42))

    def test_collection_does_not_walk_large_chat(self):
        composer = editor()
        document = Node("/document", children=[Node(f"/line/{i}") for i in range(2000)])
        atspi, app, frame, matches = fixture(document, composer, focused=[composer])
        self.assertIs(_find_field(atspi, 42), composer)
        self.assertEqual([app.child_reads, frame.child_reads, document.child_reads], [0, 0, 0])
        self.assertEqual(matches.call_args.args[-2:], (32, True))

    def test_stale_focus_in_inactive_window_is_rejected(self):
        composer = editor()
        atspi, _, _, _ = fixture(composer, active=False)
        self.assertIsNone(_find_field(atspi, 42))

    def test_field_losing_focus_after_query_is_rejected(self):
        composer = editor(focused=False)
        atspi, _, _, _ = fixture(composer)
        self.assertIsNone(_find_field(atspi, 42))

    def test_two_focused_editors_are_ambiguous(self):
        atspi, _, _, _ = fixture(editor("/one"), editor("/two"))
        self.assertIsNone(_find_field(atspi, 42))

    def test_password_is_returned_to_explicit_blocking_check(self):
        password = Node("/password", role="password", states={"focused"})
        atspi, _, _, _ = fixture(password)
        self.assertIs(_find_field(atspi, 42), password)
        with self.assertRaisesRegex(FocusError, "password"):
            _read_field(password, atspi)

    def test_terminal_is_not_an_editor(self):
        terminal = editor(role="terminal")
        atspi, _, _, _ = fixture(terminal)
        self.assertIsNone(_find_field(atspi, 42))

    def test_collection_unsupported_uses_bounded_traversal(self):
        composer = editor()
        document = Node("/document", states={"focused"}, text=True, children=[composer])
        atspi, _, _, _ = fixture(document, collection=False)
        self.assertIs(_find_field(atspi, 42), composer)

    def test_fallback_rejects_stale_focused_inactive_frame(self):
        composer = editor()
        atspi, _, frame, _ = fixture(composer, collection=False, active=False)
        frame.states.add("focused")
        frame.text = True
        self.assertIsNone(_find_field(atspi, 42))

    def test_fallback_rejects_incomplete_tree(self):
        composer = editor()
        document = Node("/document", children=[Node(f"/line/{i}") for i in range(2000)])
        atspi, _, _, _ = fixture(composer, document, collection=False)
        self.assertIsNone(_find_field(atspi, 42))

    def test_unrelated_process_is_not_searched(self):
        atspi, app, _, matches = fixture(editor())
        with patch("voiced.focus._same_application", return_value=False):
            self.assertIsNone(_find_field(atspi, 99))
        self.assertEqual(app.child_reads, 0)
        matches.assert_not_called()

    def test_bus_owner_distinguishes_identical_object_paths(self):
        first, second = editor(), editor()
        first.app = SimpleNamespace(bus_name=":1.4")
        second.app = SimpleNamespace(bus_name=":1.5")
        self.assertNotEqual(_node_identity(first), _node_identity(second))


class ProcessOwnershipTests(unittest.TestCase):
    def test_exact_process_requires_no_proc_lookup(self):
        with patch("voiced.focus.os.readlink") as read:
            self.assertTrue(_same_application(42, 42))
        read.assert_not_called()

    def test_renderer_must_share_binary_and_descend_from_window_owner(self):
        with patch("voiced.focus.os.readlink", return_value="/opt/ChatGPT"), \
                patch("voiced.focus.Path.read_text", side_effect=["PPid:\t30\n", "PPid:\t42\n"]):
            self.assertTrue(_same_application(31, 42))

    def test_independent_instances_of_same_binary_do_not_match(self):
        with patch("voiced.focus.os.readlink", return_value="/opt/ChatGPT"), \
                patch("voiced.focus.Path.read_text", return_value="PPid:\t1\n"):
            self.assertFalse(_same_application(31, 42))

    def test_other_program_child_does_not_match(self):
        with patch("voiced.focus.os.readlink", side_effect=["/bin/editor", "/opt/ChatGPT"]), \
                patch("voiced.focus.Path.read_text") as read:
            self.assertFalse(_same_application(31, 42))
        read.assert_not_called()


class MutationFocusTests(unittest.TestCase):
    def test_field_change_within_window_prevents_edit(self):
        before = TextField("42:/old", "draft", 5, can_delete=True)
        after = TextField("42:/new", "draft", 5, can_delete=True)
        insertion = Mock()
        atspi = SimpleNamespace(
            init=lambda: None, set_timeout=lambda *args: None,
            EditableText=SimpleNamespace(insert_text=insertion),
        )
        modules = {
            "gi": SimpleNamespace(require_version=lambda *args: None),
            "gi.repository": SimpleNamespace(Atspi=atspi),
        }
        window = Window("test", "123", 42)
        with patch.dict(sys.modules, modules), \
                patch("voiced.focus.focused_window", return_value=window), \
                patch("voiced.focus._find_field", side_effect=[object(), object()]), \
                patch("voiced.focus._read_field", side_effect=[before, after]):
            with self.assertRaisesRegex(FocusError, "field or caret changed"):
                _accessibility_request({
                    "window": asdict(window), "action": "insert",
                    "expected": asdict(before), "text": " hello",
                })
        insertion.assert_not_called()


if __name__ == "__main__":
    unittest.main()
