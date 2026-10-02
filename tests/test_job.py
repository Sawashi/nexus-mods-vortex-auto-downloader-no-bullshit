import os
import threading
import time

import pytest

from nexus_dl import job as job_module
from nexus_dl.budget import RequestBudget
from nexus_dl.downloads import DownloadFailed, unique_path
from nexus_dl.errors import Cancelled
from nexus_dl.job import Job
from nexus_dl.site_flow import LoginRequired, PageUnavailable
from nexus_dl.store import Config, Manifest
from nexus_dl.verify import check_file

from .helpers import FakeApi, coll_ref, collection, file, make_zip, mod, need, offsite, pin, ref, truncate


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
    """Plays back a script: file id -> list of outcomes.

    An exception is raised, "corrupt" saves a damaged archive, anything else saves a good zip.
    """

    script: dict = {}
    member = 1
    downloaded: list = []
    replace_args: list = []  # the `replace` argument of every download call

    def __init__(self, session, watcher, **kwargs):
        self.session, self.directory = session, watcher.directory

    def current_member(self):
        return FakeFlow.member

    def wait_for_login(self):
        FakeFlow.member = 5
        return 5

    def download(self, mod_ref, file_info, replace=None):
        outcomes = FakeFlow.script.get(file_info.file_id)
        outcome = outcomes.pop(0) if outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        FakeFlow.downloaded.append(mod_ref.mod_id)
        FakeFlow.replace_args.append(replace)
        name = file_info.expected_name or f"{mod_ref.mod_id}-{file_info.file_id}.zip"
        path = replace if replace is not None else unique_path(self.directory, name)
        if outcome == "corrupt":
            path.write_bytes(b"this is not a zip archive")
        else:
            make_zip(path)
        return path


@pytest.fixture
def run(tmp_path, monkeypatch):
    """run(api, links, **config) -> (ui, job); the browser layer is replaced by the fakes above."""
    FakeFlow.script, FakeFlow.member, FakeFlow.downloaded, FakeFlow.replace_args = {}, 1, [], []
    FakeSession.on_sleep = None
    monkeypatch.setattr(job_module, "ensure_browser", lambda *a, **k: False)
    monkeypatch.setattr(job_module, "BrowserSession", FakeSession)
    monkeypatch.setattr(job_module, "DownloadWatcher", FakeWatcher)
    monkeypatch.setattr(job_module, "SiteFlow", FakeFlow)

    def go(api, roots, ui=None, stop=None, budget=None, redo=None, **options):
        api_kwargs = {}  # what the job passed to NexusApi(...)
        monkeypatch.setattr(job_module, "NexusApi", lambda **kwargs: api_kwargs.update(kwargs) or api)
        options.setdefault("confirm", False)
        config = Config(download_dir=str(tmp_path), delay_seconds=3, **options)
        ui = ui or FakeUi()
        links = [ref(r) if isinstance(r, int) else r for r in roots]
        job = Job(config, links, ui, stop or threading.Event(), budget, redo)
        job.api_kwargs = api_kwargs
        job.run()
        return ui, job

    return go


def put_download(tmp_path, mod_ref, file_info, name=None):
    """Pretend an earlier run downloaded this file: a real zip on disk plus its manifest entry."""
    name = name or f"{mod_ref.mod_id}-{file_info.file_id}.zip"
    path = make_zip(tmp_path / name)
    Manifest(tmp_path).mark_done(mod_ref, f"mod {mod_ref.mod_id}", file_info, name, path.stat().st_size, [])
    return path


def zips(tmp_path):
    return sorted(p.name for p in tmp_path.glob("*.zip"))


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
    put_download(tmp_path, ref(2), file(20))
    ui, _ = run(FakeApi(mod(1, [need(2)]), mod(2)), [1])
    assert FakeFlow.downloaded == [1]
    assert ui.summary == "Done: 1 downloaded, 1 already there, 0 failed."


def test_everything_already_downloaded_does_not_open_the_browser(run, tmp_path, monkeypatch):
    put_download(tmp_path, ref(1), file(10))
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


# -- duplicates: a file that is already there is not downloaded again ---------------------------------


def test_a_second_run_downloads_nothing_and_creates_no_copies(run, tmp_path):
    api = FakeApi(mod(1, [need(2)]), mod(2))
    run(api, [1])
    FakeFlow.downloaded.clear()
    ui, _ = run(api, [1])
    assert FakeFlow.downloaded == [] and ui.summary == "Everything is already downloaded."
    assert zips(tmp_path) == ["1-10.zip", "2-20.zip"]


def test_a_valid_file_nobody_recorded_is_adopted_instead_of_downloaded_twice(run, tmp_path):
    path = make_zip(tmp_path / "Some-Mod-1-1-0-1700000000.zip")  # e.g. the manifest was deleted, or Vortex got it
    api = FakeApi(mod(1, files=[file(10, uri=path.name, size=path.stat().st_size)]))
    ui, _ = run(api, [1])
    assert FakeFlow.downloaded == [] and ui.summary == "Everything is already downloaded."
    assert Manifest(tmp_path).entry(ref(1), 10)["fileName"] == path.name  # remembered from now on
    assert any("Found Some-Mod-1-1-0-1700000000.zip already in the folder" in line for line in ui.logs)
    assert zips(tmp_path) == [path.name]


def test_downloading_again_on_request_overwrites_instead_of_making_a_copy(run, tmp_path):
    api = FakeApi(mod(1))
    run(api, [1])
    FakeFlow.downloaded.clear()
    ui, _ = run(api, [1], skip_existing=False)
    assert FakeFlow.downloaded == [1]
    assert FakeFlow.replace_args == [None, tmp_path / "1-10.zip"]  # the second call was told what to overwrite
    assert zips(tmp_path) == ["1-10.zip"]
    assert ui.rows[0].status == "queued (downloads it again)"


# -- corrupted files: found, deleted and downloaded again --------------------------------------------


def test_a_corrupted_file_is_deleted_and_downloaded_again(run, tmp_path):
    api = FakeApi(mod(1))
    run(api, [1])
    path = tmp_path / "1-10.zip"
    truncate(path, 0.5)  # the download gets damaged afterwards
    FakeFlow.downloaded.clear()

    ui, _ = run(api, [1])

    assert FakeFlow.downloaded == [1]
    assert zips(tmp_path) == ["1-10.zip"]  # no "(1)" copy and no corrupted leftover
    assert check_file(path, deep=True) is None
    assert Manifest(tmp_path).entry(ref(1), 10)["size"] == path.stat().st_size
    assert ui.rows[0].status == "queued (replaces a corrupted file)"  # what the table showed before downloading
    assert ui.status["cyberpunk2077:1#10"] == "downloaded (replaced a corrupted file)"  # ... and what it ended as
    assert any("Corrupted file found: 1-10.zip" in line for line in ui.logs)
    assert any("Deleted the corrupted file 1-10.zip" in line for line in ui.logs)
    assert ui.summary == "Done: 1 downloaded, 0 already there, 0 failed. 1 corrupted file(s) were replaced."
    assert "downloaded (replaced a corrupted file)" in (tmp_path / "requirements_report.md").read_text(encoding="utf-8")


def test_the_plan_shows_which_files_are_corrupted_before_anything_happens(run, tmp_path):
    api = FakeApi(mod(1), mod(2))
    run(api, [1, 2])
    truncate(tmp_path / "2-20.zip", 0.3)
    ui = FakeUi(confirm=False)
    run(api, [1, 2], ui=ui, confirm=True)
    assert [row.status for row in ui.rows] == ["already downloaded", "queued (replaces a corrupted file)"]
    assert "1 corrupted file(s) in the folder will be deleted and downloaded again" in ui.confirm_texts[0]


def test_scanning_or_declining_never_deletes_a_corrupted_file(run, tmp_path):
    api = FakeApi(mod(1))
    run(api, [1])
    path = tmp_path / "1-10.zip"
    truncate(path, 0.5)
    damaged = path.read_bytes()

    ui, _ = run(api, [1], scan_only=True)
    assert ui.summary == "Scan finished." and path.read_bytes() == damaged
    assert "corrupted file found (not replaced yet)" in (tmp_path / "requirements_report.md").read_text(encoding="utf-8")

    ui, _ = run(api, [1], ui=FakeUi(confirm=False), confirm=True)
    assert path.read_bytes() == damaged and zips(tmp_path) == ["1-10.zip"]
    assert Manifest(tmp_path).entry(ref(1), 10) is not None  # still remembered until it is actually replaced


def test_if_the_new_download_also_fails_nothing_corrupted_is_left_and_the_next_run_recovers(run, tmp_path):
    api = FakeApi(mod(1))
    run(api, [1])
    truncate(tmp_path / "1-10.zip", 0.5)
    FakeFlow.script = {10: [DownloadFailed("offline")] * 3}

    ui, _ = run(api, [1])
    assert "1 failed" in ui.summary
    assert zips(tmp_path) == [] and Manifest(tmp_path).entry(ref(1), 10) is None
    assert "failed: offline" in (tmp_path / "requirements_report.md").read_text(encoding="utf-8")

    FakeFlow.script = {}
    ui, _ = run(api, [1])
    assert ui.summary == "Done: 1 downloaded, 0 already there, 0 failed." and zips(tmp_path) == ["1-10.zip"]


def test_a_file_with_the_right_name_but_the_wrong_size_is_replaced_once_not_forever(run, tmp_path):
    path = make_zip(tmp_path / "Some-Mod-1-1-0-1700000000.zip")
    api = FakeApi(mod(1, files=[file(10, uri=path.name, size=path.stat().st_size + 999)]))  # sizes disagree
    ui, _ = run(api, [1])
    assert FakeFlow.downloaded == [1] and zips(tmp_path) == [path.name]  # replaced in place, not duplicated
    assert any("Corrupted file found" in line for line in ui.logs)

    FakeFlow.downloaded.clear()
    ui, _ = run(api, [1])  # the manifest now records what was really downloaded, so this is not repeated
    assert FakeFlow.downloaded == [] and ui.summary == "Everything is already downloaded."


# -- downloading chosen entries again -----------------------------------------------------------------


def file_works(ui):
    """What the table hands to "Download again": the `work` of every row that is a file."""
    return [row.work for row in ui.rows if row.work is not None]


def test_table_rows_carry_what_delete_and_download_again_need(run):
    ui, _ = run(FakeApi(mod(1, [need(2), offsite("tool")]), mod(2)), [1])
    assert [row.id for row in ui.rows if row.work] == ["cyberpunk2077:2#20", "cyberpunk2077:1#10"]
    assert [row.work for row in ui.rows if row.id.startswith("ext:")] == [None]  # off-site: nothing to act on
    work = file_works(ui)[0]
    assert work.item.ref == ref(2) and work.file.file_id == 20


def test_download_again_replaces_the_copy_without_scanning_or_rewriting_the_report(run, tmp_path):
    api = FakeApi(mod(1))
    ui, _ = run(api, [1])
    works, report = file_works(ui), (tmp_path / "requirements_report.md").read_text(encoding="utf-8")
    api.calls.clear()

    ui2, job = run(api, [], redo=works)  # "skip existing" is on in the settings, and still the file is replaced

    assert api.calls == [] and job.api_kwargs == {}  # nothing was looked up on Nexus
    assert FakeFlow.downloaded == [1, 1] and FakeFlow.replace_args == [None, tmp_path / "1-10.zip"]
    assert zips(tmp_path) == ["1-10.zip"]  # replaced, not copied
    assert ui2.rows == []  # the table was left alone: only statuses change
    assert ui2.status["cyberpunk2077:1#10"] == "downloaded"
    assert ui2.summary == "Done: 1 downloaded, 0 already there, 0 failed."
    assert (tmp_path / "requirements_report.md").read_text(encoding="utf-8") == report  # not rewritten


def test_download_again_works_for_a_file_that_was_deleted(run, tmp_path):
    api = FakeApi(mod(1))
    ui, _ = run(api, [1])
    works = file_works(ui)
    (tmp_path / "1-10.zip").unlink()
    ui2, _ = run(api, [], redo=works)
    assert zips(tmp_path) == ["1-10.zip"] and FakeFlow.replace_args[-1] is None  # nothing to overwrite
    assert ui2.summary == "Done: 1 downloaded, 0 already there, 0 failed."


def test_download_again_of_a_corrupted_file_deletes_it_first(run, tmp_path):
    api = FakeApi(mod(1))
    ui, _ = run(api, [1])
    works = file_works(ui)
    truncate(tmp_path / "1-10.zip", 0.5)
    ui2, _ = run(api, [], redo=works)
    assert zips(tmp_path) == ["1-10.zip"] and check_file(tmp_path / "1-10.zip", deep=True) is None
    assert any("Deleted the corrupted file 1-10.zip" in line for line in ui2.logs)
    assert ui2.summary.endswith("1 corrupted file(s) were replaced.")


def test_a_failed_download_again_keeps_the_old_good_copy(run, tmp_path):
    api = FakeApi(mod(1))
    ui, _ = run(api, [1])
    works = file_works(ui)
    before = (tmp_path / "1-10.zip").read_bytes()
    FakeFlow.script = {10: [DownloadFailed("offline")] * 3}
    ui2, _ = run(api, [], redo=works)
    assert "1 failed" in ui2.summary
    assert (tmp_path / "1-10.zip").read_bytes() == before  # the copy is only replaced once the new one is complete
    assert Manifest(tmp_path).entry(ref(1), 10) is not None


def test_downloading_several_entries_again_asks_first_but_a_single_one_does_not(run):
    api = FakeApi(mod(1), mod(2))
    ui, _ = run(api, [1, 2])
    works = file_works(ui)
    FakeFlow.downloaded.clear()

    asking = FakeUi(confirm=False)
    run(api, [], redo=works, ui=asking, confirm=True)
    assert FakeFlow.downloaded == [] and asking.summary == "Cancelled before downloading."
    assert "2 file(s) to download" in asking.confirm_texts[0]

    quick = FakeUi(confirm=False)
    run(api, [], redo=works[:1], ui=quick, confirm=True)
    assert quick.confirm_texts == [] and len(FakeFlow.downloaded) == 1  # one file: no question, it just happens


def test_download_again_still_waits_for_the_request_budget(run):
    clock = Clock()
    budget = RequestBudget(hourly=20, clock=clock)
    api = FakeApi(mod(1))
    ui, _ = run(api, [1])
    works = file_works(ui)
    budget.record(15)
    FakeSession.on_sleep = clock.advance
    ui2, _ = run(api, [], redo=works, budget=budget)
    assert any("hourly request limit is close" in line for line in ui2.logs)
    assert ui2.summary.startswith("Done: 1 downloaded")


# -- a fresh download is checked before it is accepted ----------------------------------------------


def test_a_corrupted_fresh_download_is_deleted_and_retried(run, tmp_path):
    FakeFlow.script = {10: ["corrupt"]}
    ui, _ = run(FakeApi(mod(1)), [1])
    assert ui.summary == "Done: 1 downloaded, 0 already there, 0 failed."
    assert zips(tmp_path) == ["1-10.zip"] and check_file(tmp_path / "1-10.zip", deep=True) is None
    assert any("the downloaded file is corrupted" in line and "attempt 1 of 3" in line for line in ui.logs)


def test_a_download_that_stays_corrupted_is_reported_and_leaves_no_file_behind(run, tmp_path):
    FakeFlow.script = {10: ["corrupt"] * 3}
    ui, _ = run(FakeApi(mod(1)), [1])
    assert "1 failed" in ui.summary
    assert ui.status["cyberpunk2077:1#10"].startswith("failed: the downloaded file is corrupted")
    assert zips(tmp_path) == [] and Manifest(tmp_path).entry(ref(1), 10) is None


def test_a_size_difference_with_nexus_is_only_a_note_when_the_archive_is_intact(run, tmp_path):
    api = FakeApi(mod(1, files=[file(10, size=1234)]))  # Nexus lists a size the real zip does not have
    ui, _ = run(api, [1])
    assert ui.summary.startswith("Done: 1 downloaded") and zips(tmp_path) == ["1-10.zip"]
    assert any("Nexus lists 1,234 bytes" in line for line in ui.logs)
    ui, _ = run(api, [1])  # ... and it is not downloaded again and again because of that
    assert ui.summary == "Everything is already downloaded."


def test_leftovers_of_a_crashed_run_are_cleaned_up_before_downloading(run, tmp_path):
    long_ago = time.time() - 3600
    junk = tmp_path / "0F8FAD5B-D9CB-469F-A165-70867728950E"
    junk.write_bytes(b"partial")
    os.utime(junk, (long_ago, long_ago))
    notes = tmp_path / "my notes.txt"
    notes.write_text("not ours", encoding="utf-8")
    ui, _ = run(FakeApi(mod(1)), [1])
    assert not junk.exists() and notes.exists()
    assert any("Removed an unfinished download" in line for line in ui.logs)


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
