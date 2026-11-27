"""The one way to hash a file's contents: sha256 hexdigest, read in chunks."""

import hashlib
from pathlib import Path


def sha256_file(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()
