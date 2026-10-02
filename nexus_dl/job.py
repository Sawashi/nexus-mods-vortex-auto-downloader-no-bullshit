"""Background worker: build the plan, confirm it with the user, then download every file."""
from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .browser import LOGIN_URL, BrowserError, BrowserSession, ensure_browser
from .budget import OFFICIAL_DAILY, OFFICIAL_HOURLY, RequestBudget, RequestLimitReached, format_duration
from .downloads import DownloadFailed, DownloadWatcher, remove_leftovers
from .errors import Cancelled
from .nexus_api import FileInfo, NexusApi, NexusApiError
from .resolver import Plan, PlanItem, Resolver
from .site_flow import FlowError, LoginRequired, PageUnavailable, SiteFlow
from .store import Config, Existing, Manifest, write_report
from .urls import Link
from .util import format_size
from .verify import check_file

FREE_SPEED = 1.5 * 1024 * 1024  # bytes per second: what a free account is throttled to
ATTEMPTS = 3
PER_FILE_REQUESTS = 12  # measured: a file costs about 9 counted requests (page load + download); margin added
LARGE_PLAN_FILES = 25

RULES_WARNING = (
    "Nexus's rules prohibit automated downloading at a volume far above normal use. "
    "Downloads are sequential and the countdown is respected."
)
LARGE_COLLECTION_WARNING = (
    "This is a big collection. Nexus offers automatic collection downloads to Premium members; on a free account "
    "this tool has to fetch every file one by one, which is the kind of bulk automation that can get an account "
    "suspended. Consider Premium with Vortex for collections this size."
)


@dataclass
class Row:
    """One line of the status table."""

    id: str
    mod: str
    file: str
    size: int | None
    status: str
    url: str = ""  # set for rows the user has to deal with by hand
    work: "Work | None" = None  # set for rows that are a file: what "Download again" and "Delete" act on


class Ui(Protocol):
    """What the worker needs from the front end. Every method is safe to call from the worker thread."""

    def log(self, text: str) -> None: ...
    def show_plan(self, rows: list[Row]) -> None: ...
    def set_status(self, row_id: str, status: str) -> None: ...
    def set_progress(self, done: int, total: int, text: str) -> None: ...
    def set_file_progress(self, received: int, total: int) -> None: ...
    def set_login(self, logged_in: bool | None, text: str) -> None: ...
    def confirm(self, title: str, text: str) -> bool: ...
    def finished(self, summary: str) -> None: ...


@dataclass
class Work:
    item: PlanItem
    file: FileInfo
    existing: Existing  # what is already in the folder for this file
    skip: bool

    @property
    def row_id(self) -> str:
        return f"{self.item.ref.key}#{self.file.file_id}"

    @property
    def corrupt(self) -> bool:
        return self.existing.state == "corrupt"


def _rank(outcome: str) -> int:
    """How good an outcome is, so a mod with several files shows its worst one in the report."""
    if outcome == "already downloaded":
        return 3
    if outcome.startswith("downloaded"):
        return 2
    if outcome == "not downloaded":
        return 1
    return 0  # failed, unavailable, corrupted ...


class Job:
    """A run: scan the links, confirm, download. With `redo` (entries of an earlier run) it only downloads those
    files again, without scanning, and always replaces the copy that is already there."""

    def __init__(
        self,
        config: Config,
        links: list[Link],
        ui: Ui,
        stop: threading.Event,
        budget: RequestBudget | None = None,
        redo: list[Work] | None = None,
    ):
        self.config = config
        self.links = links
        self.ui = ui
        self.stop = stop
        self.budget = budget
        self.redo = redo
        # A good copy is overwritten by the new one (not saved next to it) when downloading again on purpose.
        self._replace_existing = redo is not None or not config.skip_existing
        self.outcomes: dict[str, str] = {}  # row id -> what happened to that file
        self._work: list[Work] = []
        self._current = ""
        self._waiting = False

    def run(self) -> None:
        summary = ""
        try:
            summary = self._run() if self.redo is None else self._run_again()
        except Cancelled:
            summary = "Stopped."
        except RequestLimitReached as exc:
            summary = f"Paused: {exc}"
        except (NexusApiError, BrowserError, FlowError) as exc:
            summary = f"Error: {exc}"
        except Exception:
            self.ui.log(traceback.format_exc())
            summary = "Unexpected error - see the log above."
        finally:
            if self.budget:
                self.budget.flush()
            self.ui.finished(summary)

    # -- the whole run -------------------------------------------------------------------------

    def _run(self) -> str:
        config, ui = self.config, self.ui
        directory = Path(config.download_dir)
        directory.mkdir(parents=True, exist_ok=True)

        plan = self._scan()
        manifest = Manifest(directory)
        self._work = self._plan_work(plan, manifest)
        work = self._work
        pending = [w for w in work if not w.skip]
        ui.show_plan(self._rows(plan, work))
        self._log_plan(plan, work)

        if config.scan_only or not pending:
            report = write_report(directory, plan, self._results())
            ui.log(f"Report written: {report}")
            return "Scan finished." if config.scan_only else "Everything is already downloaded."
        if config.confirm and not ui.confirm("Start downloading?", self._confirm_text(plan, work, pending)):
            return "Cancelled before downloading."

        try:
            counts = self._download(directory, manifest, pending, len(work) - len(pending))
        finally:  # also when stopped or failed halfway: the report shows what did and did not arrive
            ui.log(f"Report written: {write_report(directory, plan, self._results())}")
        return self._summary(counts, len(work) - len(pending))

    def _run_again(self) -> str:
        """Download the chosen entries again. The table stays as it is and the full report is not rewritten."""
        config, ui = self.config, self.ui
        directory = Path(config.download_dir)
        directory.mkdir(parents=True, exist_ok=True)
        manifest = Manifest(directory)
        self._work = work = [Work(w.item, w.file, manifest.inspect(w.item.ref, w.file), False) for w in self.redo]
        for w in work:
            ui.set_status(w.row_id, self._queued_status(w))
        if len(work) > 1 and config.confirm and not ui.confirm(
            "Download these files again?", self._confirm_text(Plan(roots=[]), work, work)
        ):
            return "Cancelled before downloading."
        return self._summary(self._download(directory, manifest, work, 0), 0)

    @staticmethod
    def _summary(counts: dict[str, int], already_there: int) -> str:
        summary = f"Done: {counts['downloaded']} downloaded, {already_there} already there, {counts['failed']} failed."
        if counts["replaced"]:
            summary += f" {counts['replaced']} corrupted file(s) were replaced."
        return summary

    @staticmethod
    def _queued_status(w: Work) -> str:
        if w.skip:
            return "already downloaded"
        if w.corrupt:
            return "queued (replaces a corrupted file)"
        return "queued (downloads it again)" if w.existing.state == "ok" else "queued"

    def _scan(self) -> Plan:
        config = self.config
        api = NexusApi(sleep=self._sleep, budget=self.budget, on_wait=self._on_budget_wait)
        resolver = Resolver(
            api,
            include_requirements=config.include_requirements,
            all_main=config.all_main,
            skip_optional=config.skip_optional,
            max_depth=config.max_depth,
            max_mods=config.max_mods,
            should_stop=self.stop.is_set,
            log=self.ui.log,
        )
        self.ui.log("Reading mod information from Nexus Mods ...")
        plan = resolver.resolve(self.links)
        if self.budget:
            usage = self.budget.usage()
            self.ui.log(
                f"Request budget used so far: {usage.hour_used} of {usage.hour_limit} this hour, "
                f"{usage.day_used:,} of {usage.day_limit:,} in 24 hours."
            )
        return plan

    def _plan_work(self, plan: Plan, manifest: Manifest) -> list[Work]:
        """Look at what is already in the folder: intact files are skipped, corrupted ones will be replaced."""
        work: list[Work] = []
        for item in plan.items:
            for file in item.files:
                existing = manifest.inspect(item.ref, file)
                skip = self.config.skip_existing and existing.state == "ok"
                if skip and not existing.recorded:  # a valid copy the manifest did not know about: remember it
                    size = existing.path.stat().st_size
                    manifest.mark_done(item.ref, item.name, file, existing.path.name, size, item.required_by)
                    self.ui.log(f"Found {existing.path.name} already in the folder - it will not be downloaded again.")
                if existing.state == "corrupt":
                    self.ui.log(
                        f"Corrupted file found: {existing.path.name} ({existing.reason}). "
                        "It will be deleted and downloaded again."
                    )
                work.append(Work(item, file, existing, skip))
        return work

    def _results(self) -> dict[str, str]:
        """One outcome per mod for the report (its worst file decides)."""
        results: dict[str, str] = {}
        for w in self._work:
            outcome = self.outcomes.get(w.row_id)
            if outcome is None:
                outcome = (
                    "already downloaded" if w.skip
                    else "corrupted file found (not replaced yet)" if w.corrupt
                    else "not downloaded"
                )
            key = w.item.ref.key
            if key not in results or _rank(outcome) < _rank(results[key]):
                results[key] = outcome
        return results

    # -- waiting politely ----------------------------------------------------------------------

    def _sleep(self, seconds: float) -> None:
        """A sleep that notices the Stop button."""
        deadline = time.monotonic() + seconds
        while True:
            if self.stop.is_set():
                raise Cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))

    def _on_budget_wait(self, delay: float, limiting: str) -> None:
        if not self._waiting:
            self._waiting = True
            self.ui.log(
                f"The {limiting} request limit is close, so the tool pauses (about {format_duration(delay)}) "
                "to stay under what Nexus allows."
            )
        if self._current:
            self.ui.set_status(self._current, f"waiting for the {limiting} request limit ({format_duration(delay)})")

    def _wait_budget(self, cost: int, sleep: Callable[[float], None]) -> None:
        if not self.budget:
            return
        self._waiting = False
        self.budget.wait_for(cost, sleep, self._on_budget_wait)
        if self._waiting:
            self.ui.log("Request budget available again - continuing.")

    # -- presenting the plan -------------------------------------------------------------------

    def _rows(self, plan: Plan, work: list[Work]) -> list[Row]:
        rows = [
            Row(w.row_id, w.item.name,
                f"{w.file.name} v{w.file.version}" + (" (optional)" if w.item.optional else ""),
                w.file.size_bytes, self._queued_status(w), work=w)
            for w in work
        ]
        for number, ext in enumerate(plan.externals):
            rows.append(Row(f"ext:{number}", ext.name or ext.url, f"off-site, needed by {ext.owner_name}",
                            None, "download it yourself", ext.url))
        for number, problem in enumerate(plan.unavailable):
            rows.append(Row(f"na:{number}", problem.ref.key, problem.reason, None, "unavailable", problem.ref.url))
        return rows

    def _log_plan(self, plan: Plan, work: list[Work]) -> None:
        log = self.ui.log
        total = sum(w.file.size_bytes or 0 for w in work)
        for collection in plan.collections:
            skipped = f", {collection.skipped_optional} optional skipped" if collection.skipped_optional else ""
            log(f"Collection '{collection.name}' (revision {collection.revision}): "
                f"{len(collection.mods)} mod(s){skipped}.")
        log(f"Plan: {len(plan.items)} mod(s), {len(work)} file(s), {format_size(total) or 'size unknown'}.")
        for number, w in enumerate(work, 1):
            state = "  (already downloaded)" if w.skip else "  (corrupted copy will be replaced)" if w.corrupt else ""
            log(f"  {number}. {w.item.name} - {w.file.name} v{w.file.version} {format_size(w.file.size_bytes)}{state}")
        for ext in plan.externals:
            log(f"  Off-site requirement of {ext.owner_name}: {ext.name} {ext.url} - get it yourself")
        for problem in plan.unavailable:
            log(f"  Not available: {problem.ref.url} ({problem.reason})")
        for dlc_owner, expansion in plan.dlc:
            log(f"  {dlc_owner} needs the game expansion: {expansion}")
        for note in plan.notes:
            log(f"  Note: {note}")

    def _confirm_text(self, plan: Plan, work: list[Work], pending: list[Work]) -> str:
        size = sum(w.file.size_bytes or 0 for w in pending)
        minutes = size / FREE_SPEED / 60
        eta = f" - roughly {minutes:.0f} min at free-account speed" if minutes >= 1 else ""
        lines = [f"{len(pending)} file(s) to download: {format_size(size) or 'size unknown'}{eta}."]
        if len(work) > len(pending):
            lines.append(f"{len(work) - len(pending)} file(s) are already downloaded and will be skipped.")
        corrupt = sum(1 for w in pending if w.corrupt)
        if corrupt:
            lines.append(f"{corrupt} corrupted file(s) in the folder will be deleted and downloaded again.")
        if plan.externals:
            lines.append(f"{len(plan.externals)} off-site requirement(s) you have to get yourself (see the table).")
        if plan.unavailable:
            lines.append(f"{len(plan.unavailable)} item(s) are unavailable (see the table).")
        lines.append(self._request_estimate(len(pending) * PER_FILE_REQUESTS))
        if plan.collections and len(pending) >= LARGE_PLAN_FILES:
            lines.append("\n" + LARGE_COLLECTION_WARNING)
        lines.append("\n" + RULES_WARNING)
        return "\n".join(lines)

    def _request_estimate(self, needed: int) -> str:
        text = (f"About {needed} requests to Nexus (it allows {OFFICIAL_HOURLY} an hour and {OFFICIAL_DAILY:,} a day; "
                "this tool pauses at 90 % of that).")
        if not self.budget:
            return text
        usage = self.budget.usage()
        left = max(0, usage.hour_limit - usage.hour_used)
        if needed > left:
            hours = (needed - left) / usage.hour_limit
            text += f" It will have to pause for the hourly limit: expect at least {format_duration(hours * 3600)}."
        return text

    # -- downloading ---------------------------------------------------------------------------

    def _download(self, directory: Path, manifest: Manifest, pending: list[Work], skipped: int) -> dict[str, int]:
        config, ui = self.config, self.ui
        counts = {"downloaded": 0, "failed": 0, "replaced": 0}
        for name in remove_leftovers(directory):
            ui.log(f"Removed an unfinished download left over from an earlier run: {name}")
        ensure_browser(config.debug_port, LOGIN_URL, ui.log, config.browser_path)
        with BrowserSession(config.debug_port, self.stop.is_set, self.budget) as session:
            watcher = DownloadWatcher(session, directory)
            flow = SiteFlow(
                session,
                watcher,
                check_url=pending[0].item.ref.url,  # a mod page: its header shows who is logged in
                start_timeout=config.start_timeout_seconds,
                stall_timeout=config.stall_seconds,
                log=ui.log,
                on_status=lambda text: ui.set_status(self._current, text),
                on_progress=self._on_file_progress,
            )
            try:
                self._ensure_login(flow)
                for number, w in enumerate(pending, 1):
                    self._current = w.row_id
                    ui.set_progress(skipped + number - 1, skipped + len(pending), f"{w.item.name}")
                    self._wait_budget(PER_FILE_REQUESTS, session.sleep)
                    self._clear_corrupt(manifest, w)
                    outcome = self._fetch(flow, manifest, w)
                    self.outcomes[w.row_id] = outcome
                    ui.set_status(w.row_id, outcome)
                    ui.log(f"{w.item.name}: {outcome}")
                    if outcome.startswith("downloaded"):
                        counts["downloaded"] += 1
                        counts["replaced"] += w.corrupt
                    else:
                        counts["failed"] += 1
                    if number < len(pending):
                        session.sleep(config.delay_seconds)
                ui.set_progress(skipped + len(pending), skipped + len(pending), "finished")
            finally:
                watcher.close()
        return counts

    def _on_file_progress(self, received: int, total: int) -> None:
        self.ui.set_file_progress(received, total)
        if total:
            self.ui.set_status(self._current, f"downloading {received * 100 // total}%")

    def _ensure_login(self, flow: SiteFlow) -> None:
        if not flow.current_member():
            self.ui.set_login(False, "Not logged in - waiting for you to log in in the browser window ...")
            flow.wait_for_login()
        self.ui.set_login(True, "Logged in")

    def _clear_corrupt(self, manifest: Manifest, w: Work) -> None:
        """Delete the corrupted copy of a file right before it is downloaded again."""
        if not w.corrupt:
            return
        path = w.existing.path
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            self.ui.log(f"Could not delete the corrupted file {path.name}: {exc.strerror or exc}. "
                        "The new copy will be saved next to it.")
            return
        manifest.forget(w.item.ref, w.file.file_id)
        self.outcomes[w.row_id] = "corrupted file deleted, the new download did not finish"  # until it does
        self.ui.log(f"Deleted the corrupted file {path.name} ({w.existing.reason}); downloading it again.")
        self.ui.set_status(w.row_id, "replacing corrupted file")

    def _verify_download(self, path: Path, file: FileInfo) -> None:
        """A finished download must be a sound archive; if not, it is deleted and the download is retried."""
        problem = check_file(path, deep=True)
        if problem:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise DownloadFailed(f"the downloaded file is corrupted: {problem}")
        size = path.stat().st_size
        if file.size_bytes is not None and size != file.size_bytes:
            self.ui.log(
                f"Note: Nexus lists {file.size_bytes:,} bytes for {path.name} but the file has {size:,}. "
                "The download was complete and the archive is intact, so it is kept."
            )

    def _fetch(self, flow: SiteFlow, manifest: Manifest, w: Work) -> str:
        """Download one file, retrying a couple of times. Returns the outcome text."""
        replace = w.existing.path if self._replace_existing and w.existing.state == "ok" else None
        failures = 0
        while True:
            try:
                self.ui.set_status(w.row_id, "opening page")
                path = flow.download(w.item.ref, w.file, replace)
                self._verify_download(path, w.file)
            except LoginRequired:
                self._ensure_login(flow)
                continue
            except PageUnavailable as exc:
                return f"unavailable: {exc}"
            except DownloadFailed as exc:
                failures += 1
                self.ui.log(f"{w.item.name}: {exc} (attempt {failures} of {ATTEMPTS})")
                if failures >= ATTEMPTS:
                    return f"failed: {exc}"
                flow.session.sleep(10 * failures)
                continue
            if replace is not None and replace != path:  # saved under another name: drop the superseded copy
                replace.unlink(missing_ok=True)
            manifest.mark_done(w.item.ref, w.item.name, w.file, path.name, path.stat().st_size, w.item.required_by)
            return "downloaded (replaced a corrupted file)" if w.corrupt else "downloaded"
