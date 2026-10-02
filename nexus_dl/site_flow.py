"""Click-through of Nexus's download page. All knowledge of the site's markup lives in this file."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

from playwright.sync_api import Locator
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .browser import BrowserSession, login_url
from .downloads import DownloadFailed, DownloadWatcher
from .nexus_api import FileInfo
from .store import APP_DIR
from .urls import ModRef

# A mod page to look at the header on (the site's home page uses a different header without the profile link).
DEFAULT_CHECK_URL = "https://www.nexusmods.com/cyberpunk2077/mods/107"

# -- the site's markup: the only things that need touching when Nexus changes its pages ----------------
FILE_COMPONENT = "mod-file-download"  # <mod-file-download> on ?tab=files&file_id=<id>, open shadow DOM
COOKIE_DECLINE = "#CybotCookiebotDialogBodyButtonDecline"  # consent banner, "Deny"
PROFILE_LINK = "a[href^='/users/']:has-text('My profile')"  # header menu; /users/0 when nobody is logged in
CHALLENGE = "iframe[src*='challenges.cloudflare.com'], #challenge-running, #challenge-form"
SLOW_DOWNLOAD = re.compile(r"slow download", re.IGNORECASE)  # the free path (5 s countdown, throttled)
FAST_DOWNLOAD = re.compile(r"fast download", re.IGNORECASE)  # premium only: for free users it is an upsell link

DIAGNOSTICS_DIR = APP_DIR / "diagnostics"


class FlowError(Exception):
    """Base class for problems with the website itself."""


class LoginRequired(FlowError):
    """Nobody is logged in to Nexus in the browser."""


class PageUnavailable(FlowError):
    """The mod or file page is gone, hidden, or not shaped the way we expect."""


def dismiss_cookie_banner(page) -> None:
    banner = page.locator(COOKIE_DECLINE).first
    try:
        if banner.count() and banner.is_visible():
            banner.click(timeout=5_000)
    except PlaywrightTimeout:
        pass


def challenge_visible(page) -> bool:
    return page.title().lower().startswith("just a moment") or page.locator(CHALLENGE).count() > 0


def member_id(page) -> int:
    """Nexus member id of whoever is logged in; 0 when nobody is."""
    try:
        href = page.locator(PROFILE_LINK).first.get_attribute("href", timeout=10_000) or ""
    except PlaywrightTimeout:
        return 0
    match = re.fullmatch(r"(?:https://www\.nexusmods\.com)?/users/(\d+)", href)
    return int(match[1]) if match else 0


class SiteFlow:
    def __init__(
        self,
        session: BrowserSession,
        watcher: DownloadWatcher | None = None,  # only needed by download(); login checks work without
        *,
        check_url: str = DEFAULT_CHECK_URL,
        start_timeout: float = 90,
        stall_timeout: float = 60,
        assist_timeout: float = 180,
        log: Callable[[str], None] = print,
        on_status: Callable[[str], None] = lambda _s: None,
        on_progress: Callable[[int, int], None] = lambda *_: None,
    ):
        self.session = session
        self.watcher = watcher
        self.check_url = check_url
        self.start_timeout = start_timeout
        self.stall_timeout = stall_timeout
        self.assist_timeout = assist_timeout
        self.log = log
        self.on_status = on_status
        self.on_progress = on_progress

    @property
    def page(self):
        return self.session.page

    # -- login ---------------------------------------------------------------------------------

    def open(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        dismiss_cookie_banner(self.page)

    def current_member(self) -> int:
        """Load a mod page and report who is logged in (0: nobody)."""
        self.open(self.check_url)
        self._wait_out_challenge()
        return member_id(self.page)

    def wait_for_login(self, timeout: float = 900) -> int:
        """Show the login page in our tab and wait for the user to finish logging in."""
        self.log("Please log in to Nexus Mods in the browser window (this tool's tab was brought to the front).")
        self.open(login_url(self.check_url))
        self.page.bring_to_front()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.session.sleep(2)
            if "users.nexusmods.com" in self.page.url:
                continue  # still on the login pages
            dismiss_cookie_banner(self.page)
            member = member_id(self.page)
            if member:
                return member
            self.open(self.check_url)  # landed somewhere without the usual header; look again
        raise LoginRequired("login was not completed in time")

    def _wait_out_challenge(self, timeout: float = 300) -> None:
        """A Cloudflare check needs a human: tell them once, then wait until it is gone."""
        deadline = time.monotonic() + timeout
        warned = False
        while challenge_visible(self.page):
            if not warned:
                self.log("Nexus is showing a verification check - please complete it in the browser window.")
                self.page.bring_to_front()
                warned = True
            if time.monotonic() > deadline:
                raise PageUnavailable("the verification check was not completed")
            self.session.sleep(2)

    # -- downloading ---------------------------------------------------------------------------

    def download(self, ref: ModRef, file: FileInfo) -> Path:
        """Click through the download page for one file and return where it was saved."""
        assert self.watcher is not None, "download() needs a DownloadWatcher"
        self.watcher.arm()
        self.open(ref.files_url(file.file_id))
        self._wait_out_challenge()
        if not member_id(self.page):
            raise LoginRequired("not logged in")
        component = self._file_component(file)
        button = self._download_button(component)
        known = self.watcher.known()
        if button is None:
            self.log("Could not find the download button - click it yourself in the browser window.")
            self.on_status("waiting for you to click download")
            capture = self.watcher.wait_for_new(known, self.assist_timeout)
        else:
            self._click(button)
            self.on_status("countdown")
            try:
                capture = self.watcher.wait_for_new(known, self.start_timeout)
            except DownloadFailed:
                self._snapshot(ref, file)
                raise
        self.on_status("downloading")
        partial = self.watcher.wait_until_done(capture, self.stall_timeout, self.on_progress)
        return self.watcher.save(capture, partial)

    def _file_component(self, file: FileInfo) -> Locator:
        component = self.page.locator(FILE_COMPONENT).first
        try:
            component.wait_for(state="attached", timeout=30_000)
        except PlaywrightTimeout:
            raise PageUnavailable("the download page did not load (mod hidden or file removed?)") from None
        shown = component.get_attribute("file-id")
        if shown not in (None, str(file.file_id)):
            raise PageUnavailable(f"the page shows file {shown}, expected {file.file_id}")
        return component

    def _download_button(self, component: Locator) -> Locator | None:
        """Fast download only exists for Premium; for free accounts it is an upsell link, so use Slow."""
        premium = component.get_attribute("user-is-premium") == "true"
        label = FAST_DOWNLOAD if premium else SLOW_DOWNLOAD
        button = component.locator("a, button").filter(has_text=label).first
        try:
            button.wait_for(state="visible", timeout=20_000)
        except PlaywrightTimeout:
            return None
        return button

    def _click(self, button: Locator) -> None:
        try:
            button.click(timeout=15_000)
        except PlaywrightTimeout:
            button.click(timeout=15_000, force=True)  # something (an ad, a banner) is overlapping it

    def _snapshot(self, ref: ModRef, file: FileInfo) -> None:
        """Keep a screenshot of the page when a download did not start, to see what the site showed."""
        try:
            DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
            path = DIAGNOSTICS_DIR / f"{ref.domain}-{ref.mod_id}-{file.file_id}.png"
            self.page.screenshot(path=str(path))
            self.log(f"Saved a screenshot of the page: {path}")
        except Exception:
            pass
