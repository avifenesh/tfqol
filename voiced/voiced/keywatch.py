"""Watch RightAlt for a 500ms long-press to arm voiced.

Design:
  - Monitor all keyboard-class /dev/input/event* devices via evdev.
  - On RightAlt key-down: start a timer.
  - If any other key goes down during the hold → cancel (user is doing a
    normal Alt+X combo).
  - On release after the threshold, fire on_arm. Short taps request finish.
  - Waiting for release prevents virtual typing with RightAlt still held.

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
RESCAN_SEC = 60  # how often to look for hot-plugged keyboards


def _is_keyboard(d: evdev.InputDevice) -> bool:
    caps = d.capabilities()
    keys = caps.get(ecodes.EV_KEY, [])
    # a real keyboard exposes at least the A key (30).
    return ecodes.KEY_A in keys


_IGNORE_NAME_SUBSTRINGS = (
    "ydotoold",        # our own ydotool virtual keyboard: avoid feedback loops
    "mouse",           # mice with key codes (MX Master): noisy for RightAlt detection
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
            log.debug("keywatch: %s (%s)", p, d.name)
        d.close()
    return out


def find_pointers() -> list[Path]:
    """Physical clicks and scrolling invalidate the live draft's caret."""
    out = []
    for path in Path("/dev/input").glob("event*"):
        try:
            device = evdev.InputDevice(str(path))
            caps = device.capabilities()
            keys = caps.get(ecodes.EV_KEY, [])
            if "ydotool" not in device.name.lower() and any(
                key in keys for key in (ecodes.BTN_LEFT, ecodes.BTN_TOUCH)
            ):
                out.append(path)
            device.close()
        except OSError:
            continue
    return out


class Gesture:
    """Activate only after a solitary RightAlt hold and release."""

    def __init__(self, hold_ms, arm, tap, interaction):
        self.hold_ms, self.arm, self.tap, self.interaction = hold_ms, arm, tap, interaction
        self.down = set()
        self.started = None
        self.cancelled = False

    def lost_device(self):
        self.started = None
        self.cancelled = True
        self.interaction()

    def event(self, device, code, value, now):
        key = (device, code)
        if value == 2:
            return
        if code == ecodes.KEY_RIGHTALT:
            if value == 1:
                self.started = (device, now)
                self.cancelled = bool(self.down)
                self.down.add(key)
            elif value == 0:
                self.down.discard(key)
                if self.started and self.started[0] == device and not self.cancelled:
                    elapsed = (now - self.started[1]) * 1000
                    (self.arm if elapsed >= self.hold_ms else self.tap)()
                self.started = None
        elif value == 1:
            self.down.add(key)
            self.cancelled = True
            self.interaction()
        elif value == 0:
            self.down.discard(key)


class KeyWatcher:
    """Background thread that fires `on_arm()` on a qualifying RightAlt long-press."""

    def __init__(self, on_arm: Callable[[], None], hold_ms: int = HOLD_MS,
                 on_interaction: Callable[[], None] | None = None,
                 on_tap: Callable[[], None] | None = None):
        self.on_arm = on_arm
        self.hold_ms = hold_ms
        self.on_interaction = on_interaction or (lambda: None)
        self.on_tap = on_tap or (lambda: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)

    def _run(self) -> None:
        sel = selectors.DefaultSelector()
        devs: dict[str, evdev.InputDevice] = {}  # path -> device
        gesture = Gesture(self.hold_ms, self.on_arm, self.on_tap, self.on_interaction)

        def add(path: Path) -> None:
            sp = str(path)
            if sp in devs:
                return
            try:
                d = evdev.InputDevice(sp)
            except OSError as e:
                log.warning("keywatch: can't open %s: %s", sp, e)
                return
            devs[sp] = d
            sel.register(d, selectors.EVENT_READ)
            log.info("keywatch: + %s (%s)", sp, d.name)

        def drop(path: str, reason: str) -> None:
            d = devs.pop(path, None)
            if d is None:
                return
            try:
                sel.unregister(d)
            except (KeyError, ValueError):
                pass
            try:
                d.close()
            except OSError:
                pass
            log.warning("keywatch: - %s (%s)", path, reason)
            gesture.lost_device()
            gesture.down = {key for key in gesture.down if key[0] != path}

        for p in [*find_keyboards(), *find_pointers()]:
            add(p)
        if not devs:
            log.warning("keywatch: waiting for readable input devices")

        log.info("keywatch: monitoring %d input devices for RightAlt long-press (%d ms)",
                 len(devs), self.hold_ms)

        last_rescan = time.monotonic()

        while not self._stop.is_set():
            events = sel.select(timeout=0.05)  # 50ms granularity
            now = time.monotonic()

            for key, _ in events:
                dev: evdev.InputDevice = key.fileobj
                try:
                    for ev in dev.read():
                        if ev.type == ecodes.EV_REL and ev.code in (ecodes.REL_WHEEL, ecodes.REL_HWHEEL):
                            self.on_interaction()
                        if ev.type != ecodes.EV_KEY:
                            continue
                        gesture.event(dev.path, ev.code, ev.value, ev.timestamp())
                except OSError as e:
                    drop(dev.path, f"read failed: {e}")

            if now - last_rescan >= RESCAN_SEC:
                last_rescan = now
                current = {str(p) for p in [*find_keyboards(), *find_pointers()]}
                for gone in [p for p in devs if p not in current]:
                    drop(gone, "disappeared")
                for p in current:
                    if p not in devs:
                        add(Path(p))
                if not devs:
                    log.warning("keywatch: waiting for input devices to reconnect")
                    self.on_interaction()
        for path in list(devs):
            drop(path, "shutdown")
        sel.close()
