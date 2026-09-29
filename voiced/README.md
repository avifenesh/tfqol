# voiced

Local streaming dictation for Linux. Hold **RightAlt for 500 ms**, then release
and speak. Text appears in the focused field while you talk. After **two seconds
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

Chromium and Electron expose text and selection but usually lack the direct
editable-text API. Voiced resolves the focused paragraph, verifies the exact draft
range, selects only that range, and pastes literal text. Only a fixed Paste chord
is sent. Dictated characters never become key commands, so strings such as `\t`
remain text. Shortened corrections use a nonempty replacement instead of Delete
or Backspace. The resulting text and caret are read back before continuing.

Fields with no verifiable text interface are refused before recording. There is
no raw-keyboard fallback. Terminal scrollback is not treated as an editable field.
The latest interrupted transcript is still available through `voicectl copy`.

For Codex Desktop, run this once and then **quit Codex fully and reopen it from
the application menu**:

```sh
voicectl enable-codex-input
```

This creates a user desktop-entry override adding `--force-renderer-accessibility`.
It preserves existing launch arguments and actions and does not restart Codex.
An app that was started without accessibility cannot be repaired by changing its
next-launch flags alone. `voicectl doctor` reports the capability of whichever
field currently has focus.

Confirmed clipboard edits preserve the original advertised MIME formats and bytes unless
another application changes clipboard ownership during the operation. On GNOME,
this uses the XWayland clipboard bridge (`DISPLAY` and `XAUTHORITY` from the desktop
session). If there is no clipboard manager, a small helper retains the restored
clipboard until the next copy, service stop, or logout. If paste completion cannot
be verified, the helper keeps the dictated text available for a possible late
paste; it does not restore unrelated clipboard data into that pending request. Unreadable or oversized clipboards are
refused before mutation (32 formats, 16 MiB).

The second pass is another speech recognition pass over the recorded audio. It
uses the same local model and does not send text to a language-model service or
paraphrase your words. The default model is English-only.

## Install

Requirements:

- Linux, Python 3.11+, `uv`, and `libportaudio2`.
- `ydotoold` running for the fixed Paste chord on GNOME; `xdotool` on X11.
- User access to keyboard and pointer devices in `/dev/input` (usually the
  `input` group).
- For safe corrections: system `/usr/bin/python3` with PyGObject and Atspi 2.0
  bindings, a working desktop accessibility bus, and an accessible editable field.
- For Chromium/Electron pasting: Gdk/GTK 4 Python introspection bindings and
  an authenticated X11 display or XWayland clipboard bridge.
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
`gir1.2-atspi-2.0`; the clipboard helper also needs `gir1.2-gtk-4.0`. The system interpreter handles accessibility separately from
the speech virtual environment.

## Configuration

The user service has a two-core CPU quota. The model uses CPU int8 and two threads.
The microphone is closed when idle; the loaded model stays resident for reuse.

| Setting | Default | Behavior |
|---|---|---|
| `VOICED_PAUSE_MS` | `2000` | Quiet period before automatic finish |
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
Environment=VOICED_PAUSE_MS=2000
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

If an application has no readable focused text field, dictation refuses to start.
For Codex, enable the startup flag above and restart the application. Password
fields with accessible protection information are rejected.

## Verification

```sh
PYTHONPATH=voiced uv run --project voiced python -m unittest discover -s voiced/tests -v
```

Run the command above from the repository root. The contract tests do not open a
microphone or type into the desktop. End-to-end checks should use an isolated
editable test field with a known prefix and suffix, then verify both are preserved
across partial and final transcripts. `VOICED_DESKTOP_BACKEND=x11` confines that
check to the test display and avoids the host virtual keyboard.
