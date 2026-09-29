"""Capture lifecycle and backpressure checks without opening a microphone."""
from __future__ import annotations

import threading
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

# Importing sounddevice initializes PortAudio. Keep these tests independent of
# the host audio server as well as the actual microphone.
with patch.dict(sys.modules, {"sounddevice": SimpleNamespace(RawInputStream=None, query_devices=None)}):
    from voiced import audio
from voiced.config import Settings


class FakeVAD:
    def __init__(self):
        self.consumed = 0
        self.changed = threading.Condition()
        self.error = None

    def is_speech(self, frame, sample_rate):
        if self.error:
            raise self.error
        with self.changed:
            self.consumed += 1
            self.changed.notify_all()
        return bool(np.frombuffer(frame, dtype=np.int16)[0])

    def wait_for(self, count):
        with self.changed:
            return self.changed.wait_for(lambda: self.consumed >= count, timeout=1)


class FakeStream:
    def __init__(self, start_frames=()):
        self.start_frames = start_frames
        self.callback = None
        self.blocksize = None
        self.closed = threading.Event()
        self.stopped = False
        self.start_error = None
        self.stop_error = None
        self.after_start = None

    def factory(self, **kwargs):
        self.callback = kwargs["callback"]
        self.blocksize = kwargs["blocksize"]
        return self

    def start(self):
        if self.start_error:
            raise self.start_error
        self.feed(self.start_frames)
        if self.after_start:
            self.after_start()

    def feed(self, values):
        for value in values:
            pcm = np.full(self.blocksize, value, dtype=np.int16)
            self.callback(pcm.tobytes(), self.blocksize, None, None)

    def stop(self):
        self.stopped = True
        if self.stop_error:
            raise self.stop_error

    def close(self):
        self.closed.set()


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(stream_interval_ms=300, input_device_name=None)
        self.cancel = threading.Event()
        self.finish = threading.Event()
        self.vad = FakeVAD()
        self.vad_patch = patch.object(audio.webrtcvad, "Vad", return_value=self.vad)
        self.vad_patch.start()
        self.addCleanup(self.vad_patch.stop)

    def capture(self, stream, settings=None):
        factory = patch.object(audio.sd, "RawInputStream", side_effect=stream.factory)
        mock_factory = factory.start()
        self.addCleanup(factory.stop)
        updates = audio.record_updates(settings or self.settings, self.cancel, self.finish)
        self.addCleanup(updates.close)
        return updates, mock_factory

    def test_no_microphone_until_iteration_and_precancel_never_opens(self):
        updates, factory = self.capture(FakeStream())
        factory.assert_not_called()
        self.cancel.set()
        self.assertEqual(list(updates), [])
        factory.assert_not_called()

    def test_brief_pause_continues_and_long_pause_closes_during_slow_decode(self):
        stream = FakeStream([1] * 10)
        updates, _ = self.capture(stream)
        first = next(updates)
        self.assertFalse(first.final)
        self.assertEqual(len(first.pcm), 10 * 480)

        # The consumer is occupied with a slow decode. Capture still hears a
        # three-second pause, more words, then a five-second final pause.
        stream.feed([0] * 100 + [2] * 10)
        self.assertTrue(self.vad.wait_for(120))
        self.assertFalse(stream.closed.is_set())
        stream.feed([0] * 167)
        self.assertTrue(stream.closed.wait(1), "mic must close without requesting next update")

        final = next(updates)
        self.assertTrue(final.final, "obsolete previews must be replaced by the final audio")
        self.assertEqual(len(final.pcm), 130 * 480)
        self.assertEqual(np.count_nonzero(final.pcm == 1), 10 * 480)
        self.assertEqual(np.count_nonzero(final.pcm == 2), 10 * 480)
        self.assertEqual(list(updates), [])

    def test_latest_preview_replaces_queued_revisions(self):
        stream = FakeStream([1] * 10)
        updates, _ = self.capture(stream)
        next(updates)
        stream.feed([2] * 10)
        self.assertTrue(self.vad.wait_for(20))
        stream.feed([3] * 10)
        self.assertTrue(self.vad.wait_for(30))
        # A finish request ensures the producer has processed all queued audio
        # before the consumer returns from a hypothetical long inference call.
        self.finish.set()
        self.assertTrue(stream.closed.wait(1))
        final = next(updates)
        self.assertTrue(final.final)
        self.assertEqual(len(final.pcm), 30 * 480)
        self.assertEqual(np.count_nonzero(final.pcm == 3), 10 * 480)

    def test_isolated_vad_blips_do_not_reset_the_finish_pause(self):
        stream = FakeStream([1] * 10)
        updates, _ = self.capture(stream)
        next(updates)
        pause = [0] * 167
        for index in (30, 70, 110, 150):
            pause[index] = 2
        stream.feed(pause)
        self.assertTrue(stream.closed.wait(1), "isolated noise must not prolong the pause")
        final = next(updates)
        self.assertTrue(final.final)
        self.assertEqual(self.vad.consumed, 177)
        self.assertEqual(np.count_nonzero(final.pcm == 2), 4 * 480)

    def test_resumed_short_word_at_deadline_gets_confirmation_time(self):
        stream = FakeStream([1] * 10)
        updates, _ = self.capture(stream)
        next(updates)
        # This 150 ms word straddles the original five-second deadline.
        stream.feed([0] * 164 + [2] * 5)
        self.assertTrue(self.vad.wait_for(179))
        self.assertFalse(stream.closed.is_set())
        stream.feed([0] * 167)
        self.assertTrue(stream.closed.wait(1))
        final = next(updates)
        self.assertTrue(final.final)
        self.assertEqual(self.vad.consumed, 346)
        self.assertEqual(np.count_nonzero(final.pcm == 2), 5 * 480)

    def test_blip_at_deadline_gets_only_one_bounded_confirmation_window(self):
        stream = FakeStream([1] * 10)
        updates, _ = self.capture(stream)
        next(updates)
        stream.feed([0] * 165 + [2] + [0] * 11)
        self.assertTrue(stream.closed.wait(1))
        final = next(updates)
        self.assertTrue(final.final)
        self.assertEqual(self.vad.consumed, 187)
        self.assertEqual(np.count_nonzero(final.pcm == 2), 480)

    def test_waiting_for_pause_does_not_redecode_unchanged_speech(self):
        stream = FakeStream([1] * 10)
        updates, _ = self.capture(stream)
        with patch.object(audio, "AudioUpdate", wraps=audio.AudioUpdate) as snapshots:
            next(updates)
            stream.feed([0] * 100)
            self.assertTrue(self.vad.wait_for(110))
            self.finish.set()
            self.assertTrue(next(updates).final)
            self.assertEqual(list(updates), [])
            self.assertEqual(snapshots.call_count, 2, "one draft and one final are sufficient")

    def test_single_vad_blip_cannot_end_session_before_real_speech(self):
        stream = FakeStream([1] + [0] * 170 + [2] * 10)
        updates, _ = self.capture(stream)
        draft = next(updates)
        self.assertFalse(draft.final)
        self.assertFalse(stream.closed.is_set())
        self.assertFalse(np.any(draft.pcm == 1), "old noise should age out of preroll")
        self.finish.set()
        final = next(updates)
        self.assertTrue(final.final)
        self.assertEqual(np.count_nonzero(final.pcm == 2), 10 * 480)

    def test_silence_and_isolated_noise_produce_no_transcript(self):
        for frames in ([0] * 10, [1] + [0] * 9):
            with self.subTest(frames=frames):
                stream = FakeStream(frames)
                updates, _ = self.capture(stream, replace(self.settings, max_session_seconds=0.3))
                self.assertEqual(list(updates), [])
                self.assertTrue(stream.closed.is_set())

    def test_maximum_audio_duration_closes_while_consumer_is_busy(self):
        stream = FakeStream([1] * 10)
        updates, _ = self.capture(stream, replace(self.settings, max_session_seconds=0.9))
        next(updates)
        stream.feed([1] * 20)
        self.assertTrue(stream.closed.wait(1))
        final = next(updates)
        self.assertTrue(final.final)
        self.assertEqual(final.seconds, 0.9)

    def test_cancel_closes_without_final_update(self):
        stream = FakeStream([1] * 10)
        updates, _ = self.capture(stream)
        next(updates)
        self.cancel.set()
        self.assertTrue(stream.closed.wait(1))
        self.assertEqual(list(updates), [])

    def test_closing_iterator_releases_microphone(self):
        stream = FakeStream([1] * 10)
        updates, _ = self.capture(stream)
        next(updates)
        updates.close()
        self.assertTrue(stream.closed.is_set())

    def test_explicit_finish_includes_frames_already_in_callback_queue(self):
        stream = FakeStream([1] * 10)
        stream.after_start = self.finish.set
        updates, _ = self.capture(stream)
        final = next(updates)
        self.assertTrue(final.final)
        self.assertEqual(len(final.pcm), 10 * 480)
        self.assertTrue(stream.closed.is_set())

    def test_overflow_fails_visibly_and_closes(self):
        stream = FakeStream([1] * (audio._RAW_QUEUE_FRAMES + 1))
        updates, _ = self.capture(stream)
        with self.assertRaisesRegex(audio.AudioCaptureError, "overflowed"):
            list(updates)
        self.assertTrue(stream.stopped)
        self.assertTrue(stream.closed.is_set())

    def test_backend_audio_status_fails_visibly_and_closes(self):
        stream = FakeStream()
        stream.after_start = lambda: stream.callback(b"", 480, None, "input overflow")
        updates, _ = self.capture(stream)
        with self.assertRaisesRegex(audio.AudioCaptureError, "input overflow"):
            list(updates)
        self.assertTrue(stream.closed.is_set())

    def test_invalid_frame_size_fails_visibly_and_closes(self):
        stream = FakeStream()
        stream.after_start = lambda: stream.callback(b"\0\0", 1, None, None)
        updates, _ = self.capture(stream)
        with self.assertRaisesRegex(audio.AudioCaptureError, "frame size"):
            list(updates)
        self.assertTrue(stream.closed.is_set())

    def test_vad_failure_is_not_misreported_as_silence(self):
        self.vad.error = RuntimeError("bad VAD frame")
        stream = FakeStream([1] * 10)
        updates, _ = self.capture(stream)
        with self.assertRaisesRegex(RuntimeError, "bad VAD frame"):
            list(updates)
        self.assertTrue(stream.closed.is_set())

    def test_start_failure_still_closes_stream(self):
        stream = FakeStream()
        stream.start_error = RuntimeError("microphone disconnected")
        updates, _ = self.capture(stream)
        with self.assertRaisesRegex(RuntimeError, "microphone disconnected"):
            list(updates)
        self.assertTrue(stream.closed.is_set())

    def test_stop_failure_still_closes_stream(self):
        stream = FakeStream([1] * 10)
        stream.after_start = self.finish.set
        stream.stop_error = RuntimeError("stop failed")
        updates, _ = self.capture(stream)
        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            list(updates)
        self.assertTrue(stream.closed.is_set())

    def test_stuck_stop_does_not_hang_iterator_cleanup(self):
        release_stop = threading.Event()
        stream = FakeStream([1] * 10)
        stream.stop = lambda: release_stop.wait(1)
        updates, _ = self.capture(stream)
        next(updates)
        try:
            with patch.object(audio, "_CAPTURE_STOP_TIMEOUT", 0.01):
                with self.assertRaisesRegex(audio.AudioShutdownError, "closure is unconfirmed"):
                    updates.close()
            self.assertFalse(stream.closed.is_set())
        finally:
            release_stop.set()
            self.assertTrue(stream.closed.wait(1))

    def test_stuck_natural_finish_reports_shutdown_failure(self):
        release_stop = threading.Event()
        stream = FakeStream([1] * 10)
        stream.stop = lambda: release_stop.wait(1)
        updates, _ = self.capture(stream)
        next(updates)
        self.finish.set()
        try:
            with patch.object(audio, "_CAPTURE_STOP_TIMEOUT", 0.01):
                with self.assertRaisesRegex(audio.AudioShutdownError, "closure is unconfirmed"):
                    list(updates)
            self.assertFalse(stream.closed.is_set())
        finally:
            release_stop.set()
            self.assertTrue(stream.closed.wait(1))

    def test_missing_audio_fails_visibly(self):
        stream = FakeStream()
        updates, _ = self.capture(stream)
        with patch.object(audio.time, "monotonic", side_effect=[0, 0, 1]):
            with self.assertRaisesRegex(audio.AudioCaptureError, "No microphone audio"):
                list(updates)
        self.assertTrue(stream.closed.is_set())

    def test_invalid_configuration_never_opens_microphone(self):
        updates, factory = self.capture(FakeStream(), replace(self.settings, frame_ms=25))
        with self.assertRaisesRegex(ValueError, "frame_ms"):
            list(updates)
        factory.assert_not_called()

    def test_record_once_preserves_final_only_api(self):
        pcm = np.array([1, 2, 3], dtype=np.int16)
        with patch.object(audio, "record_updates", return_value=iter([
            audio.AudioUpdate(pcm=pcm[:1], sample_rate=16000, final=False),
            audio.AudioUpdate(pcm=pcm, sample_rate=16000, final=True),
        ])) as updates:
            utterance = audio.record_once(self.settings, max_seconds=42)
        self.assertIsInstance(utterance, audio.Utterance)
        np.testing.assert_array_equal(utterance.pcm, pcm)
        self.assertEqual(updates.call_args.args[0].max_session_seconds, 42)


if __name__ == "__main__":
    unittest.main()
