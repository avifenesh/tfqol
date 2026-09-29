import sys
import tempfile
import subprocess
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

with patch.dict(sys.modules, {"sounddevice": SimpleNamespace(RawInputStream=None, query_devices=None)}):
    from voiced import daemon
from voiced.config import Settings


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)
        self.notify=patch.object(daemon,'_notify').start()
        self.addCleanup(patch.stopall)
        patch.object(daemon,'STATEFILE',self.path/'state.json').start()
        patch.object(daemon,'RECOVERY',self.path/'latest.txt').start()
        patch.object(daemon,'STT',return_value=Mock()).start()
        self.d=daemon.Daemon(Settings())

    def test_stuck_microphone_requests_exit_and_rejects_another_recording(self):
        patch.object(daemon.router,'DraftWriter',return_value=Mock()).start()
        def fail(*args):
            raise daemon.audio.AudioShutdownError('closure is unconfirmed')
            yield
        patch.object(daemon.audio,'record_updates',side_effect=fail).start()
        self.d._busy.acquire()
        with self.assertLogs('voiced',level='ERROR'):
            self.d._session(0)
        self.assertTrue(self.d._stop.is_set())
        self.assertTrue(self.d.audio_shutdown_failed)
        self.assertTrue(self.d._cancel.is_set())
        self.assertFalse(self.d._busy.locked())
        with patch.object(daemon.threading,'Thread') as thread:
            self.d._on_arm()
            thread.assert_not_called()

    def test_physical_interaction_cancels_active_capture(self):
        self.d._busy.acquire()
        self.d._on_interaction()
        self.assertTrue(self.d._cancel.is_set())
        self.assertEqual(self.d._interaction_epoch,1)
        self.d._busy.release()

    def test_start_refusal_explains_reason_and_clears_previous_text(self):
        previous=self.path/'latest.txt'
        previous.write_text('previous dictation')
        patch.object(daemon.router,'DraftWriter',side_effect=daemon.router.InterruptedDraft('Clear the text selection before dictating')).start()
        capture=patch.object(daemon.audio,'record_updates').start()
        self.d._busy.acquire()
        self.d._session(0)
        self.assertFalse(previous.exists())
        self.assertFalse(self.d._busy.locked())
        self.assertEqual(self.d._state,'interrupted')
        self.notify.assert_called_once_with('voiced cannot start','Clear the text selection before dictating')
        capture.assert_not_called()

    def test_latest_recovery_is_private_and_replaced(self):
        self.d._remember('old')
        self.d._remember('corrected')
        p=self.path/'latest.txt'
        self.assertEqual(p.read_text(),'corrected')
        self.assertEqual(p.stat().st_mode & 0o777,0o600)

    def test_fatal_exit_bypasses_backend_atexit_handler(self):
        # A subprocess is necessary: a mocked os._exit would not establish
        # whether the native library's registered cleanup runs.
        marker=self.path/'atexit-ran'
        program=textwrap.dedent('''
            import atexit, sys
            from pathlib import Path
            from types import SimpleNamespace
            from unittest.mock import patch
            with patch.dict(sys.modules, {'sounddevice': SimpleNamespace()}):
                from voiced import daemon
            atexit.register(lambda: Path(sys.argv[1]).write_text('cleanup ran'))
            class FailedDaemon:
                audio_shutdown_failed=True
                def __init__(self, settings): pass
                def run(self): return 0
            with patch.object(daemon,'Daemon',FailedDaemon), patch.object(daemon,'setup_logging'), patch.object(daemon,'RUNTIME_DIR',Path(sys.argv[2])):
                daemon.main([])
        ''')
        result=subprocess.run([sys.executable,'-c',program,str(marker),str(self.path)],
                              capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,1,result.stderr)
        self.assertFalse(marker.exists())


if __name__=='__main__':unittest.main()
