from types import SimpleNamespace
import unittest

from voiced.pipeline import dictate
from voiced.router import RouterError


class PipelineTests(unittest.TestCase):
    def run_pipeline(self, outputs, block=False):
        events=[]
        class STT:
            def transcribe(self, pcm, *, final):
                events.append(('decode',final))
                return outputs[pcm]
        class Draft:
            def update(self,text):
                events.append(('type',text))
                if block:raise RouterError('focus changed')
            def finish(self):events.append(('finish',))
        updates=[SimpleNamespace(pcm=i,final=i==len(outputs)-1) for i in range(len(outputs))]
        result=dictate(updates,STT(),Draft(),on_text=lambda t:events.append(('save',t)))
        return result,events

    def test_partial_is_typed_before_final_decode_and_saved_first(self):
        result,events=self.run_pipeline(['the rigid image','The original image.'])
        self.assertEqual(events,[('decode',False),('save','the rigid image'),('type','the rigid image'),
                                 ('decode',True),('save','The original image.'),('type','The original image.'),('finish',)])
        self.assertEqual(result.previews,1)
        self.assertTrue(result.corrected)

    def test_empty_final_never_erases_a_draft(self):
        result,events=self.run_pipeline(['Some words',''])
        self.assertEqual(result.text,'Some words')
        self.assertFalse(result.corrected)
        self.assertNotIn(('type',''),events)

    def test_interrupted_draft_preserves_latest_correction(self):
        result,events=self.run_pipeline(['some word','Some words.'],block=True)
        self.assertEqual(result.text,'Some words.')
        self.assertEqual(result.blocked,'focus changed')
        self.assertIn(('save','Some words.'),events)
        self.assertNotIn(('finish',),events)

    def test_unreadable_field_streams_safe_prefix_without_rewriting(self):
        class Draft:
            corrections_available=False
            typed_text=''
            writes=[]
            def update(self,text):
                self.writes.append(text);self.typed_text=text
            def finish(self):pass
        outputs=['We need the rigid image','We need the original image now','We need the original image now.']
        class STT:
            def transcribe(self,pcm,*,final):return outputs[pcm]
        draft=Draft()
        result=dictate([SimpleNamespace(pcm=i,final=i==2) for i in range(3)],STT(),draft)
        self.assertEqual(draft.writes,['We need the ','We need the original ','We need the original image now.'])
        self.assertEqual(result.blocked,'')

    def test_unreadable_committed_revision_preserves_field_and_final_copy(self):
        class Draft:
            corrections_available=False
            typed_text='We need the '
            def update(self,text):raise AssertionError('unsafe rewrite')
            def finish(self):raise AssertionError('cannot finalize rejected correction')
        class STT:
            def transcribe(self,pcm,*,final):return 'We want the original image.'
        result=dictate([SimpleNamespace(pcm=0,final=True)],STT(),Draft())
        self.assertTrue(result.blocked)
        self.assertEqual(result.text,'We want the original image.')


if __name__=='__main__':unittest.main()
