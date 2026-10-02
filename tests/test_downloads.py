import os
import time

import pytest

from nexus_dl.downloads import DownloadFailed, DownloadWatcher, remove_leftovers, unique_path
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


def finished_download(session, watcher, tmp_path, data=b"x" * 100, total=100):
    begin(session)
    (tmp_path / "g1").write_bytes(data)
    progress(session, received=len(data), total=total, state="completed")
    return watcher.captures["g1"]


def test_a_download_that_replaces_a_file_takes_its_place_instead_of_becoming_name_1(tmp_path):
    session = FakeSession()
    watcher = DownloadWatcher(session, tmp_path)
    old = tmp_path / "Mod-1-0-1.zip"
    old.write_bytes(b"old copy")
    capture = finished_download(session, watcher, tmp_path)
    saved = watcher.save(capture, tmp_path / "g1", replace=old)
    assert saved == old and old.read_bytes() == b"x" * 100
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Mod-1-0-1.zip"]


def test_without_replace_an_existing_file_is_never_overwritten(tmp_path):
    session = FakeSession()
    watcher = DownloadWatcher(session, tmp_path)
    (tmp_path / "Mod-1-0-1.zip").write_bytes(b"somebody else's file")
    capture = finished_download(session, watcher, tmp_path)
    assert watcher.save(capture, tmp_path / "g1") == tmp_path / "Mod-1-0-1 (1).zip"
    assert (tmp_path / "Mod-1-0-1.zip").read_bytes() == b"somebody else's file"


def test_replace_is_ignored_when_the_new_file_has_another_name(tmp_path):
    session = FakeSession()
    watcher = DownloadWatcher(session, tmp_path)
    other = tmp_path / "renamed-by-the-user.zip"
    other.write_bytes(b"keep me")
    capture = finished_download(session, watcher, tmp_path)
    assert watcher.save(capture, tmp_path / "g1", replace=other) == tmp_path / "Mod-1-0-1.zip"
    assert other.read_bytes() == b"keep me"


def test_when_the_browser_gives_no_size_the_size_nexus_lists_is_used(tmp_path):
    session = FakeSession()
    watcher = DownloadWatcher(session, tmp_path)
    capture = finished_download(session, watcher, tmp_path, data=b"x" * 60, total=0)
    with pytest.raises(DownloadFailed, match="incomplete"):
        watcher.wait_until_done(capture, stall_timeout=10, expected_size=100)
    assert not (tmp_path / "g1").exists()

    session2 = FakeSession()
    watcher2 = DownloadWatcher(session2, tmp_path)
    capture2 = finished_download(session2, watcher2, tmp_path, data=b"x" * 100, total=0)
    assert watcher2.wait_until_done(capture2, stall_timeout=10, expected_size=100) == tmp_path / "g1"


def test_the_size_the_browser_announced_wins_over_the_one_nexus_lists(tmp_path):
    session = FakeSession()
    watcher = DownloadWatcher(session, tmp_path)
    capture = finished_download(session, watcher, tmp_path, data=b"x" * 100, total=100)
    assert watcher.wait_until_done(capture, stall_timeout=10, expected_size=999) == tmp_path / "g1"


def test_unfinished_downloads_of_an_earlier_run_are_removed_but_nothing_else(tmp_path):
    old = time.time() - 3600
    guid = "0F8FAD5B-D9CB-469F-A165-70867728950E"
    leftovers = [tmp_path / guid, tmp_path / (guid.lower() + ".crdownload")]
    keep = [tmp_path / "Mod-1-0-1.zip", tmp_path / "notes.txt", tmp_path / (guid + ".zip"), tmp_path / "1234-5678"]
    for path in leftovers + keep:
        path.write_bytes(b"data")
        os.utime(path, (old, old))
    assert sorted(remove_leftovers(tmp_path)) == sorted(p.name for p in leftovers)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(p.name for p in keep)


def test_a_fresh_unfinished_file_is_left_alone_in_case_another_download_is_running(tmp_path):
    (tmp_path / "0F8FAD5B-D9CB-469F-A165-70867728950E").write_bytes(b"in progress")
    assert remove_leftovers(tmp_path) == []
    assert remove_leftovers(tmp_path / "does-not-exist") == []


def test_close_restores_default_download_behaviour(tmp_path):
    session = FakeSession()
    DownloadWatcher(session, tmp_path).close()
    assert session.cdp.sent[-1] == ("Browser.setDownloadBehavior", {"behavior": "default"})
