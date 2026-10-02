import pytest

from nexus_dl.downloads import DownloadFailed, DownloadWatcher, unique_path
from nexus_dl.errors import Cancelled


class FakeCdp:
    def __init__(self):
        self.handlers = {}
        self.sent = []

    def on(self, event, handler):
        self.handlers[event] = handler

    def send(self, method, params=None):
        self.sent.append((method, params))

    def detach(self):
        pass

    def fire(self, event, params):
        self.handlers[event](params)


class FakeSession:
    """`script` holds one callable per sleep() call: what the browser does while we wait."""

    def __init__(self, script=()):
        self.cdp = FakeCdp()
        self.browser = self
        self.script = list(script)

    def new_browser_cdp_session(self):
        return self.cdp

    def sleep(self, seconds):
        if self.script:
            self.script.pop(0)()


def begin(session, guid="g1", name="Mod-1-0-1.zip"):
    session.cdp.fire("Browser.downloadWillBegin", {"guid": guid, "url": "https://cdn/x", "suggestedFilename": name})


def progress(session, guid="g1", received=0, total=100, state="inProgress"):
    session.cdp.fire("Browser.downloadProgress", {"guid": guid, "receivedBytes": received, "totalBytes": total, "state": state})


def test_unique_path(tmp_path):
    assert unique_path(tmp_path, "a.zip") == tmp_path / "a.zip"
    (tmp_path / "a.zip").write_bytes(b"1")
    assert unique_path(tmp_path, "a.zip") == tmp_path / "a (1).zip"
    (tmp_path / "a (1).zip").write_bytes(b"1")
    assert unique_path(tmp_path, "a.zip") == tmp_path / "a (2).zip"


def test_arm_asks_the_browser_to_save_here_and_report_progress(tmp_path):
    session = FakeSession()
    DownloadWatcher(session, tmp_path / "out").arm()
    assert session.cdp.sent == [
        ("Browser.setDownloadBehavior",
         {"behavior": "allowAndName", "downloadPath": str(tmp_path / "out"), "eventsEnabled": True})
    ]
    assert (tmp_path / "out").is_dir()


def test_waits_for_a_download_that_was_not_already_running(tmp_path):
    session = FakeSession()
    watcher = DownloadWatcher(session, tmp_path)
    begin(session, "old")
    known = watcher.known()
    session.script = [lambda: None, lambda: begin(session, "new")]
    assert watcher.wait_for_new(known, timeout=5).guid == "new"


def test_times_out_when_nothing_starts(tmp_path):
    watcher = DownloadWatcher(FakeSession(), tmp_path)
    with pytest.raises(DownloadFailed, match="did not start"):
        watcher.wait_for_new(set(), timeout=1)


def test_complete_download_is_renamed_to_its_real_name(tmp_path):
    session = FakeSession()
    watcher = DownloadWatcher(session, tmp_path)
    begin(session)
    capture = watcher.captures["g1"]
    seen = []

    def finish():
        (tmp_path / "g1").write_bytes(b"x" * 100)
        progress(session, received=100, state="completed")

    session.script = [lambda: progress(session, received=40), finish]
    partial = watcher.wait_until_done(capture, stall_timeout=10, on_progress=lambda got, total: seen.append(got))
    saved = watcher.save(capture, partial)
    assert saved == tmp_path / "Mod-1-0-1.zip" and saved.read_bytes() == b"x" * 100
    assert not (tmp_path / "g1").exists()
    assert seen[0] == 40 and seen[-1] == 100


def test_stalled_download_is_cancelled_and_cleaned_up(tmp_path):
    session = FakeSession()
    watcher = DownloadWatcher(session, tmp_path)
    begin(session)
    (tmp_path / "g1.crdownload").write_bytes(b"partial")
    with pytest.raises(DownloadFailed, match="no data"):
        watcher.wait_until_done(watcher.captures["g1"], stall_timeout=1)
    assert ("Browser.cancelDownload", {"guid": "g1"}) in session.cdp.sent
    assert not (tmp_path / "g1.crdownload").exists()


def test_stop_during_a_transfer_cancels_it_in_the_browser(tmp_path):
    session = FakeSession()
    watcher = DownloadWatcher(session, tmp_path)
    begin(session)
    (tmp_path / "g1.crdownload").write_bytes(b"partial")

    def press_stop():
        raise Cancelled()

    session.script = [lambda: progress(session, received=10), press_stop]
    with pytest.raises(Cancelled):
        watcher.wait_until_done(watcher.captures["g1"], stall_timeout=10)
    assert ("Browser.cancelDownload", {"guid": "g1"}) in session.cdp.sent
    assert not (tmp_path / "g1.crdownload").exists()


def test_incomplete_file_is_rejected(tmp_path):
    session = FakeSession()
    watcher = DownloadWatcher(session, tmp_path)
    begin(session)

    def finish():
        (tmp_path / "g1").write_bytes(b"x" * 60)  # browser says 100 bytes, only 60 arrived
        progress(session, received=60, total=100, state="completed")

    session.script = [finish]
    with pytest.raises(DownloadFailed, match="incomplete"):
        watcher.wait_until_done(watcher.captures["g1"], stall_timeout=10)
    assert not (tmp_path / "g1").exists()


def test_browser_cancelling_the_download_is_a_failure(tmp_path):
    session = FakeSession()
    watcher = DownloadWatcher(session, tmp_path)
    begin(session)
    session.script = [lambda: progress(session, state="canceled")]
    with pytest.raises(DownloadFailed, match="cancelled"):
        watcher.wait_until_done(watcher.captures["g1"], stall_timeout=10)


def test_close_restores_default_download_behaviour(tmp_path):
    session = FakeSession()
    DownloadWatcher(session, tmp_path).close()
    assert session.cdp.sent[-1] == ("Browser.setDownloadBehavior", {"behavior": "default"})
