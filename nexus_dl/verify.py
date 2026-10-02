"""Is a downloaded archive complete and readable?

Existing files get cheap checks (so a rerun stays fast); a fresh download gets a thorough one.
"""
from __future__ import annotations

import zipfile
import zlib
from pathlib import Path

_SIGNATURES = {"7z": b"7z\xbc\xaf\x27\x1c", "rar": b"Rar!\x1a\x07"}


def _other_archive(head: bytes) -> bool:
    return any(head.startswith(signature) for signature in _SIGNATURES.values())


def check_file(path: Path, expected_size: int | None = None, *, deep: bool = False) -> str | None:
    """None if the file looks complete and readable, otherwise a short reason why it is corrupted.

    Always: it is not empty, it has the expected size, and its archive structure is sound (a zip's directory can
    be read, a .7z/.rar starts with its signature). `deep` also reads a whole zip and checks every CRC.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError as exc:
        return f"it cannot be read ({exc.strerror or exc})"
    if size == 0:
        return "the file is empty"
    if expected_size is not None and size != expected_size:
        return f"it has {size:,} bytes but should have {expected_size:,}"
    suffix = path.suffix.lower()
    if suffix == ".zip":
        # An author may have saved a .rar or .7z under a .zip name: that is not corruption.
        return None if _other_archive(head) else _check_zip(path, deep)
    kind = suffix.lstrip(".")
    if kind in _SIGNATURES and not head.startswith(_SIGNATURES[kind]):
        return f"it is not a valid .{kind} archive"
    return None


def _check_zip(path: Path, deep: bool) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            encrypted = any(info.flag_bits & 0x1 for info in archive.infolist())  # its CRCs cannot be tested
            if deep and not encrypted:
                damaged = archive.testzip()
                if damaged is not None:
                    return f"'{damaged}' inside the zip is damaged"
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        return f"the zip is damaged ({exc})"
    except (NotImplementedError, RuntimeError):  # a compression method Python cannot read: no verdict possible
        return None
    except OSError as exc:
        return f"it cannot be read ({exc.strerror or exc})"
    return None
