"""Minimal text normalization (kept for future expansion)."""
from __future__ import annotations

import re
import string


def normalize(text: str) -> str:
    t = text.lower().strip()
    t = t.strip(string.punctuation + " ")
    t = re.sub(r"\s+", " ", t)
    return t
