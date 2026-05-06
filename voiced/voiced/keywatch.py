"""Watch RightAlt for a 500ms long-press to arm voiced.

Design:
  - Monitor all keyboard-class /dev/input/event* devices via evdev.
  - On RightAlt key-down: start a timer.
  - If any other key goes down during the hold → cancel (user is doing a
    normal Alt+X combo).
  - If the hold reaches the threshold and no other key has been pressed →
    fire the on_arm callback.
  - Once armed, don't re-fire until the user releases RightAlt.

Requires the `input` group (already granted on this system).
"""
from __future__ import annotations

import logging
import selectors
import threading
import time
from pathlib import Path
from typing import Callable

import evdev
from evdev import ecodes

log = logging.getLogger(__name__)

HOLD_MS = 500  # long-press threshold


def _is_keyboard(d: evdev.InputDevice) -> bool:
    caps = d.capabilities()
    keys = caps.get(ecodes.EV_KEY, [])
    # a real keyboard exposes at least the A key (30).
    return ecodes.KEY_A in keys


_IGNORE_NAME_SUBSTRINGS = (
    "ydotoold",        # our own ydotool virtual keyboard — avoid feedback loops
    "mouse",           # mice with key codes (MX Master) — noisy for RightAlt detection
    "video bus",
    "power button",
    "sleep button",
    "consumer control",
)


def find_keyboards() -> list[Path]:
    out: list[Path] = []
    for p in sorted(Path("/dev/input").glob("event*")):
        try:
            d = evdev.InputDevice(str(p))
        except (PermissionError, OSError):
            continue
        name_l = (d.name or "").lower()
        if any(s in name_l for s in _IGNORE_NAME_SUBSTRINGS):
            log.debug("keywatch: skipping %s (%s)", p, d.name)
            d.close()
            continue
        if _is_keyboard(d):
            out.append(p)
            log.info("keywatch: %s (%s)", p, d.name)
        d.close()
    return out


class KeyWatcher:
    """Background thread that fires `on_arm()` on a qualifying RightAlt long-press."""

    def __init__(self, on_arm: Callable[[], None], hold_ms: int = HOLD_MS):
        self.on_arm = on_arm
        self.hold_ms = hold_ms
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        paths = find_keyboards()
        if not paths:
            log.error("keywatch: no keyboards found (is user in `input` group?)")
            return
        devs: list[evdev.InputDevice] = []
        try:
            for p in paths:
                try:
                    d = evdev.InputDevice(str(p))
                    d.grab  # noqa: B018 — just check attribute presence
                    devs.append(d)
                except OSError as e:
                    log.warning("keywatch: can't open %s: %s", p, e)
        except Exception:  # noqa: BLE001
            log.exception("keywatch: dev open failed")
            return

        sel = selectors.DefaultSelector()
        for d in devs:
            sel.register(d, selectors.EVENT_READ)

        hold_start: float | None = None
        armed_pending = False  # True once hold_ms elapsed; reset on release
        other_key_during_hold = False

        log.info("keywatch: monitoring %d keyboard(s) for RightAlt long-press (%d ms)",
                 len(devs), self.hold_ms)

        while not self._stop.is_set():
            events = sel.select(timeout=0.05)  # 50ms granularity
            now = time.monotonic()

            # On every tick, check if we should fire
            if hold_start is not None and not armed_pending and not other_key_during_hold:
                if (now - hold_start) * 1000 >= self.hold_ms:
                    armed_pending = True
                    try:
                        log.info("keywatch: fire ARM (hold %.0fms)", (now - hold_start) * 1000)
                        self.on_arm()
                    except Exception:  # noqa: BLE001
                        log.exception("keywatch: on_arm callback crashed")

            for key, _ in events:
                dev: evdev.InputDevice = key.fileobj
                try:
                    for ev in dev.read():
                        if ev.type != ecodes.EV_KEY:
                            continue
                        code = ev.code
                        val = ev.value  # 0 up, 1 down, 2 repeat
                        if code == ecodes.KEY_RIGHTALT:
                            if val == 1:  # down
                                hold_start = now
                                armed_pending = False
                                other_key_during_hold = False
                            elif val == 0:  # up
                                hold_start = None
                                armed_pending = False
                                other_key_during_hold = False
                            # repeats ignored
                        else:
                            # Any non-RightAlt key press during the hold cancels.
                            if val == 1 and hold_start is not None and not armed_pending:
                                other_key_during_hold = True
                                log.debug("keywatch: hold cancelled (other key %d)", code)
                except OSError:
                    continue
