"""Capture a browser download through the DevTools protocol and save it under its real file name.

The browser is told to write downloads into the chosen folder under a temporary name (their GUID) and to
report progress events, so we know exactly when a download starts, how far it is and when it is complete.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .browser import BrowserSession


class DownloadFailed(Exception):
    """The browser never started the download, it stalled, or the file is incomplete."""


@dataclass
class Capture:
    guid: str
    url: str
    suggested_name: str
    received: int = 0
    total: int = 0
    state: str = "inProgress"  # inProgress | completed | canceled


def unique_path(directory: Path, name: str) -> Path:
    """`name`, or `name (1)`, `name (2)` ... if something with that name already exists."""
    path = directory / name
    stem, suffix = path.stem, path.suffix
    counter = 1
    while path.exists():
        path = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return path


class DownloadWatcher:
    """Turn the browser's download events into something a click-through can wait on."""

    def __init__(self, session: BrowserSession, directory: Path):
        self.session = session
        self.directory = directory
        self.captures: dict[str, Capture] = {}
        self._cdp = session.browser.new_browser_cdp_session()
        self._cdp.on("Browser.downloadWillBegin", self._on_begin)
        self._cdp.on("Browser.downloadProgress", self._on_progress)

    def arm(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._cdp.send(
            "Browser.setDownloadBehavior",
            {"behavior": "allowAndName", "downloadPath": str(self.directory), "eventsEnabled": True},
        )

    def close(self) -> None:
        try:
            self._cdp.send("Browser.setDownloadBehavior", {"behavior": "default"})
            self._cdp.detach()
        except Exception:
            pass

    def _on_begin(self, params: dict) -> None:
        self.captures[params["guid"]] = Capture(
            guid=params["guid"], url=params.get("url", ""), suggested_name=params.get("suggestedFilename", "")
        )

    def _on_progress(self, params: dict) -> None:
        capture = self.captures.get(params["guid"])
        if capture is None:  # progress for a download that began before we were listening
            return
        capture.received = int(params.get("receivedBytes", 0))
        capture.total = int(params.get("totalBytes", 0))
        capture.state = params.get("state", capture.state)

    def known(self) -> set[str]:
        return set(self.captures)

    def wait_for_new(self, known: set[str], timeout: float) -> Capture:
        """Wait until a download that was not in `known` begins."""
        waited = 0.0
        while waited < timeout:
            for guid, capture in self.captures.items():
                if guid not in known:
                    return capture
            self.session.sleep(0.25)
            waited += 0.25
        raise DownloadFailed(f"the download did not start within {timeout:.0f} s")

    def wait_until_done(
        self, capture: Capture, stall_timeout: float, on_progress: Callable[[int, int], None] = lambda *_: None
    ) -> Path:
        """Wait for the file to finish, give up if no bytes arrive for `stall_timeout` seconds."""
        last_bytes, idle = -1, 0.0
        try:
            while capture.state == "inProgress":
                self.session.sleep(0.25)
                if capture.received != last_bytes:
                    last_bytes, idle = capture.received, 0.0
                    on_progress(capture.received, capture.total)
                else:
                    idle += 0.25
                    if idle >= stall_timeout:
                        raise DownloadFailed(f"no data received for {stall_timeout:.0f} s")
        except BaseException:
            # Stalled, or the user pressed Stop: never leave the browser downloading in the background.
            if capture.state == "inProgress":
                self.cancel(capture)
            raise
        on_progress(capture.received, capture.total)
        partial = self.directory / capture.guid
        if capture.state != "completed" or not partial.is_file():
            self.discard(capture)
            raise DownloadFailed("the browser cancelled or failed the download")
        if capture.total and partial.stat().st_size != capture.total:
            self.discard(capture)
            raise DownloadFailed("the downloaded file is incomplete")
        return partial

    def save(self, capture: Capture, partial: Path) -> Path:
        """Give the finished download its real name."""
        target = unique_path(self.directory, capture.suggested_name or capture.guid)
        os.replace(partial, target)
        return target

    def cancel(self, capture: Capture) -> None:
        try:
            self._cdp.send("Browser.cancelDownload", {"guid": capture.guid})
        except Exception:
            pass
        self.discard(capture)

    def discard(self, capture: Capture) -> None:
        for name in (capture.guid, capture.guid + ".crdownload"):
            try:
                (self.directory / name).unlink()
            except OSError:
                pass
