"""Two decoding passes share one guarded draft, never the whole target field."""
from dataclasses import dataclass

from .router import RouterError
import re


@dataclass
class Result:
    text: str = ""
    corrected: bool = False
    blocked: str = ""
    previews: int = 0


def dictate(updates, stt, draft, on_state=lambda state: None, on_text=lambda text: None,
            on_blocked=lambda: None):
    result = Result()
    for update in updates:
        on_state("correcting" if update.final else "listening")
        text = stt.transcribe(update.pcm, final=update.final)
        if not text:
            continue  # an empty final pass must never erase a visible draft
        result.text = text
        on_text(text)
        if update.final:
            result.corrected = True
        else:
            result.previews += 1
        candidate = text
        if not getattr(draft, "corrections_available", True):
            # Keep the uncertain final words out of fields that cannot verify a
            # replacement. Revisions are saved for recovery, never backspaced.
            if not update.final:
                words = list(re.finditer(r"\S+", text))
                candidate = text[:words[-2].start()] if len(words) > 2 else ""
            if not candidate.startswith(draft.typed_text):
                if update.final:
                    result.blocked = "This field cannot verify the final correction."
                continue
            if candidate == draft.typed_text:
                continue
        try:
            draft.update(candidate)
        except RouterError as exc:
            if not result.blocked:
                on_blocked()
            result.blocked = str(exc)
    if result.text and not result.blocked:
        try:
            draft.finish()
        except RouterError as exc:
            result.blocked = str(exc)
            on_blocked()
    return result
