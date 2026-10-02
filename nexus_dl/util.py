"""Small helpers shared by several modules."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path


def write_json(path: Path, data) -> None:
    """Write atomically so a crash never leaves a half-written file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def format_size(size: int | None) -> str:
    """12345678 -> '11.8 MB'; unknown or zero sizes give an empty string."""
    if not size:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _home_pattern() -> re.Pattern | None:
    parts = [part for part in re.split(r"[\\/]", str(Path.home())) if part]
    if not parts:
        return None
    return re.compile(r"[\\/]".join(re.escape(part) for part in parts), re.IGNORECASE)


def hide_home(text: str) -> str:
    """Replace the user's home folder by ~ so logs can be shared without revealing the Windows user name."""
    pattern = _home_pattern()
    return pattern.sub("~", text) if pattern else text
