import threading

import pytest

from nexus_dl import job as job_module
from nexus_dl.budget import RequestBudget
from nexus_dl.downloads import DownloadFailed
from nexus_dl.errors import Cancelled
from nexus_dl.job import Job
from nexus_dl.site_flow import LoginRequired, PageUnavailable
from nexus_dl.store import Config, Manifest

from .helpers import FakeApi, coll_ref, collection, file, mod, need, offsite, pin, ref


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeUi:
    def __init__(self, confirm=True):
        self.logs, self.rows, self.status, self.login = [], [], {}, []
        self.confirm_texts, self._confirm, self.summary = [], confirm, None

    def log(self, text):
        self.logs.append(text)

    def show_plan(self, rows):
        self.rows = rows

    def set_status(self, row_id, status):
        self.status[row_id] = status

    def set_progress(self, done, total, text):
        pass

    def set_file_progress(self, received, total):
        pass

    def set_login(self, logged_in, text):
        self.login.append(logged_in)

    def confirm(self, title, text):
        self.confirm_texts.append(text)
        return self._confirm

    def finished(self, summary):
        self.summary = summary


class FakeSession:
    on_sleep = None  # tests may advance a fake clock here

    def __init__(self, *args, **kwargs):
        FakeSession.sleeps = []
        self.budget = args[2] if len(args) > 2 else kwargs.get("budget")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def sleep(self, seconds):
        FakeSession.sleeps.append(seconds)
        if FakeSession.on_sleep:
            FakeSession.on_sleep(seconds)


class FakeWatcher:
    def __init__(self, session, directory):
        self.directory = directory

    def close(self):
        pass


class FakeFlow:
    """Plays back a script: file id -> list of outcomes (an exception is raised, anything else succeeds)."""

    script: dict = {}
    member = 1
    downloaded: list = []

    def __init__(self, session, watcher, **kwargs):
        self.session, self.directory = session, watcher.directory

    def current_member(self):
        return FakeFlow.member

    def wait_for_login(self):
        FakeFlow.member = 5
        return 5

    def download(self, mod_ref, file_info):
        outcomes = FakeFlow.script.get(file_info.file_id)
        if outcomes:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        FakeFlow.downloaded.append(mod_ref.mod_id)
        path = self.directory / f"{mod_ref.mod_id}-{file_info.file_id}.zip"
        path.write_bytes(b"x" * 10)
        return path


@pytest.fixture
def run(tmp_path, monkeypatch):
    """run(api, links, **config) -> (ui, job); the browser layer is replaced by the fakes above."""
    FakeFlow.script, FakeFlow.member, FakeFlow.downloaded = {}, 1, []
    FakeSession.on_sleep = None
    monkeypatch.setattr(job_module, "ensure_browser", lambda *a, **k: False)
    monkeypatch.setattr(job_module, "BrowserSession", FakeSession)
    monkeypatch.setattr(job_module, "DownloadWatcher", FakeWatcher)
    monkeypatch.setattr(job_module, "SiteFlow", FakeFlow)

    def go(api, roots, ui=None, stop=None, budget=None, **options):
        api_kwargs = {}  # what the job passed to NexusApi(...)
        monkeypatch.setattr(job_module, "NexusApi", lambda **kwargs: api_kwargs.update(kwargs) or api)
        options.setdefault("confirm", False)
        config = Config(download_dir=str(tmp_path), delay_seconds=3, **options)
        ui = ui or FakeUi()
        links = [ref(r) if isinstance(r, int) else r for r in roots]
        job = Job(config, links, ui, stop or threading.Event(), budget)
        job.api_kwargs = api_kwargs
        job.run()
        return ui, job

    return go


def test_downloads_requirements_first_then_records_everything(run, tmp_path):
    api = FakeApi(mod(1, [need(2), offsite("Tool", "https://github.com/a/b")], name="Big"), mod(2, name="Base"))
    ui, _ = run(api, [1])
    assert FakeFlow.downloaded == [2, 1]
    assert ui.summary == "Done: 2 downloaded, 0 already there, 0 failed."
    manifest = Manifest(tmp_path)
    assert manifest.is_done(ref(1), file(10)) and manifest.is_done(ref(2), file(20))
    assert manifest.entry(ref(2), 20)["fileName"] == "2-20.zip"
    assert manifest.entry(ref(1), 10)["requiredBy"] == []
    assert manifest.entry(ref(2), 20)["requiredBy"] == ["cyberpunk2077:1"]
    report = (tmp_path / "requirements_report.md").read_text(encoding="utf-8")
    assert "downloaded" in report and "https://github.com/a/b" in report
    assert any(row.url == "https://github.com/a/b" for row in ui.rows)  # off-site row to open by hand


def test_pauses_between_downloads_but_not_after_the_last(run):
    run(FakeApi(mod(1, [need(2), need(3)]), mod(2), mod(3)), [1])
    assert FakeSession.sleeps == [3, 3]  # three files, two gaps


def test_skips_what_is_already_downloaded(run, tmp_path):
    (tmp_path / "2-20.zip").write_bytes(b"x" * 10)
    Manifest(tmp_path).mark_done(ref(2), "mod 2", file(20), "2-20.zip", 10, [])
    ui, _ = run(FakeApi(mod(1, [need(2)]), mod(2)), [1])
    assert FakeFlow.downloaded == [1]
    assert ui.summary == "Done: 1 downloaded, 1 already there, 0 failed."


def test_everything_already_downloaded_does_not_open_the_browser(run, tmp_path, monkeypatch):
    (tmp_path / "1-10.zip").write_bytes(b"x" * 10)
    Manifest(tmp_path).mark_done(ref(1), "mod 1", file(10), "1-10.zip", 10, [])
    opened = []
    monkeypatch.setattr(job_module, "ensure_browser", lambda *a, **k: opened.append(1))
    ui, _ = run(FakeApi(mod(1)), [1])
    assert ui.summary == "Everything is already downloaded." and not opened


def test_declining_the_confirmation_downloads_nothing(run):
    ui = FakeUi(confirm=False)
    run(FakeApi(mod(1, [need(2)]), mod(2)), [1], ui=ui, confirm=True)
    assert FakeFlow.downloaded == [] and ui.summary == "Cancelled before downloading."
    assert "2 file(s) to download" in ui.confirm_texts[0]


def test_scan_only_writes_the_report_and_downloads_nothing(run, tmp_path):
    ui, _ = run(FakeApi(mod(1, [need(2)]), mod(2)), [1], scan_only=True)
    assert FakeFlow.downloaded == [] and ui.summary == "Scan finished."
    assert (tmp_path / "requirements_report.md").is_file()


def test_failed_download_is_retried_then_reported_and_the_rest_continue(run, tmp_path):
    FakeFlow.script = {20: [DownloadFailed("stalled")] * 3}
    ui, _ = run(FakeApi(mod(1, [need(2)]), mod(2)), [1])
    assert FakeFlow.downloaded == [1]
    assert ui.summary == "Done: 1 downloaded, 0 already there, 1 failed."
    assert ui.status["cyberpunk2077:2#20"] == "failed: stalled"
    assert Manifest(tmp_path).entry(ref(2), 20) is None
    assert FakeSession.sleeps[:2] == [10, 20]  # back-off between the attempts
    assert "failed: stalled" in (tmp_path / "requirements_report.md").read_text(encoding="utf-8")


def test_a_flaky_download_succeeds_on_retry(run):
    FakeFlow.script = {10: [DownloadFailed("hiccup")]}
    ui, _ = run(FakeApi(mod(1)), [1])
    assert FakeFlow.downloaded == [1] and ui.summary.startswith("Done: 1 downloaded")


def test_unavailable_page_is_not_retried(run):
    FakeFlow.script = {10: [PageUnavailable("gone")]}
    ui, _ = run(FakeApi(mod(1)), [1])
    assert FakeFlow.downloaded == [] and ui.status["cyberpunk2077:1#10"] == "unavailable: gone"


def test_waits_for_login_before_starting(run):
    FakeFlow.member = 0
    ui, _ = run(FakeApi(mod(1)), [1])
    assert ui.login == [False, True] and FakeFlow.downloaded == [1]


def test_login_lost_mid_run_waits_and_retries(run):
    FakeFlow.script = {10: [LoginRequired("not logged in")]}
    ui, _ = run(FakeApi(mod(1)), [1])
    assert FakeFlow.downloaded == [1] and ui.summary.startswith("Done: 1 downloaded")


def test_stopping_halfway_still_writes_the_report(run, tmp_path):
    FakeFlow.script = {10: [Cancelled()]}  # the second file (mod 1) is interrupted by Stop
    ui, _ = run(FakeApi(mod(1, [need(2)]), mod(2)), [1])
    assert ui.summary == "Stopped." and FakeFlow.downloaded == [2]
    report = (tmp_path / "requirements_report.md").read_text(encoding="utf-8")
    assert "mod 2" in report and "downloaded" in report and "not downloaded" in report


def test_stop_before_scanning(run):
    stop = threading.Event()
    stop.set()
    ui, _ = run(FakeApi(mod(1)), [1], stop=stop)
    assert ui.summary == "Stopped." and FakeFlow.downloaded == []


def test_api_trouble_is_reported_not_raised(run):
    class Broken:
        def mod_info(self, r):
            raise job_module.NexusApiError("HTTP 503 (gave up after 5 attempts)")

    ui, _ = run(Broken(), [1])
    assert ui.summary == "Error: HTTP 503 (gave up after 5 attempts)"


def test_the_login_text_never_shows_the_member_number(run):
    texts = []

    class Ui(FakeUi):
        def set_login(self, logged_in, text):
            texts.append(text)

    FakeFlow.member = 0
    run(FakeApi(mod(1)), [1], ui=Ui())
    assert texts and not any(char.isdigit() for text in texts for char in text)


# -- collections ----------------------------------------------------------------------------------


def test_a_collection_downloads_every_pinned_file(run, tmp_path):
    api = FakeApi(collections=[collection("abc", pin(5, 51), pin(3, 31, optional=True), pin(3, 32), name="Pack")])
    ui, _ = run(api, [coll_ref("abc")])
    assert FakeFlow.downloaded == [5, 3, 3]
    assert ui.summary == "Done: 3 downloaded, 0 already there, 0 failed."
    manifest = Manifest(tmp_path)
    assert all(manifest.entry(ref(m), f) for m, f in ((5, 51), (3, 31), (3, 32)))
    assert any("Collection 'Pack'" in line for line in ui.logs)


def test_optional_collection_files_are_marked_in_the_table_and_can_be_skipped(run):
    api = FakeApi(collections=[collection("abc", pin(1), pin(2, optional=True))])
    ui, _ = run(api, [coll_ref("abc")], scan_only=True)
    assert [("(optional)" in row.file) for row in ui.rows] == [False, True]
    ui, _ = run(api, [coll_ref("abc")], scan_only=True, skip_optional=True)
    assert len(ui.rows) == 1


def test_a_collection_that_is_already_downloaded_is_skipped(run, tmp_path):
    api = FakeApi(collections=[collection("abc", pin(1, 11), pin(2, 21))])
    run(api, [coll_ref("abc")])
    FakeFlow.downloaded.clear()
    ui, _ = run(api, [coll_ref("abc")])
    assert FakeFlow.downloaded == [] and ui.summary == "Everything is already downloaded."


def test_a_big_collection_confirmation_carries_the_premium_warning(run):
    ui = FakeUi(confirm=False)
    run(FakeApi(collections=[collection("abc", *(pin(i) for i in range(1, 27)))]), [coll_ref("abc")],
        ui=ui, confirm=True)
    text = ui.confirm_texts[0]
    assert "26 file(s)" in text and "big collection" in text and "Premium" in text
    assert "About 312 requests" in text  # 26 files x 12


def test_small_runs_and_big_plain_mod_lists_get_no_collection_warning(run):
    ui = FakeUi(confirm=False)
    run(FakeApi(collections=[collection("abc", pin(1), pin(2))]), [coll_ref("abc")], ui=ui, confirm=True)
    assert "big collection" not in ui.confirm_texts[0] and "Nexus's rules" in ui.confirm_texts[0]
    ui = FakeUi(confirm=False)
    run(FakeApi(*(mod(i) for i in range(1, 31))), list(range(1, 31)), ui=ui, confirm=True)
    assert "big collection" not in ui.confirm_texts[0]


# -- request limits -------------------------------------------------------------------------------


def test_confirmation_explains_the_request_limits_and_a_long_run(run):
    budget = RequestBudget(clock=Clock())
    ui = FakeUi(confirm=False)
    run(FakeApi(*(mod(i) for i in range(1, 41))), list(range(1, 41)), ui=ui, budget=budget, confirm=True)
    text = ui.confirm_texts[0]
    assert "About 480 requests" in text and "500 an hour" in text and "20,000 a day" in text
    assert "pause for the hourly limit" in text and "at least" in text  # 480 > the 450 an hour this tool allows


def test_pauses_when_the_hourly_budget_is_used_up_then_continues(run):
    clock = Clock()
    budget = RequestBudget(hourly=20, clock=clock)
    budget.record(15)  # only 5 left this hour, a file needs ~12
    FakeSession.on_sleep = clock.advance
    ui, _ = run(FakeApi(mod(1)), [1], budget=budget)
    assert FakeFlow.downloaded == [1] and ui.summary.startswith("Done: 1 downloaded")
    assert any("hourly request limit is close" in line for line in ui.logs)
    assert "Request budget available again - continuing." in ui.logs
    assert ui.status["cyberpunk2077:1#10"] == "downloaded"


def test_a_used_up_daily_budget_pauses_the_run_with_a_clear_message(run, tmp_path):
    clock = Clock()
    budget = RequestBudget(hourly=500, daily=20, clock=clock)
    budget.record(15)
    ui, _ = run(FakeApi(mod(1, [need(2)]), mod(2)), [1], budget=budget)
    assert FakeFlow.downloaded == []
    assert ui.summary.startswith("Paused:") and "daily request budget" in ui.summary
    assert "finished files are remembered" in ui.summary
    assert (tmp_path / "requirements_report.md").is_file()


def test_the_scan_is_budgeted_and_its_waits_can_be_interrupted(run):
    budget = RequestBudget(clock=Clock())
    _, job = run(FakeApi(mod(1)), [1], budget=budget, scan_only=True)
    assert job.api_kwargs["budget"] is budget
    assert callable(job.api_kwargs["on_wait"])
    job.stop.set()
    with pytest.raises(Cancelled):
        job.api_kwargs["sleep"](5)  # the API client sleeps through the job's stop-aware sleep
