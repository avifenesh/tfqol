# tfqol — tools for quality of life

Small, focused Linux utilities I built for myself. Each tool is self-contained
under its own subdirectory with its own `README.md`, `pyproject.toml`, and
runtime.

## Contents

| tool | what |
|---|---|
| [`voiced/`](./voiced) | Push-to-talk voice dictation. Hold RightAlt → speak → stop → transcription lands in the focused window. CPU Whisper, no wake word, no always-on mic. |

## Design principles

- **Zero cost at idle.** If a tool isn't being used, it should not be holding
  the microphone, consuming GPU, or spinning. `voiced` opens the mic only
  during an active session so Bluetooth headphones stay in A2DP for music and
  calls.
- **Deterministic triggers.** Physical keys over wake words. ML is a great
  tool, not a good switch.
- **No surprise actions.** A dictation tool types; it doesn't press Enter. A
  focus switcher focuses; it doesn't type. User presses the commit button.
- **Small, readable code.** Each tool under ~500 lines. Uninstall = `rm -rf`.
