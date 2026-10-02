"""Deleting downloaded files: those of chosen entries, or everything a folder's manifest knows about.

Only files the manifest tracks (or that carry the exact name Nexus gives a chosen file) are ever deleted: the
rest of the folder is none of this tool's business.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .nexus_api import FileInfo
from .store import Manifest
from .urls import ModRef
from .util import format_size


@dataclass(frozen=True)
class Removal:
    row_id: str  # matches the table row of that file: "<game>:<mod id>#<file id>"
    state: str  # "deleted", "missing" (nothing was on disk) or "failed"
    name: str = ""
    size: int = 0
    reason: str = ""  # why it failed


def _remove(path: Path) -> tuple[int, str]:
    """Delete one file. Returns (bytes freed, "") or (0, the reason it could not be deleted)."""
    try:
        size = path.stat().st_size
        path.unlink()
        return size, ""
    except OSError as exc:
        return 0, exc.strerror or str(exc)


def delete_files(manifest: Manifest, targets: list[tuple[ModRef, FileInfo]]) -> list[Removal]:
    """Delete the downloaded copy of each chosen file and forget it. A copy that cannot be deleted stays recorded."""
    removals: list[Removal] = []
    changed = False
    for ref, file in targets:
        row_id = f"{ref.key}#{file.file_id}"
        path = manifest.locate(ref, file)
        if path is None:
            removals.append(Removal(row_id, "missing"))
        else:
            freed, reason = _remove(path)
            if reason:
                removals.append(Removal(row_id, "failed", path.name, 0, reason))
                continue
            removals.append(Removal(row_id, "deleted", path.name, freed))
        changed |= manifest.drop(ref.key, file.file_id)
    if changed:
        manifest.save()
    return removals


def delete_everything(manifest: Manifest) -> list[Removal]:
    """Delete every file the manifest tracks and empty it. Files that cannot be deleted stay recorded."""
    removals: list[Removal] = []
    for item in manifest.tracked():
        mod_key, _, file_id = item.row_id.partition("#")
        if item.path is None:  # an entry that does not name a plain file: report it, never touch anything
            removals.append(Removal(item.row_id, "failed", item.name, 0, "not a plain file name"))
        elif not item.path.is_file():
            removals.append(Removal(item.row_id, "missing", item.name))
        else:
            freed, reason = _remove(item.path)
            if reason:
                removals.append(Removal(item.row_id, "failed", item.name, 0, reason))
                continue
            removals.append(Removal(item.row_id, "deleted", item.name, freed))
        manifest.drop(mod_key, file_id)
    manifest.save()
    return removals


def summarize(removals: list[Removal]) -> str:
    deleted = [r for r in removals if r.state == "deleted"]
    parts = [f"Deleted {len(deleted)} file{'s' if len(deleted) != 1 else ''}"
             + (f" ({format_size(sum(r.size for r in deleted))})" if deleted else "")]
    failed = sum(r.state == "failed" for r in removals)
    missing = sum(r.state == "missing" for r in removals)
    if missing:
        parts.append(f"{missing} had no file on disk")
    if failed:
        parts.append(f"{failed} could not be deleted")
    return ", ".join(parts) + "."
