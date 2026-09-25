"""Deterministic hashing and atomic artifact persistence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def canonical_sha256(data: object) -> str:
    """Hash a JSON-compatible value using a stable serialization."""
    serialized = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Hash a file without loading the complete file into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(output_path: str | Path, data: object) -> None:
    """Write JSON through a sibling temporary file before replacing output."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(output_path)

