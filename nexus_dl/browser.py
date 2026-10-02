"""Start (or reuse) the user's own Chrome/Edge window and drive it over the DevTools protocol.

The tool never types credentials: the user logs in by hand in that window and the session lives in a
dedicated browser profile, so it is still there next time.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlparse

from .budget import RequestBudget
from .errors import Cancelled
from .store import PROFILE_DIR

# The kinds of browser request that count against Nexus's limits: the page itself and its API-style calls
# (a typical file page makes about 9 of these; images, scripts and styles are not counted).
COUNTED_REQUEST_TYPES = {"document", "xhr", "fetch"}

LOGIN_URL = "https://users.nexusmods.com/auth/sign_in"


def login_url(redirect_to: str) -> str:
    """The login page, sending the user back to `redirect_to` once they are in."""
    return f"{LOGIN_URL}?redirect_url={quote(redirect_to, safe='')}"


class BrowserError(Exception):
    """The browser could not be started or reached."""


# In order of preference: (install path under the root, environment variables that may hold the root).
_BROWSERS = (
    ("BraveSoftware/Brave-Browser/Application/brave.exe", ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")),
    ("Google/Chrome/Application/chrome.exe", ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")),
    ("Microsoft/Edge/Application/msedge.exe", ("PROGRAMFILES(X86)", "PROGRAMFILES")),
)


def find_browser(preferred: str = "") -> Path | None:
    """The browser to drive: `preferred` if it exists, else Brave, Chrome or Edge (any Chromium works)."""
    if preferred and Path(preferred).is_file():
        return Path(preferred)
    for relative, variables in _BROWSERS:
        for variable in variables:
            root = os.environ.get(variable)
            if root and (Path(root) / relative).is_file():
                return Path(root) / relative
    for name in ("brave", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def profile_dir_for(exe: Path) -> Path:
    """One profile per browser product; Chromium browsers must not share a profile folder."""
    return PROFILE_DIR / exe.stem.lower()


def endpoint(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def debug_port_open(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"{endpoint(port)}/json/version", timeout=1.5):
            return True
    except OSError:
        return False


def ensure_browser(
    port: int, start_url: str = LOGIN_URL, log: Callable[[str], None] = print, preferred: str = ""
) -> bool:
    """Make sure a browser with the debugging port is running. Returns True if it was started here."""
    if debug_port_open(port):
        return False
    exe = find_browser(preferred)
    if exe is None:
        raise BrowserError("Brave, Chrome or Edge was not found on this computer.")
    profile = profile_dir_for(exe)
    profile.mkdir(parents=True, exist_ok=True)
    log(f"Starting {exe.name} with its own profile ({profile}) ...")
    args = [
        str(exe),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        start_url,
    ]
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    subprocess.Popen(
        args,
        creationflags=flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if debug_port_open(port):
            return True
        time.sleep(0.3)
    raise BrowserError(
        f"The browser did not open its debugging port {port}. If a window using the tool's profile is "
        "already open (started some other way), close it and try again."
    )


def is_nexus_url(url: str) -> bool:
    return (urlparse(url).hostname or "").endswith(("nexusmods.com", "nexus-cdn.com"))


class BrowserSession:
    """Playwright attached to the running browser. Create and use it from one thread only."""

    def __init__(
        self,
        port: int,
        should_stop: Callable[[], bool] = lambda: False,
        budget: RequestBudget | None = None,
    ):
        self.port = port
        self.should_stop = should_stop
        self.budget = budget
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def __enter__(self) -> "BrowserSession":
        from playwright.sync_api import sync_playwright  # imported late: tests and the UI don't need it

        self._playwright = sync_playwright().start()
        try:
            self.browser = self._playwright.chromium.connect_over_cdp(endpoint(self.port))
        except Exception as exc:
            self._playwright.stop()
            raise BrowserError(f"Could not attach to the browser on port {self.port}: {exc}") from exc
        self.context = self.browser.contexts[0]
        self.page = self.context.new_page()  # our own tab: the user's login tab is left alone
        if self.budget is not None:
            self.page.on("request", self._count_request)
        return self

    def _count_request(self, request) -> None:
        if request.resource_type in COUNTED_REQUEST_TYPES and is_nexus_url(request.url):
            self.budget.record(1)

    def __exit__(self, *exc_info) -> None:
        try:
            if self.page is not None and not self.page.is_closed():
                self.page.close()
        except Exception:
            pass
        finally:
            if self._playwright is not None:
                self._playwright.stop()

    def sleep(self, seconds: float) -> None:
        """Stop-aware sleep. Uses Playwright's own timer so protocol events keep being delivered."""
        deadline = time.monotonic() + seconds
        while True:
            if self.should_stop():
                raise Cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.page.wait_for_timeout(min(250, remaining * 1000))
