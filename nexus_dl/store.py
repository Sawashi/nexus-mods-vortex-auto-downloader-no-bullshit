"""Settings, the per-folder download manifest and the human-readable requirements report."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

from .nexus_api import FileInfo
from .resolver import Plan
from .urls import ModRef
from .util import write_json

APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "NexusAutoDownloader"
CONFIG_PATH = APP_DIR / "config.json"
PROFILE_DIR = APP_DIR / "browser-profile"
USAGE_PATH = APP_DIR / "request_usage.json"


# -- settings --------------------------------------------------------------------------------


@dataclass
class Config:
    """Everything the tool remembers between runs. No links, no passwords, no keys."""

    download_dir: str = ""
    delay_seconds: int = 5  # pause between two downloads
    start_timeout_seconds: int = 90  # how long a click may take to produce a download (covers the countdown)
    stall_seconds: int = 60  # give up on a transfer that receives no data for this long
    include_requirements: bool = True
    skip_existing: bool = True
    skip_optional: bool = False  # leave out the optional mods of collections
    scan_only: bool = False
    confirm: bool = True
    all_main: bool = False
    browser_path: str = ""  # empty: auto-detect Brave, Chrome or Edge
    debug_port: int = 9222
    max_depth: int = 8
    max_mods: int = 60  # how many mods may be pulled in through requirements (not counting the ones asked for)
    request_limit_hour: int = 450  # Nexus allows 500 an hour: stop at 90 %
    request_limit_day: int = 18_000  # Nexus allows 20,000 a day: stop at 90 %


def load_config(path: Path = CONFIG_PATH) -> Config:
    config = Config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return config
    if not isinstance(raw, dict):
        return config
    for field_ in fields(Config):  # unknown keys (from older versions) are ignored, and dropped on the next save
        value = raw.get(field_.name)
        default = getattr(config, field_.name)
        if value is not None and type(value) is type(default):
            setattr(config, field_.name, value)
    return config


def save_config(config: Config, path: Path = CONFIG_PATH) -> None:
    write_json(path, asdict(config))


# -- manifest --------------------------------------------------------------------------------


class Manifest:
    """What has already been downloaded into a folder, so reruns skip it."""

    FILENAME = "nexus_manifest.json"

    def __init__(self, directory: Path | str):
        self.directory = Path(directory)
        self.path = self.directory / self.FILENAME
        self.mods: dict[str, dict] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        mods = raw.get("mods") if isinstance(raw, dict) else None
        if isinstance(mods, dict):
            self.mods = mods

    def entry(self, ref: ModRef, file_id: int) -> dict | None:
        return self.mods.get(ref.key, {}).get("files", {}).get(str(file_id))

    def is_done(self, ref: ModRef, file: FileInfo) -> bool:
        entry = self.entry(ref, file.file_id)
        if not entry:
            return False
        path = self.directory / entry.get("fileName", "")
        return path.is_file() and path.stat().st_size == entry.get("size")

    def mark_done(
        self, ref: ModRef, mod_name: str, file: FileInfo, file_name: str, size: int, required_by: list[ModRef]
    ) -> None:
        mod = self.mods.setdefault(ref.key, {"name": mod_name, "files": {}})
        mod["name"] = mod_name
        mod["files"][str(file.file_id)] = {
            "fileName": file_name,
            "fileLabel": file.name,
            "version": file.version,
            "size": size,
            "downloadedAt": datetime.now().isoformat(timespec="seconds"),
            "requiredBy": sorted(r.key for r in required_by),
        }
        self.save()

    def save(self) -> None:
        write_json(self.path, {"version": 1, "mods": self.mods})


# -- report ----------------------------------------------------------------------------------

REPORT_NAME = "requirements_report.md"


def _tree_lines(plan: Plan) -> list[str]:
    by_key = {item.ref.key: item for item in plan.items}
    lines: list[str] = []
    seen: set[str] = set()

    def walk(ref: ModRef, depth: int) -> None:
        item = by_key.get(ref.key)
        indent = "  " * depth
        if item is None:
            lines.append(f"{indent}- {ref.key} (not available)")
            return
        repeated = ref.key in seen
        lines.append(f"{indent}- {item.name}" + (" (listed above)" if repeated and item.requires else ""))
        if repeated:
            return
        seen.add(ref.key)
        for child in item.requires:
            walk(child, depth + 1)

    for collection in plan.collections:
        lines.append(f"- Collection: {collection.name} (revision {collection.revision})")
        for ref in collection.mods:
            walk(ref, 1)
    for root in plan.roots:
        walk(root, 0)
    return lines


def render_report(plan: Plan, results: dict[str, str]) -> str:
    """`results` maps a mod key to a short outcome such as 'downloaded' or 'failed: ...'."""
    out = ["# Requirements report", "", f"Generated {datetime.now():%Y-%m-%d %H:%M}", ""]
    if plan.collections:
        out += ["## Collections", ""]
        for collection in plan.collections:
            size = f", {collection.size_bytes / 1_048_576:.0f} MB" if collection.size_bytes else ""
            skipped = f", {collection.skipped_optional} optional skipped" if collection.skipped_optional else ""
            out.append(
                f"- [{collection.name}]({collection.url}) - revision {collection.revision}, "
                f"{len(collection.mods)} mods{size}{skipped}"
            )
        out.append("")
    out += ["## Install order (requirements first)", ""]
    for number, item in enumerate(plan.items, 1):
        files = ", ".join(f"{f.name} v{f.version}" for f in item.files)
        outcome = results.get(item.ref.key, "not downloaded")
        optional = " (optional)" if item.optional else ""
        out.append(f"{number}. [{item.name}]({item.ref.url}) - {files}{optional} - {outcome}")
    out += ["", "## Dependency tree", ""] + _tree_lines(plan)
    if plan.externals:
        out += ["", "## Off-site requirements (download these yourself)", ""]
        for ext in plan.externals:
            link = f"[{ext.name or ext.url}]({ext.url})" if ext.url else ext.name
            note = f" - {ext.notes}" if ext.notes else ""
            out.append(f"- {link} (needed by {ext.owner_name}){note}")
    if plan.dlc:
        out += ["", "## Game expansions required", ""]
        out += [f"- {expansion} (needed by {name})" for name, expansion in plan.dlc]
    if plan.unavailable:
        out += ["", "## Not available", ""]
        for problem in plan.unavailable:
            needed = f", required by {problem.required_by.key}" if problem.required_by else ""
            out.append(f"- {problem.ref.url} - {problem.reason}{needed}")
    if plan.notes:
        out += ["", "## Notes", ""] + [f"- {note}" for note in plan.notes]
    return "\n".join(out) + "\n"


def write_report(directory: Path | str, plan: Plan, results: dict[str, str]) -> Path:
    path = Path(directory) / REPORT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(plan, results), encoding="utf-8")
    return path
