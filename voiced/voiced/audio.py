"""On-demand capture with cumulative previews and a pause-delimited final pass.

Capture runs independently of transcription. A slow decoder sees the newest
preview while silence detection and microphone cleanup keep running.
"""
from __future__ import annotations

import collections
import logging
import math
import queue
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Iterator

import numpy as np
import sounddevice as sd
import webrtcvad

from .config import Settings

log = logging.getLogger(__name__)

_RAW_QUEUE_FRAMES = 256
_MIN_SPEECH_MS = 150
_SPEECH_WINDOW_MS = 300
_FINAL_TAIL_MS = 300
_CAPTURE_STOP_TIMEOUT = 2.0


@dataclass
class Utterance:
    pcm: np.ndarray
    sample_rate: int

    @property
    def seconds(self) -> float:
        return len(self.pcm) / self.sample_rate


@dataclass
class AudioUpdate(Utterance):
    final: bool


class AudioCaptureError(RuntimeError):
    """Capture failed; its incomplete audio must not be treated as final."""


class AudioShutdownError(AudioCaptureError):
    """The capture thread is still alive; only process exit guarantees release."""


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


def _capture(
    s: Settings,
    cancel: threading.Event,
    finish: threading.Event | None,
    stop: threading.Event,
    publish: Callable[[AudioUpdate], None],
    closing: threading.Event,
) -> AudioUpdate | None:
    if s.frame_ms not in (10, 20, 30):
        raise ValueError("frame_ms must be 10, 20, or 30 for WebRTC VAD")
    if s.sample_rate not in (8000, 16000, 32000, 48000):
        raise ValueError("sample_rate is not supported by WebRTC VAD")
    if s.max_session_seconds <= 0 or s.stream_interval_ms <= 0 or s.session_silence_ms <= 0:
        raise ValueError("capture duration, stream interval, and silence timeout must be positive")

    vad = webrtcvad.Vad(s.vad_aggressiveness)
    frame_samples = s.sample_rate * s.frame_ms // 1000
    min_speech_frames = math.ceil(_MIN_SPEECH_MS / s.frame_ms)
    speech_window_frames = math.ceil(_SPEECH_WINDOW_MS / s.frame_ms)
    preroll_frames = max(min_speech_frames, math.ceil(s.preroll_ms / s.frame_ms))
    end_silence_frames = math.ceil(s.session_silence_ms / s.frame_ms)
    preview_frames = max(1, math.ceil(s.stream_interval_ms / s.frame_ms))
    max_frames = max(1, math.ceil(s.max_session_seconds * 1000 / s.frame_ms))
    raw_q: queue.Queue[bytes] = queue.Queue(maxsize=_RAW_QUEUE_FRAMES)
    capture_failed = threading.Event()
    callback_errors: list[AudioCaptureError] = []

    def fail(message: str) -> None:
        if not capture_failed.is_set():
            callback_errors.append(AudioCaptureError(message))
            capture_failed.set()

    def on_audio(indata, frames, time_info, status):
        if cancel.is_set() or stop.is_set() or capture_failed.is_set():
            return
        if status:
            fail(f"Microphone capture reported an audio error: {status}")
            return
        if frames != frame_samples or len(indata) != frame_samples * 2:
            fail("Microphone returned an unexpected audio frame size")
            return
        try:
            raw_q.put_nowait(bytes(indata))
        except queue.Full:
            fail("Microphone capture fell behind and overflowed; retry dictation")

    preroll: collections.deque[tuple[bytes, bool]] = collections.deque(maxlen=preroll_frames)
    recent_speech: collections.deque[bool] = collections.deque(maxlen=speech_window_frames)
    voiced: list[bytes] = []
    triggered = False
    trailing = 0
    unvoiced_tail = 0
    pause_deadline: int | None = None
    total_frames = 0
    last_preview = 0
    pending_speech = False

    def consume(frame: bytes) -> bool:
        """Return true when a pause or the frame duration ends this session."""
        nonlocal triggered, trailing, unvoiced_tail, pause_deadline, total_frames, pending_speech
        total_frames += 1
        # A VAD/device error is a capture failure, never a synthetic quiet frame.
        is_speech = vad.is_speech(frame, s.sample_rate)
        recent_speech.append(is_speech)
        confirmed_speech = is_speech and sum(recent_speech) >= min_speech_frames
        if not triggered:
            preroll.append((frame, is_speech))
            if confirmed_speech:
                triggered = True
                voiced.extend(audio for audio, _ in preroll)
                preroll.clear()
        else:
            voiced.append(frame)
            trailing = 0 if confirmed_speech else trailing + 1
        unvoiced_tail = 0 if is_speech else unvoiced_tail + 1
        if triggered and confirmed_speech:
            pending_speech = True
            pause_deadline = None
        if trailing >= end_silence_frames:
            if pause_deadline is None:
                # A word beginning at the deadline gets one bounded window to
                # prove it is speech. Further blips cannot renew this grace.
                grace = speech_window_frames if any(recent_speech) else 0
                pause_deadline = total_frames + grace
            if total_frames >= pause_deadline:
                return True
        return total_frames >= max_frames

    def snapshot(*, final: bool) -> AudioUpdate:
        # Keep every spoken frame and internal pause, but trim the long idle tail.
        tail_frames = math.ceil(_FINAL_TAIL_MS / s.frame_ms)
        # Keep even unconfirmed short speech in the final audio. Confirmation
        # controls the endpoint, not which words the decoder is allowed to hear.
        trim = max(0, unvoiced_tail - tail_frames) if final else 0
        frames = voiced[:-trim] if trim else voiced
        pcm = np.frombuffer(b"".join(frames), dtype=np.int16)
        return AudioUpdate(pcm=pcm, sample_rate=s.sample_rate, final=final)

    device = _resolve_device(s.input_device_name)
    stream = sd.RawInputStream(
        samplerate=s.sample_rate,
        channels=1,
        dtype="int16",
        blocksize=frame_samples,
        callback=on_audio,
        device=device,
    )
    natural_end = False
    explicit_finish = False
    try:
        stream.start()
        log.info("mic open (device=%s)", device if device is not None else "default")
        started = last_audio = time.monotonic()
        while not (cancel.is_set() or stop.is_set()):
            if capture_failed.is_set():
                raise callback_errors[0]
            if finish is not None and finish.is_set():
                explicit_finish = True
                break
            if time.monotonic() - started >= s.max_session_seconds:
                break
            try:
                first = raw_q.get(timeout=0.1)
            except queue.Empty:
                if time.monotonic() - last_audio >= 0.5:
                    raise AudioCaptureError("No microphone audio received for 500 ms")
                continue
            last_audio = time.monotonic()
            batch = [first]
            # Coalesce queued frames before publishing a preview. Decoding never
            # has to catch up with a queue of obsolete transcript revisions.
            for _ in range(raw_q.qsize()):
                try:
                    batch.append(raw_q.get_nowait())
                except queue.Empty:
                    break
            for frame in batch:
                if consume(frame):
                    natural_end = True
                    break
            if natural_end:
                break
            if pending_speech and len(voiced) - last_preview >= preview_frames:
                if capture_failed.is_set():
                    raise callback_errors[0]
                publish(snapshot(final=False))
                last_preview = len(voiced)
                pending_speech = False
    finally:
        # close must still run if start or stop raises (device unplug, shutdown).
        closing.set()
        try:
            stream.stop()
        finally:
            stream.close()
            log.info("mic closed (frames=%d, speech=%s)", total_frames, triggered)

    if cancel.is_set() or stop.is_set():
        return None
    if capture_failed.is_set():
        raise callback_errors[0]
    if explicit_finish and not natural_end:
        # stop() has quiesced callbacks. Include audio captured just before the
        # finish request, without ever reopening the microphone.
        while not raw_q.empty():
            if consume(raw_q.get_nowait()):
                break
    return snapshot(final=True) if triggered else None


def record_updates(
    s: Settings,
    cancel: threading.Event,
    finish: threading.Event | None = None,
) -> Iterator[AudioUpdate]:
    """Yield the latest cumulative draft and one final update, if speech exists.

    The microphone closes on the capture thread even while the caller decodes a
    draft. Closing this iterator or setting cancel stops capture without a final
    update. Explicit finish returns the speech captured so far.
    """
    if cancel.is_set():
        return
    updates: queue.Queue[AudioUpdate | Exception | None] = queue.Queue(maxsize=1)
    stop = threading.Event()
    closing = threading.Event()
    shutdown_timed_out = False

    def publish(update: AudioUpdate | Exception | None) -> None:
        try:
            updates.put_nowait(update)
        except queue.Full:
            try:
                updates.get_nowait()
            except queue.Empty:
                pass
            updates.put_nowait(update)

    def run() -> None:
        try:
            publish(_capture(s, cancel, finish, stop, publish, closing))
        except Exception as exc:
            publish(exc)

    worker = threading.Thread(target=run, name="voiced-capture", daemon=True)
    worker.start()
    try:
        while not cancel.is_set():
            try:
                update = updates.get(timeout=0.1)
            except queue.Empty:
                if closing.is_set():
                    worker.join(timeout=_CAPTURE_STOP_TIMEOUT)
                    if worker.is_alive():
                        shutdown_timed_out = True
                        raise AudioShutdownError(
                            "Microphone shutdown timed out; closure is unconfirmed. Restart voiced."
                        )
                continue
            if isinstance(update, Exception):
                raise update
            if update is None or cancel.is_set():
                return
            yield update
            if update.final:
                return
    finally:
        stop.set()
        worker.join(timeout=0 if shutdown_timed_out else _CAPTURE_STOP_TIMEOUT)
        if worker.is_alive() and not shutdown_timed_out:
            raise AudioShutdownError(
                "Microphone shutdown timed out; closure is unconfirmed. Restart voiced."
            )


def record_once(s: Settings, *, max_seconds: float = 60.0) -> Utterance | None:
    """Compatibility wrapper for callers that only need the completed audio."""
    settings = replace(s, max_session_seconds=max_seconds)
    for update in record_updates(settings, threading.Event()):
        if update.final:
            return Utterance(pcm=update.pcm, sample_rate=update.sample_rate)
    return None
