from types import SimpleNamespace as NS
from unittest.mock import patch
import unittest
from voiced import focus


class Node:
    def __init__(self,path,role,text,caret,parent=None):
        self.path,self.role,self.text,self.caret,self.parent=path,role,text,caret,parent
        self.children=[]
        self.editable=True
    def get_role(self):return self.role
    def get_state_set(self):return NS(contains=lambda state:self.editable and state=='editable')
    def get_text_iface(self):return self
    def get_editable_text_iface(self):return None
    def get_process_id(self):return 42
    def get_parent(self):return self.parent


class EmbeddedEditorTests(unittest.TestCase):
    def setUp(self):
        self.root=Node('/editor','entry','\ufffc',0)
        self.paragraph=Node('/paragraph','paragraph','prefix suffix',7,self.root)
        self.root.children=[self.paragraph]
        self.atspi=NS(
            Role=NS(ENTRY='entry',TEXT='text',PARAGRAPH='paragraph',PASSWORD_TEXT='password',TERMINAL='terminal'),
            StateType=NS(EDITABLE='editable'),
            Text=NS(get_text=lambda n,*args:n.text,get_caret_offset=lambda n:n.caret,
                    get_character_count=lambda n:len(n.text),get_n_selections=lambda n:0),
            Hypertext=NS(get_link_index=lambda n,offset:offset,get_link=lambda n,i:n.children[i]),
            Hyperlink=NS(get_object=lambda link,i:link),
        )

    def test_rich_editor_returns_paragraph_offsets_not_object_marker(self):
        with patch.object(focus,'_find_editor',return_value=self.root):
            node=focus._find_field(self.atspi,42)
        field=focus._read_field(node,self.atspi)
        self.assertIs(node,self.paragraph)
        self.assertEqual((field.text,field.caret),('prefix suffix',7))
        self.assertEqual(field.editor_identity,'42:/editor')

    def test_empty_single_paragraph_is_a_verified_placeholder(self):
        self.paragraph.text='\n';self.paragraph.caret=0
        self.assertTrue(focus._read_field(self.paragraph,self.atspi).empty_editor)

    def test_blank_paragraph_in_nonempty_editor_is_not_a_placeholder(self):
        self.root.text='\ufffc\ufffc'
        self.paragraph.text='\n';self.paragraph.caret=0
        self.assertFalse(focus._read_field(self.paragraph,self.atspi).empty_editor)

    def test_inaccessible_embedded_content_is_not_a_draft(self):
        self.paragraph.editable=False
        with patch.object(focus,'_find_editor',return_value=self.root):
            self.assertIsNone(focus._find_field(self.atspi,42))


if __name__=='__main__':unittest.main()
