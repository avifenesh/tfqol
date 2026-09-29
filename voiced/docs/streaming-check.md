# Streaming dictation check

Local verification on 29 September 2026.

The decoder was faster-whisper 1.2.1 / CTranslate2 4.7.1 with the cached
`distil-small.en` artifact, CPU int8, two threads, and a systemd 200% CPU quota.
The host CPU was Intel Core Ultra 9 275HX. The input was an 8.27-second English
sentence generated with espeak-ng at 150 words/minute, followed by silence. This
was synthetic replay, not a microphone or human-speech quality measurement.

The replay fed 30 ms PCM frames at real-time cadence into the production capture
callback and used the real WebRTC VAD, streaming pipeline, and Whisper decoder.
The output was a GTK entry inside an isolated X11 desktop. Its pre-existing text
was `Keep this:  [existing tail]`, with the insertion point between the spaces.

| Event | Elapsed seconds |
|---|---:|
| Capture callback opened | 0.080 |
| First draft decoded | 2.882 |
| Second draft decoded | 4.660 |
| Third draft decoded | 6.498 |
| Fourth draft decoded | 8.377 |
| Fifth draft decoded | 10.820 |
| Capture callback closed after the pause | 13.151 |
| Final full-utterance pass decoded | 14.853 |

The first draft was `We need to update the`. A later draft ended in `send the
extract.`. The final text was:

> We need to update the screenshot tool. Please keep the original image and send the extracted text to the coding session.

The GTK field contained that final text between the original prefix and suffix.
Both were checked byte-for-byte. A separate staged replay changed `the rigid`
to `the original image` while preserving the surrounding text. Moving to another
field with Tab caused a draft update to refuse input and preserved the original
text.

Contract tests cover shorter pauses, isolated VAD noise during the finish period,
resumed speech at the deadline, slow decoders, bounded shutdown, physical-input
interruption, selection/caret changes, and no blind deletion in fields without
editable accessibility support.

Current limits: the English-only model is unchanged. This does not establish
recognition quality on the owner's voice or microphone. Automatic correction is
verified in the GTK field; applications that do not expose verifiable editable
text receive extending drafts and retain final corrections for manual copy.
