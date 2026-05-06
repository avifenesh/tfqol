"""voiced — push-to-arm voice dictation.

Long-press RightAlt (500ms) → mic opens → speak → 2s silence → transcribe →
type (no Enter) into the focused window → mic closes. Idle = zero audio,
zero model inference, BT headphones revert to A2DP.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from . import audio, router
from .config import DEFAULT, Settings, ensure_dirs, LOG_FILE, RUNTIME_DIR
from .keywatch import KeyWatcher
from .stt import STT

PIDFILE = Path(RUNTIME_DIR) / "voiced.pid"

log = logging.getLogger("voiced")


def setup_logging(verbose: bool) -> None:
    ensure_dirs()
    fmt = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format=fmt, handlers=handlers, force=True)


def _notify(title: str, body: str = "") -> None:
    try:
        subprocess.Popen(
            ["notify-send", "--app-name", "voiced", "--expire-time", "1200",
             "--icon", "audio-input-microphone", title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass


class Daemon:
    def __init__(self, settings: Settings):
        self.s = settings
        self.stt = STT(settings)
        self._busy = threading.Lock()
        self._stop = threading.Event()

    def run(self) -> int:
        for p in router.check_prereqs():
            log.warning("prereq: %s", p)

        ensure_dirs()
        try:
            PIDFILE.write_text(str(os.getpid()))
        except OSError as e:
            log.warning("pidfile write failed: %s", e)

        self.stt.load()

        watcher = KeyWatcher(self._on_arm, hold_ms=self.s.hold_ms)
        watcher.start()
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

        log.info("voiced up. long-press RightAlt (%dms) to dictate", self.s.hold_ms)

        try:
            self._stop.wait()
        finally:
            watcher.stop()
            try:
                PIDFILE.unlink(missing_ok=True)
            except OSError:
                pass
        return 0

    def _on_signal(self, *_):
        log.info("signal received, shutting down")
        self._stop.set()

    def _on_arm(self) -> None:
        # Ignore re-triggers while a session is already running.
        if not self._busy.acquire(blocking=False):
            log.info("arm: already in a session, ignoring")
            return
        threading.Thread(target=self._session, daemon=True).start()

    def _session(self) -> None:
        try:
            _notify("voiced", "listening...")
            log.info("SESSION START")
            utt = audio.record_once(self.s)
            if utt is None:
                log.info("SESSION: no speech heard")
                _notify("voiced", "(no speech)")
                return
            log.info("SESSION: captured %.2fs; transcribing", utt.seconds)
            text = self.stt.transcribe(utt.pcm)
            if not text:
                log.info("SESSION: empty transcription")
                _notify("voiced", "(nothing transcribed)")
                return
            log.info("TYPE: %r", text)
            try:
                router.type_text_focused(text, send=False)
                _notify("voiced → typed", text[:80])
            except router.RouterError as e:
                log.error("type failed: %s", e)
                _notify("voiced", f"type failed: {e}")
        finally:
            self._busy.release()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="voiced", description="Push-to-arm voice dictation")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    return Daemon(DEFAULT).run()


if __name__ == "__main__":
    raise SystemExit(main())
