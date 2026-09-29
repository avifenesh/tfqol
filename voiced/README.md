# voiced

Local streaming dictation for Linux. Hold **RightAlt for 500 ms**, then release
and speak. Text appears in the focused field while you talk. After **five seconds
of quiet**, voiced closes the microphone and decodes the complete utterance again
with a wider search to correct recognition and punctuation. It never presses Enter.

A short pause keeps the session open. Tap RightAlt to finish early. Typing,
clicking, scrolling, or moving focus stops automatic editing. The latest decoded
text remains available through `voicectl copy`.

## How corrections work

In fields that expose editable text through accessibility, voiced remembers the
original field, caret, and surrounding text. It checks those before each update
and replaces only the changed part of its own draft. If the application or user
changes the field, it stops instead of guessing which characters to erase.

Some terminals and applications do not expose a verifiable editable field. In
those, voiced streams extending text while holding back the last two provisional
words. It never backspaces blindly. If the final pass needs to change text already
inserted, the final transcript is kept for `voicectl copy` instead. Full automatic
correction therefore depends on the destination application's accessibility support.

The second pass is another speech recognition pass over the recorded audio. It
uses the same local model and does not send text to a language-model service or
paraphrase your words. The default model is English-only.

## Install

Requirements:

- Linux, Python 3.11+, `uv`, and `libportaudio2`.
- `ydotoold` running for fields that need keyboard input.
- User access to keyboard and pointer devices in `/dev/input` (usually the
  `input` group).
- For safe corrections: system `/usr/bin/python3` with PyGObject and Atspi 2.0
  bindings, a working desktop accessibility bus, and an accessible editable field.
- GNOME Wayland: the existing Computer Use Linux or Codex window-control extension
  for identifying focus. X11: `xdotool`.

```sh
cd ~/projects/tfqol/voiced
uv sync --frozen
# Download the default model once. Runtime loading is offline after this step.
.venv/bin/python -c 'from faster_whisper import WhisperModel; WhisperModel("distil-small.en", device="cpu", compute_type="int8", cpu_threads=2)'
ln -sf "$PWD/.venv/bin/voiced" ~/.local/bin/voiced
ln -sf "$PWD/.venv/bin/voicectl" ~/.local/bin/voicectl
cp systemd/voiced.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now voiced
```

On Debian/Ubuntu, accessibility bindings come from `python3-gi` and
`gir1.2-atspi-2.0`. The system interpreter handles accessibility separately from
the speech virtual environment.

## Configuration

The user service has a two-core CPU quota. The model uses CPU int8 and two threads.
The microphone is closed when idle; the loaded model stays resident for reuse.

| Setting | Default | Behavior |
|---|---|---|
| `VOICED_PAUSE_MS` | `5000` | Quiet period before automatic finish |
| `VOICED_INPUT` | System default | Input device name substring |
| `hold_ms` | `500` | RightAlt activation hold |
| `stream_interval_ms` | `1200` | Minimum new audio before a draft update; decoding adds latency |
| `max_session_seconds` | `60` | Maximum capture duration |
| `whisper_model` | `distil-small.en` | Cached English recognition model |
| `final_beam` | `5` | Search width for the final full-utterance decode |

The last five settings are in `voiced/config.py`. Set environment overrides with
`systemctl --user edit voiced`, for example:

```ini
[Service]
Environment=VOICED_PAUSE_MS=6000
```

Then run `systemctl --user restart voiced`.

## Controls

```text
voicectl status        service PID and current dictation state
voicectl finish        finish now and run the final pass
voicectl copy          copy the latest draft or correction from a desktop terminal
voicectl mute          stop recording and disable the trigger
voicectl unmute        enable the trigger again
voicectl logs -n 50    recent logs
voicectl logs -f       follow new logs
```

The latest transcript is stored with private permissions in
`$XDG_RUNTIME_DIR/voiced/latest.txt`, replaced during dictation, and cleared when a
new session starts. Audio is kept only in memory. Normal logs contain lifecycle
and timing information, not dictated text; log files rotate at 2 MB.

## Failure behavior

- A few noisy VAD frames cannot keep dictation open indefinitely. Confirmed speech
  needs 150 ms within a 300 ms window. A word starting at the finish boundary gets
  one bounded confirmation window.
- Audio capture runs independently of decoding. Slow inference receives the most
  recent accumulated audio instead of a backlog of obsolete drafts.
- An empty final transcription never erases a visible draft.
- Device errors, overflows, and unconfirmed microphone shutdown are reported.
- Cursor, field, focus, selection, or physical-input changes stop edits. No Ctrl+A,
  automatic refocus, or blind Backspace correction is used.

If a particular application has no editable accessibility interface, use
`voicectl copy` for corrections. Newly launched applications may be needed after
repairing a broken accessibility bus. Password fields with accessible protection
information are rejected.

## Verification

```sh
PYTHONPATH=voiced uv run --project voiced python -m unittest discover -s voiced/tests -v
```

Run the command above from the repository root. The contract tests do not open a
microphone or type into the desktop. End-to-end checks should use an isolated
editable test field with a known prefix and suffix, then verify both are preserved
across partial and final transcripts. `VOICED_DESKTOP_BACKEND=x11` confines that
check to the test display and avoids the host virtual keyboard.
