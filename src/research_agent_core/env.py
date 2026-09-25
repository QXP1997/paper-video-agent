"""Small dependency-free loader for checkout-local environment variables."""

from __future__ import annotations

import os
from pathlib import Path


def load_local_env(source_root: str | Path | None = None) -> None:
    """Load the first checkout-local ``.env`` without overriding the process."""
    checkout_root = (
        Path(source_root).resolve()
        if source_root is not None
        else Path(__file__).resolve().parents[2]
    )
    candidates = [Path.cwd() / ".env", checkout_root / ".env"]
    env_path = next((path for path in candidates if path.is_file()), None)
    if env_path is None:
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)

