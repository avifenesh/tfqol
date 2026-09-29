from dataclasses import asdict,replace
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from voiced import clipboard,focus


class BrowserPasteTests(unittest.TestCase):
    def setUp(self):
        self.window=focus.Window('gnome','100',42,client_type='x11')
        self.field=focus.TextField('42:/field','before bad after',10,paste_editable=True)
        self.current=self.field
        self.bounds=None
        self.events=[]
        self.backends=[]
        self.wrong_selection=False
        self.change_on_snapshot=False
        self.wrong_paste=False
        self.node=object()
        case=self
        class Clip:
            def __init__(self,backend):self.text='';case.backends.append(backend)
            def snapshot(self):
                case.events.append('snapshot')
                if case.change_on_snapshot:case.current=replace(case.current,caret=0)
            def set_text(self,text):self.text=text
            def paste(self):
                case.events.append('paste')
                start,end=case.bounds or (case.current.caret,case.current.caret)
                text=self.text if not case.wrong_paste else 'wrong'
                case.current=replace(case.current,text=case.current.text[:start]+text+case.current.text[end:],caret=start+len(text),selection=False)
                case.bounds=None
            def restore(self):case.events.append('restore');return True
            def keep_pending(self):case.events.append('keep pending');return True
        def select(node,start,end):
            self.bounds=(start-1 if self.wrong_selection else start,end)
            self.current=replace(self.current,selection=True)
            return True
        def clear(node,caret):
            self.current=replace(self.current,caret=caret,selection=False)
            self.bounds=None
            return True
        self.atspi=SimpleNamespace(Text=SimpleNamespace(
            add_selection=select,
            get_n_selections=lambda node:int(self.bounds is not None),
            get_selection=lambda node,index:SimpleNamespace(start_offset=self.bounds[0],end_offset=self.bounds[1]),
            set_caret_offset=clear,
        ))
        for patcher in [patch.object(clipboard,'Clipboard',Clip),
                        patch.object(clipboard,'pump_until',side_effect=lambda predicate,timeout:predicate()),
                        patch.object(focus,'focused_window',return_value=self.window),
                        patch.object(focus,'_find_field',return_value=self.node),
                        patch.object(focus,'_node_identity',return_value=self.field.identity),
                        patch.object(focus,'_read_field',side_effect=lambda *args:self.current),
                        patch.object(focus,'_clipboard_keeper',None)]:
            patcher.start();self.addCleanup(patcher.stop)

    def request(self):
        return focus._verified_paste(self.atspi,self.window,self.node,self.field,self.field,
                                     {'start':7,'text':'good'})

    def test_replaces_only_verified_selection_and_restores_clipboard(self):
        result=self.request()
        self.assertEqual(result['field']['text'],'before good after')
        self.assertEqual(self.events,['snapshot','paste','restore'])
        self.assertEqual(self.backends,['x11'])
        self.assertFalse(self.current.selection)

    def test_changed_field_after_snapshot_never_gets_paste(self):
        self.change_on_snapshot=True
        with self.assertRaises(focus.FocusError):self.request()
        self.assertNotIn('paste',self.events)
        self.assertIn('restore',self.events)

    def test_wrong_selection_never_gets_paste(self):
        self.wrong_selection=True
        with self.assertRaises(focus.FocusError):self.request()
        self.assertNotIn('paste',self.events)
        self.assertEqual(self.current.text,self.field.text)

    def test_readback_mismatch_stops_without_retry(self):
        self.wrong_paste=True
        with self.assertRaisesRegex(focus.FocusError,'could not be verified'):self.request()
        self.assertEqual(self.events.count('paste'),1)
        self.assertNotIn('restore',self.events)
        self.assertIn('keep pending',self.events)


if __name__=='__main__':unittest.main()
