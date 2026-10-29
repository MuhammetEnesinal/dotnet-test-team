"""SHA-256 helpers (lower-case hex)."""
from __future__ import annotations

import hashlib
import os

from .paths import open_regular_read


def file_sha256(path):
    """Hash of the file bytes. Refuses a symlink as the last component where the OS supports O_NOFOLLOW."""
    fd = open_regular_read(path)
    try:
        h = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
        return h.hexdigest()
    finally:
        os.close(fd)


