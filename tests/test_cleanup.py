from pathlib import Path

import pytest

from nexus_dl.cleanup import Removal, delete_everything, delete_files, summarize
from nexus_dl.store import Manifest

from .helpers import file, make_zip, ref


def record(manifest, folder, mod_id, file_id, name=None):
    """A real zip in the folder plus its manifest entry."""
    name = name or f"{mod_id}-{file_id}.zip"
    path = make_zip(folder / name)
    manifest.mark_done(ref(mod_id), f"mod {mod_id}", file(file_id), name, path.stat().st_size, [])
    return path


def names(folder):
    return sorted(p.name for p in folder.iterdir() if p.name != Manifest.FILENAME)


# -- deleting the files of chosen entries -------------------------------------------------------------


def test_only_the_chosen_file_is_deleted_and_forgotten(tmp_path):
    manifest = Manifest(tmp_path)
    one, two = record(manifest, tmp_path, 1, 10), record(manifest, tmp_path, 2, 20)
    size = one.stat().st_size
    removals = delete_files(manifest, [(ref(1), file(10))])
    assert removals == [Removal("cyberpunk2077:1#10", "deleted", "1-10.zip", size)]
    assert not one.exists() and two.exists()
    reloaded = Manifest(tmp_path)  # the change is on disk
    assert reloaded.entry(ref(1), 10) is None and reloaded.entry(ref(2), 20) is not None


def test_a_recorded_file_that_is_already_gone_is_reported_and_forgotten(tmp_path):
    manifest = Manifest(tmp_path)
    record(manifest, tmp_path, 1, 10).unlink()
    assert delete_files(manifest, [(ref(1), file(10))]) == [Removal("cyberpunk2077:1#10", "missing")]
    assert Manifest(tmp_path).entry(ref(1), 10) is None  # the stale entry is cleaned up


def test_a_file_nobody_recorded_is_found_by_the_name_nexus_gives_it(tmp_path):
    path = make_zip(tmp_path / "Some-Mod-1-1-0-1700000000.zip")
    removals = delete_files(Manifest(tmp_path), [(ref(1), file(10, uri=path.name))])
    assert [r.state for r in removals] == ["deleted"] and not path.exists()


def test_a_file_that_cannot_be_deleted_stays_recorded_and_the_others_are_still_deleted(tmp_path, monkeypatch):
    manifest = Manifest(tmp_path)
    locked, other = record(manifest, tmp_path, 1, 10), record(manifest, tmp_path, 2, 20)
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name == locked.name:
            raise PermissionError(13, "The process cannot access the file")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    removals = delete_files(manifest, [(ref(1), file(10)), (ref(2), file(20))])
    assert [(r.state, r.name) for r in removals] == [("failed", "1-10.zip"), ("deleted", "2-20.zip")]
    assert "cannot access" in removals[0].reason
    assert locked.exists() and not other.exists()
    reloaded = Manifest(tmp_path)
    assert reloaded.entry(ref(1), 10) is not None and reloaded.entry(ref(2), 20) is None


def test_other_files_in_the_folder_are_never_touched(tmp_path):
    manifest = Manifest(tmp_path)
    record(manifest, tmp_path, 1, 10)
    (tmp_path / "vacation.jpg").write_bytes(b"precious")
    delete_files(manifest, [(ref(1), file(10))])
    assert names(tmp_path) == ["vacation.jpg"]


def test_a_hand_edited_manifest_cannot_make_it_delete_outside_the_folder(tmp_path):
    folder = tmp_path / "mods"
    folder.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me", encoding="utf-8")
    manifest = Manifest(folder)
    for bad in ("../outside.txt", "..\\outside.txt", str(outside), "sub/x.zip", ".."):
        manifest.mods = {"cyberpunk2077:1": {"name": "m", "files": {"10": {"fileName": bad, "size": 7}}}}
        assert [r.state for r in delete_files(manifest, [(ref(1), file(10))])] == ["missing"]
        manifest.mods = {"cyberpunk2077:1": {"name": "m", "files": {"10": {"fileName": bad, "size": 7}}}}
        assert [r.state for r in delete_everything(manifest)] == ["failed"]
    assert outside.read_text(encoding="utf-8") == "keep me"


# -- deleting everything the folder's manifest knows -------------------------------------------------


def test_delete_everything_removes_every_tracked_file_and_empties_the_manifest(tmp_path):
    manifest = Manifest(tmp_path)
    paths = [record(manifest, tmp_path, 1, 10), record(manifest, tmp_path, 2, 20), record(manifest, tmp_path, 2, 21)]
    (tmp_path / "vacation.jpg").write_bytes(b"precious")
    (tmp_path / "requirements_report.md").write_text("# report", encoding="utf-8")
    removals = delete_everything(manifest)
    assert sorted(r.row_id for r in removals) == ["cyberpunk2077:1#10", "cyberpunk2077:2#20", "cyberpunk2077:2#21"]
    assert all(r.state == "deleted" for r in removals)
    assert not any(p.exists() for p in paths)
    assert names(tmp_path) == ["requirements_report.md", "vacation.jpg"]  # not ours (or only bookkeeping)
    assert Manifest(tmp_path).mods == {}


def test_delete_everything_writes_the_manifest_once_not_once_per_file(tmp_path, monkeypatch):
    manifest = Manifest(tmp_path)
    for number in range(1, 6):
        record(manifest, tmp_path, number, number * 10)
    saves = []
    original = Manifest.save
    monkeypatch.setattr(Manifest, "save", lambda self: saves.append(1) or original(self))
    delete_everything(manifest)
    assert len(saves) == 1


def test_delete_everything_reports_missing_and_failed_files_and_keeps_the_failed_ones(tmp_path, monkeypatch):
    manifest = Manifest(tmp_path)
    gone, locked, fine = (record(manifest, tmp_path, n, n * 10) for n in (1, 2, 3))
    gone.unlink()
    real_unlink = Path.unlink
    monkeypatch.setattr(
        Path, "unlink",
        lambda self, *a, **k: (_ for _ in ()).throw(PermissionError(13, "in use")) if self.name == locked.name
        else real_unlink(self, *a, **k),
    )
    states = {r.row_id: r.state for r in delete_everything(manifest)}
    assert states == {"cyberpunk2077:1#10": "missing", "cyberpunk2077:2#20": "failed", "cyberpunk2077:3#30": "deleted"}
    assert list(Manifest(tmp_path).mods) == ["cyberpunk2077:2"]  # only the file that is still there stays recorded


def test_delete_everything_on_an_empty_folder_does_nothing(tmp_path):
    assert delete_everything(Manifest(tmp_path)) == []


# -- the sentence shown to the user -------------------------------------------------------------------


@pytest.mark.parametrize(
    "removals, text",
    [
        ([], "Deleted 0 files."),
        ([Removal("a#1", "deleted", "a.zip", 1_048_576)], "Deleted 1 file (1.0 MB)."),
        (
            [Removal("a#1", "deleted", "a.zip", 1024), Removal("b#1", "deleted", "b.zip", 2048),
             Removal("c#1", "missing"), Removal("d#1", "failed", "d.zip", 0, "in use")],
            "Deleted 2 files (3.0 KB), 1 had no file on disk, 1 could not be deleted.",
        ),
    ],
)
def test_summary(removals, text):
    assert summarize(removals) == text
