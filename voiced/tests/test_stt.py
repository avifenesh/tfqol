"""Whisper wrapper checks with a fake model, no downloads or inference."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from voiced.config import Settings
from voiced.stt import STT


def segment(text, probability=0.1, logprob=-0.1):
    return SimpleNamespace(text=text, no_speech_prob=probability, avg_logprob=logprob)


class STTTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(final_beam=5, whisper_threads=2)
        self.stt = STT(self.settings)
        self.model = Mock()
        self.stt._model = self.model
        self.pcm = np.array([0, 4096, -8192, 32767, -32768], dtype=np.int16)

    def output(self, *segments):
        self.model.transcribe.return_value = (iter(segments), None)

    def test_draft_and_final_use_complete_audio_with_distinct_beams(self):
        self.model.transcribe.side_effect = [
            (iter([segment("open the file")]), None),
            (iter([segment("Open the file.")]), None),
        ]
        self.assertEqual(self.stt.transcribe(self.pcm), "open the file")
        self.assertEqual(self.stt.transcribe(self.pcm, final=True), "Open the file.")
        calls = self.model.transcribe.call_args_list
        self.assertEqual(calls[0].kwargs["beam_size"], 1)
        self.assertEqual(calls[1].kwargs["beam_size"], 5)
        for call in calls:
            np.testing.assert_array_equal(call.args[0], self.pcm.astype(np.float32) / 32768)
            self.assertEqual(call.args[0].dtype, np.float32)
            self.assertFalse(call.kwargs["condition_on_previous_text"])

    def test_short_valid_words_are_kept(self):
        for text in ("Okay.", "Thanks.", "You", "Yeah.", "Bye."):
            with self.subTest(text=text):
                self.output(segment(text))
                self.assertEqual(self.stt.transcribe(self.pcm), text)

    def test_confident_short_word_survives_elevated_no_speech_score(self):
        self.output(segment("Okay.", probability=0.8, logprob=-0.1))
        self.assertEqual(self.stt.transcribe(self.pcm), "Okay.")

    def test_uncertain_noise_segment_is_removed_without_discarding_speech(self):
        self.output(segment("garbage", probability=0.9, logprob=-2), segment("Open file."))
        self.assertEqual(self.stt.transcribe(self.pcm), "Open file.")

    def test_no_probability_metadata_preserves_words(self):
        self.output(segment("hello", probability=None, logprob=None))
        self.assertEqual(self.stt.transcribe(self.pcm), "hello")

    def test_empty_silence_never_loads_or_invokes_model(self):
        self.stt._model = None
        with patch.object(self.stt, "load") as load:
            self.assertEqual(self.stt.transcribe(np.array([], dtype=np.int16)), "")
            self.assertEqual(self.stt.transcribe(np.zeros(16000, dtype=np.int16)), "")
        load.assert_not_called()
        self.model.transcribe.assert_not_called()

    def test_punctuation_only_is_not_dictated(self):
        self.output(segment(". ..."))
        self.assertEqual(self.stt.transcribe(self.pcm), "")

    def test_stereo_input_fails_before_model(self):
        with self.assertRaisesRegex(ValueError, "mono"):
            self.stt.transcribe(np.ones((100, 2), dtype=np.int16))
        self.model.transcribe.assert_not_called()

    def test_logs_do_not_contain_transcript(self):
        self.output(segment("private phrase", probability=0.9, logprob=-2))
        with self.assertLogs("voiced.stt", level="DEBUG") as logs:
            self.stt.transcribe(self.pcm)
        self.assertNotIn("private phrase", " ".join(logs.output))

    def test_load_uses_cached_model_with_bounded_cpu_and_one_worker(self):
        self.stt._model = None
        self.model.transcribe.return_value = (iter([]), None)
        with patch("voiced.stt.WhisperModel", return_value=self.model) as factory:
            self.stt.load()
            self.stt.load()
        factory.assert_called_once_with(
            "distil-small.en", device="cpu", compute_type="int8",
            cpu_threads=2, num_workers=1, local_files_only=True,
        )
        self.model.transcribe.assert_called_once()
        self.assertEqual(self.model.transcribe.call_args.kwargs["beam_size"], 1)


if __name__ == "__main__":
    unittest.main()
