# Chromium input repair

Verified on 29 September 2026.

The previous dictation version could fall back to character-by-character virtual
keyboard input when Codex exposed no accessible field. That path could invoke
application shortcuts and could not revise text. It has been removed.

Chromium rich editors expose text and selections but not AT-SPI EditableText.
Their outer textbox may contain an embedded-object marker instead of the actual
paragraph text. The repair resolves the paragraph at the caret, verifies the
exact field and draft range, selects only the changed suffix, and sends literal
text through one Paste chord. It reads the resulting text and caret back before
continuing. No dictated characters are interpreted as key commands.

## Live checks

A separate Chromium instance ran in an isolated X11 desktop with accessibility
enabled. The fixture contained a rich editor with a paragraph and nested span,
pre-existing text before and after the caret, input-event instrumentation, and
an unexpected-hotkey counter.

Four updates covered a recognition correction, literal backslash sequences,
changed punctuation, and shortening an existing draft. The final editor text was
`Keep this: The original image. [existing tail]`. Both surrounding strings were
preserved. The browser recorded four input events and zero unexpected hotkeys.
Each update took approximately 0.41 to 0.43 seconds in that fixture.

The clipboard contained text, HTML, and a binary MIME format. Every original
advertised format and byte was restored after the successful edits. Testing found
and fixed two Gdk edge cases: formats arrive asynchronously, and serialization
helpers may advertise a format the remote clipboard does not actually offer.
The helper now waits for real metadata and verifies the X selection owner.

An empty rich editor with a sole `<br>` placeholder also passed initial insertion
and revision from `Hello word` to `Hello world.`. That placeholder is distinguished
from a blank paragraph inside a nonempty editor.

The automatic finish setting is now 2000 ms. The audio contract test stays open
at 1.98 seconds and closes at 2.01 seconds of quiet. The final speech recognition
pass remains unchanged: a bounded synthetic comparison did not establish a
meaningful speed improvement from reducing the beam width.

## Codex activation

The running Codex instance on this machine was launched without renderer
accessibility and exposes no AT-SPI editor. `voicectl enable-codex-input` prepares
a user desktop-entry override with `--force-renderer-accessibility`. It requires
a full quit and relaunch from the application menu. No current Codex conversation
was edited or submitted by these checks. Live verification in Codex itself still
requires that relaunch; the verified editor above is Chromium, not the running
Codex application.
