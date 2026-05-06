# voiced

Push-to-talk voice dictation for Linux. CPU-only Whisper, no network.

**Hold RightAlt for 500 ms** → mic opens → speak → stop → 2 s of silence later
the transcription is typed into whichever window has keyboard focus (no Enter —
you review and send yourself).

Idle cost: **zero audio, zero inference** — mic is closed, so Bluetooth
headphones stay in A2DP and phone calls work normally.

## Install

Requirements:
- Linux with `ydotoold` running (`systemctl --user status ydotool`)
- User in `input` group (for `/dev/input/event*` read access)
- `libportaudio2` (for the sounddevice Python bindings)

```bash
cd ~/projects/tfqol/voiced
uv venv && uv sync && uv pip install -e .

# pin the input device (substring match on the sounddevice device name)
export VOICED_INPUT=bluez      # Sony WH-1000XM6 HFP profile
# or leave unset to use the system default

.venv/bin/voiced -v            # foreground
```

Autostart as a user service:

```bash
cp systemd/voiced.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now voiced
```

## Tunables (in `voiced/config.py`)

| setting | default | what |
|---|---|---|
| `hold_ms` | 500 | RightAlt long-press threshold |
| `session_silence_ms` | 2000 | trailing silence that ends a session |
| `whisper_model` | `distil-small.en` | ~160 MB, ~10× realtime on CPU |
| `vad_aggressiveness` | 2 | 0–3 (higher = more aggressive silence detection) |

## Architecture

```
RightAlt long-press (evdev on /dev/input/event*)
       ↓
  audio.record_once() — opens mic, VAD endpointer
       ↓
  2 s of silence after first speech
       ↓
  faster-whisper (CPU int8, 4 threads)
       ↓
  ydotool type  (no Enter — you review)
       ↓
  mic closed, back to idle
```

Key design calls:
- **Hold to arm**, not hold to record. Avoids accidental Alt+X combos — if any
  other key is pressed during the hold, the arm is cancelled.
- **No Enter on type.** The transcription lands in the input line and waits.
- **No wake word.** Whisper was unreliable as a wake detector on a noisy mic
  and kept the BT profile stuck in HFP. A physical key is deterministic.
- **No focus switching.** Types into whatever window already has focus. If you
  want a different window, focus it before pressing RightAlt.

## Operator CLI

```
voicectl status        # show pid / log path
voicectl logs -n 100   # tail ~/.cache/voiced/voiced.log
voicectl mute          # touch-file + SIGUSR1 (no-op under push-to-talk; kept for symmetry)
voicectl unmute
```

## Troubleshooting

- **No response when I hold RightAlt** — check `voicectl logs -n 30`. You should
  see `ARM (hold ...ms)` on release. If nothing fires, verify user is in `input`
  group: `id | grep input`. Re-login if just added.
- **BT headphones in HFP when idle** — shouldn't happen. Check no other app is
  holding the mic. PipeWire auto-switches profiles based on who's using the mic.
- **Transcription types garbage** — Whisper mis-hears on noisy mic. Move closer,
  reduce background noise, or try `whisper_model = "distil-large-v3"` in
  `config.py` (larger RAM footprint, slightly slower).
- **`/dev/input/event*` permission denied** — not in `input` group. Run
  `sudo usermod -aG input $USER`, then log out + in.
