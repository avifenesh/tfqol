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
        )
        # warm up with 0.5s of silence
        warm = np.zeros(self.s.sample_rate // 2, dtype=np.float32)
        list(self._model.transcribe(warm, beam_size=self.s.whisper_beam,
                                    language="en", vad_filter=False)[0])
        log.info("faster-whisper %s/%s loaded in %.2fs",
                 self.s.whisper_model, self.s.whisper_compute, time.perf_counter() - t0)

    # Common Whisper hallucinations on silence / noise.
    _HALLUCINATIONS = frozenset({
        "", ".", "you", "thank you.", "thanks.", "thank you for watching.",
        "uh-huh.", "uh-huh", "uh.", "hmm.", "okay.", "ok.", "oh.", "bye.",
        "mm-hmm.", "yeah.",
    })

    def transcribe(self, pcm_int16: np.ndarray) -> str:
        if self._model is None:
            self.load()
        assert self._model is not None
        audio = pcm_int16.astype(np.float32) / 32768.0
        segments, _ = self._model.transcribe(
            audio,
            beam_size=self.s.whisper_beam,
            language="en",
            vad_filter=False,
            condition_on_previous_text=False,
            temperature=0.0,
            no_speech_threshold=0.5,
            log_prob_threshold=-1.0,
        )
        parts: list[str] = []
        for seg in segments:
            log.info("stt seg: nsp=%.2f logprob=%.2f text=%r",
                     seg.no_speech_prob or 0, seg.avg_logprob or 0, seg.text)
            if seg.no_speech_prob is not None and seg.no_speech_prob > 0.5:
                log.debug("dropped (no_speech_prob=%.2f): %r",
                          seg.no_speech_prob, seg.text)
                continue
            parts.append(seg.text.strip())
        text = " ".join(parts).strip()
        # squash known hallucinations
        if text.lower().strip() in self._HALLUCINATIONS:
            log.debug("dropped hallucination: %r", text)
            return ""
        return text
