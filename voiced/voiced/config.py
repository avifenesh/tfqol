"""Paths, tunables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voiced"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "voiced"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "voiced"
LOG_FILE = CACHE_DIR / "voiced.log"


@dataclass
class Settings:
    # single wake phrase. Chosen for uniqueness — won't fire mid-speech.
    wake_phrase: str = "hi dino"
    cmd_send: tuple[str, ...] = (
        "send it", "send the message", "send message", "send now",
        "just send it", "send that",
    )
    cmd_stop: tuple[str, ...] = ("stop listening", "stop dino", "bye dino")

    # audio
    sample_rate: int = 16000
    frame_ms: int = 30
    vad_aggressiveness: int = 2
    preroll_ms: int = 300
    session_silence_ms: int = 2000  # end a session after this much quiet
    input_device_name: str | None = os.environ.get("VOICED_INPUT") or None

    # key trigger
    hold_ms: int = 500

    # stt
    whisper_model: str = "distil-small.en"
    whisper_compute: str = "int8"
    whisper_threads: int = 4
    whisper_beam: int = 1


DEFAULT = Settings()


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
