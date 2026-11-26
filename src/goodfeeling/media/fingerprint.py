"""Content fingerprints: size plus a hash of 1 MB from the head, middle, and tail."""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK = 1 << 20


def fingerprint(path: str | Path) -> str:
    path = Path(path)
    size = path.stat().st_size
    digest = hashlib.sha256(str(size).encode())
    with path.open("rb") as handle:
        for offset in sorted({0, max(0, size // 2 - CHUNK // 2), max(0, size - CHUNK)}):
            handle.seek(offset)
            digest.update(handle.read(CHUNK))
    return f"v1-{size}-{digest.hexdigest()[:24]}"
