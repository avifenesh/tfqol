"""Stream a live draft and refine it after a longer automatic pause."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import json
import fcntl
from logging.handlers import RotatingFileHandler
from contextlib import closing
from pathlib import Path

from . import audio, router
from .config import DEFAULT, Settings, ensure_dirs, LOG_FILE, RUNTIME_DIR
from .keywatch import KeyWatcher
from .stt import STT
from .pipeline import dictate

PIDFILE = Path(RUNTIME_DIR) / "voiced.pid"
STATEFILE = Path(RUNTIME_DIR) / "state.json"
RECOVERY = Path(RUNTIME_DIR) / "latest.txt"
MUTEFILE = Path(RUNTIME_DIR) / "voiced.muted"

log = logging.getLogger("voiced")


def setup_logging(verbose: bool) -> None:
    ensure_dirs()
    fmt = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format=fmt, handlers=handlers, force=True)


def _notify(title: str, body: str = "") -> None:
    try:
        subprocess.Popen(
            ["notify-send", "--app-name", "voiced", "--expire-time", "3000",
             "--hint", "string:x-canonical-private-synchronous:voiced",
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
        self._cancel = threading.Event()
        self._finish = threading.Event()
        self._interaction_epoch = 0
        self._worker = None
        self._state = None

    def _set_state(self, state):
        if self._state == state:
            return
        self._state = state
        payload = {"state": state, "pid": os.getpid(),
                   "finish_pause_ms": self.s.session_silence_ms,
                   "recovery_available": RECOVERY.exists()}
        temporary = STATEFILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload))
        temporary.replace(STATEFILE)
        log.info("state: %s", state)
        if state == "correcting":
            _notify("voiced", "Finishing the text…")

    def _remember(self, text):
        temporary = RECOVERY.with_suffix(".tmp")
        temporary.write_text(text)
        temporary.chmod(0o600)
        temporary.replace(RECOVERY)

    def run(self) -> int:
        for p in router.check_prereqs():
            log.warning("prereq: %s", p)

        ensure_dirs()
        try:
            PIDFILE.write_text(str(os.getpid()))
        except OSError as e:
            log.warning("pidfile write failed: %s", e)

        self.stt.load()
        self._set_state("idle")

        watcher = KeyWatcher(self._on_arm, hold_ms=self.s.hold_ms,
                             on_interaction=self._on_interaction, on_tap=self._finish_now)
        watcher.start()
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGUSR1, self._mute)
        signal.signal(signal.SIGUSR2, self._unmute)
        signal.signal(signal.SIGHUP, lambda *_: self._finish_now())

        log.info("voiced up. long-press RightAlt (%dms) to dictate", self.s.hold_ms)

        try:
            self._stop.wait()
        finally:
            watcher.stop()
            self._cancel.set()
            if self._worker:
                self._worker.join(timeout=5)
            try:
                PIDFILE.unlink(missing_ok=True)
                STATEFILE.unlink(missing_ok=True)
            except OSError:
                pass
        return 0

    def _on_signal(self, *_):
        log.info("signal received, shutting down")
        self._stop.set()
        self._cancel.set()

    def _mute(self, *_):
        MUTEFILE.touch(mode=0o600)
        self._cancel.set()

    def _unmute(self, *_):
        MUTEFILE.unlink(missing_ok=True)

    def _finish_now(self):
        if self._busy.locked():
            self._finish.set()

    def _on_interaction(self):
        self._interaction_epoch += 1
        if self._busy.locked():
            self._cancel.set()

    def _on_arm(self) -> None:
        if MUTEFILE.exists():
            _notify("voiced is muted", "Run voicectl unmute to enable dictation.")
            return
        # Ignore re-triggers while a session is already running.
        if not self._busy.acquire(blocking=False):
            self._finish.set()
            return
        self._cancel.clear()
        self._finish.clear()
        epoch = self._interaction_epoch
        self._worker = threading.Thread(target=self._session, args=(epoch,), daemon=True)
        self._worker.start()

    def _session(self, epoch) -> None:
        try:
            draft = router.DraftWriter(lambda: self._cancel.is_set() or self._interaction_epoch != epoch)
            RECOVERY.unlink(missing_ok=True)
            self._set_state("listening")
            _notify("voiced", f"Listening. Pause {self.s.session_silence_ms / 1000:g}s to finish, or tap RightAlt.")
            with closing(audio.record_updates(self.s, self._cancel, self._finish)) as updates:
                result = dictate(updates, self.stt, draft, self._set_state,
                                 self._remember, self._cancel.set)
            if result.blocked or self._cancel.is_set():
                self._set_state("interrupted")
                _notify("voiced stopped editing", (result.blocked or "You interacted with the keyboard or mouse.") + " Use voicectl copy for the latest text.")
            elif not result.text:
                self._set_state("idle")
                _notify("voiced", "No speech recognized.")
            else:
                self._set_state("done")
                _notify("voiced", "Dictation corrected. Review before sending." if result.corrected else "Draft kept. No final correction was available.")
            log.info("session finished: previews=%d final=%s blocked=%s", result.previews,
                     result.corrected, bool(result.blocked))
        except Exception:
            log.exception("dictation session failed")
            self._set_state("error")
            _notify("voiced", "Dictation failed. Check voicectl logs.")
        finally:
            self._busy.release()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="voiced", description="Push-to-arm voice dictation")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    os.umask(0o077)
    setup_logging(args.verbose)
    with (Path(RUNTIME_DIR) / "daemon.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.error("voiced is already running")
            return 1
        return Daemon(DEFAULT).run()


if __name__ == "__main__":
    raise SystemExit(main())
