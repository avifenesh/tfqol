"""faster-whisper CPU wrapper."""
from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np
from faster_whisper import WhisperModel

from .config import Settings

log = logging.getLogger(__name__)


class STT:
    def __init__(self, settings: Settings):
        self.s = settings
        self._model: Optional[WhisperModel] = None

    def load(self) -> None:
        if self._model is not None:
            return
        t0 = time.perf_counter()
        self._model = WhisperModel(
            self.s.whisper_model,
            device="cpu",
            compute_type=self.s.whisper_compute,
            cpu_threads=self.s.whisper_threads,
            num_workers=1,
            local_files_only=True,
        )
        # warm up with 0.5s of silence
        warm = np.zeros(self.s.sample_rate // 2, dtype=np.float32)
        list(self._model.transcribe(warm, beam_size=1,
                                    language="en", vad_filter=False)[0])
        log.info("faster-whisper %s/%s loaded in %.2fs",
                 self.s.whisper_model, self.s.whisper_compute, time.perf_counter() - t0)

    def transcribe(self, pcm_int16: np.ndarray, *, final: bool = False) -> str:
        """Decode cumulative audio, with a wider search for the final revision."""
        if pcm_int16.ndim != 1:
            raise ValueError("transcription expects mono PCM")
        if not len(pcm_int16) or not np.any(pcm_int16):
            return ""
        if self._model is None:
            self.load()
        assert self._model is not None
        audio = pcm_int16.astype(np.float32) / 32768.0
        segments, _ = self._model.transcribe(
            audio,
            beam_size=self.s.final_beam if final else 1,
            language="en",
            vad_filter=False,
            condition_on_previous_text=False,
            temperature=0.0,
            no_speech_threshold=0.5,
            log_prob_threshold=-1.0,
        )
        parts: list[str] = []
        for seg in segments:
            # Match Whisper's confidence rule. High-confidence short speech
            # such as "okay" is valid even when no_speech_prob is elevated.
            if (seg.no_speech_prob is not None and seg.no_speech_prob > 0.5
                    and (seg.avg_logprob is None or seg.avg_logprob <= -1.0)):
                log.debug("dropped uncertain segment (no_speech=%.2f)", seg.no_speech_prob)
                continue
            parts.append(seg.text.strip())
        text = " ".join(parts).strip()
        # Do not blacklist ordinary short phrases. Confidence and capture VAD
        # decide whether speech exists, rather than the words the user chose.
        return text if any(character.isalnum() for character in text) else ""
