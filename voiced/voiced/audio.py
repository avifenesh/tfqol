"""On-demand mic capture: record until N ms of trailing silence.

Only opens the device when `record_once()` is called, closes it at the end.
That lets the BT headphones revert to A2DP when idle and frees the mic for
phone calls.
"""
from __future__ import annotations

import collections
import logging
import queue
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
import webrtcvad

from .config import Settings

log = logging.getLogger(__name__)


@dataclass
class Utterance:
    pcm: np.ndarray
    sample_rate: int

    @property
    def seconds(self) -> float:
        return len(self.pcm) / self.sample_rate


def _resolve_device(name: str | None) -> int | None:
    if not name:
        return None
    want = name.lower()
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] <= 0:
            continue
        if want in d["name"].lower():
            log.info("using input device [%d] %r (matched %r)", i, d["name"], want)
            return i
    log.warning("no input device matching %r; using default", want)
    return None


def record_once(s: Settings, *, max_seconds: float = 60.0) -> Utterance | None:
    """Open the mic, record up to max_seconds, return on `s.session_silence_ms`
    of trailing silence after speech is detected. Returns None if no speech.
    """
    vad = webrtcvad.Vad(s.vad_aggressiveness)
    frame_samples = s.sample_rate * s.frame_ms // 1000
    preroll_frames = max(1, s.preroll_ms // s.frame_ms)
    end_silence_frames = max(1, s.session_silence_ms // s.frame_ms)
    max_frames = int(max_seconds * 1000 // s.frame_ms)

    raw_q: queue.Queue[bytes] = queue.Queue(maxsize=256)

    def on_audio(indata, frames, time_info, status):
        try:
            raw_q.put_nowait(bytes(indata))
        except queue.Full:
            pass

    device = _resolve_device(s.input_device_name)
    preroll: collections.deque[bytes] = collections.deque(maxlen=preroll_frames)
    voiced: list[bytes] = []
    triggered = False
    trailing = 0
    heard_speech = False

    stream = sd.RawInputStream(
        samplerate=s.sample_rate,
        channels=1,
        dtype="int16",
        blocksize=frame_samples,
        callback=on_audio,
        device=device,
    )
    try:
        stream.start()
        log.info("mic open (device=%s)", device if device is not None else "default")
        for _ in range(max_frames):
            try:
                frame = raw_q.get(timeout=0.5)
            except queue.Empty:
                # No audio coming through — device likely stuck. Bail.
                log.warning("no audio for 500ms; aborting capture")
                break
            try:
                is_speech = vad.is_speech(frame, s.sample_rate)
            except Exception:  # noqa: BLE001
                is_speech = False

            if not triggered:
                preroll.append(frame)
                if is_speech:
                    triggered = True
                    heard_speech = True
                    voiced.extend(preroll)
                    preroll.clear()
                    trailing = 0
            else:
                voiced.append(frame)
                if is_speech:
                    trailing = 0
                else:
                    trailing += 1
                    if trailing >= end_silence_frames:
                        break
    finally:
        stream.stop()
        stream.close()
        log.info("mic closed (voiced_frames=%d, heard=%s)", len(voiced), heard_speech)

    if not heard_speech:
        return None
    pcm = np.frombuffer(b"".join(voiced), dtype=np.int16)
    return Utterance(pcm=pcm, sample_rate=s.sample_rate)
