import json
from dataclasses import fields

from nexus_dl.resolver import Resolver
from nexus_dl.store import Config, Existing, Manifest, load_config, render_report, save_config, write_report

from .helpers import FakeApi, coll_ref, collection, file, make_zip, mod, need, offsite, pin, ref, truncate


def make_plan():
    api = FakeApi(
        mod(1, [need(2), offsite("Tool", "https://github.com/a/b", "grab it")], name="Big Mod"),
        mod(2, name="Framework"),
        mod(5, dlc=["Phantom Liberty"], name="Needs DLC"),
        missing=[7],
    )
    return Resolver(api).resolve([ref(1), ref(5), ref(7)])


# -- config ----------------------------------------------------------------------------------


def test_config_round_trip(tmp_path):
    path = tmp_path / "config.json"
    save_config(Config(download_dir="D:/mods", delay_seconds=9, all_main=True, skip_optional=True), path)
    loaded = load_config(path)
    assert loaded.download_dir == "D:/mods" and loaded.delay_seconds == 9
    assert loaded.all_main is True and loaded.skip_optional is True


def test_config_survives_missing_corrupt_and_wrong_typed_files(tmp_path):
    assert load_config(tmp_path / "nope.json") == Config()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_config(bad) == Config()
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"delay_seconds": "fast", "unknown": 1, "confirm": False}), encoding="utf-8")
    loaded = load_config(wrong)
    assert loaded.delay_seconds == Config().delay_seconds and loaded.confirm is False


def test_the_settings_file_has_no_room_for_links_or_secrets():
    names = {f.name for f in fields(Config)}
    assert not names & {"links", "api_key", "password", "token", "cookie"}


def test_old_settings_files_lose_their_links_and_api_key_on_the_next_save(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"links": "https://www.nexusmods.com/cyberpunk2077/mods/1", "api_key": "SECRET",
                                "download_dir": "D:/mods"}), encoding="utf-8")
    config = load_config(path)
    assert config.download_dir == "D:/mods"
    save_config(config, path)
    text = path.read_text(encoding="utf-8")
    assert "SECRET" not in text and "nexusmods.com" not in text and "api_key" not in text


def test_default_limits_are_ninety_percent_of_nexus_limits():
    assert (Config().request_limit_hour, Config().request_limit_day) == (450, 18_000)


# -- manifest --------------------------------------------------------------------------------


def test_manifest_marks_and_detects_downloads(tmp_path):
    f = file(10, size=5)
    manifest = Manifest(tmp_path)
    assert not manifest.is_done(ref(1), f)

    (tmp_path / "mod-1.dat").write_bytes(b"12345")
    manifest.mark_done(ref(1), "Mod One", f, "mod-1.dat", 5, [ref(9)])

    reloaded = Manifest(tmp_path)  # persisted to disk
    assert reloaded.is_done(ref(1), f)
    assert reloaded.entry(ref(1), 10)["requiredBy"] == ["cyberpunk2077:9"]
    assert not reloaded.is_done(ref(1), file(11))  # a different (newer) file is not "done"


def test_manifest_tolerates_garbage(tmp_path):
    (tmp_path / Manifest.FILENAME).write_text("[]", encoding="utf-8")
    assert Manifest(tmp_path).mods == {}


# -- what is already in the folder: intact, missing or corrupted ------------------------------------


def downloaded(tmp_path, name="mod-1.zip", size=None):
    """A manifest that recorded a real zip download of ref(1) / file 10."""
    path = make_zip(tmp_path / name)
    manifest = Manifest(tmp_path)
    manifest.mark_done(ref(1), "Mod One", file(10, size=size), name, path.stat().st_size, [])
    return manifest, path


def test_an_intact_recorded_file_is_ok(tmp_path):
    manifest, path = downloaded(tmp_path)
    assert manifest.inspect(ref(1), file(10)) == Existing("ok", path, "", True)


def test_a_missing_file_is_missing(tmp_path):
    manifest, path = downloaded(tmp_path)
    path.unlink()
    assert manifest.inspect(ref(1), file(10)).state == "missing"
    assert Manifest(tmp_path / "empty").inspect(ref(1), file(10)).state == "missing"


def test_a_truncated_file_is_corrupt_and_says_why(tmp_path):
    manifest, path = downloaded(tmp_path)
    truncate(path, 0.5)
    found = manifest.inspect(ref(1), file(10))
    assert (found.state, found.path, found.recorded) == ("corrupt", path, True)
    assert "should have" in found.reason  # the size no longer matches what was downloaded


def test_a_file_of_the_right_size_but_a_broken_structure_is_corrupt(tmp_path):
    manifest, path = downloaded(tmp_path)
    size = path.stat().st_size
    path.write_bytes(b"x" * size)  # same size, not an archive any more
    assert "the zip is damaged" in manifest.inspect(ref(1), file(10)).reason


def test_the_recorded_size_decides_not_the_size_nexus_lists(tmp_path):
    """Otherwise a stale size on Nexus's side would make a good file look corrupt on every run."""
    manifest, path = downloaded(tmp_path, size=123_456_789)
    assert manifest.inspect(ref(1), file(10, size=123_456_789)).state == "ok"


def test_a_valid_file_nobody_recorded_is_found_by_the_name_nexus_gives_it(tmp_path):
    path = make_zip(tmp_path / "Some-Mod-1-2-3-1700000000.zip")
    manifest = Manifest(tmp_path)  # empty: e.g. the manifest was deleted, or Vortex downloaded it
    found = manifest.inspect(ref(1), file(10, size=path.stat().st_size, uri=path.name))
    assert (found.state, found.path, found.recorded) == ("ok", path, False)


def test_a_file_found_by_name_but_with_the_wrong_size_is_corrupt(tmp_path):
    path = make_zip(tmp_path / "Some-Mod-1-2-3-1700000000.zip")
    truncate(path, 0.7)
    found = Manifest(tmp_path).inspect(ref(1), file(10, size=path.stat().st_size + 500, uri=path.name))
    assert found.state == "corrupt" and found.recorded is False


def test_newer_files_only_have_a_storage_path_so_they_cannot_be_looked_up_by_name(tmp_path):
    make_zip(tmp_path / "ab.zip")
    assert Manifest(tmp_path).inspect(ref(1), file(10, uri="8f/ba/44/8fba44ab-cdc1")).state == "missing"
    assert Manifest(tmp_path).inspect(ref(1), file(10, uri="")).state == "missing"


def test_tracked_lists_everything_the_manifest_knows(tmp_path):
    manifest, path = downloaded(tmp_path)
    manifest.mark_done(ref(2), "Mod Two", file(20), "two.dat", 9, [])
    assert [(t.row_id, t.name, t.size) for t in manifest.tracked()] == [
        ("cyberpunk2077:1#10", "mod-1.zip", path.stat().st_size), ("cyberpunk2077:2#20", "two.dat", 9)]
    assert manifest.tracked()[0].path == path


def test_locate_finds_a_recorded_file_or_one_with_the_name_nexus_gives_it(tmp_path):
    manifest, path = downloaded(tmp_path)
    assert manifest.locate(ref(1), file(10)) == path
    assert manifest.locate(ref(1), file(11)) is None
    named = make_zip(tmp_path / "Some-Mod-3-1-0-1700000000.zip")
    assert manifest.locate(ref(3), file(30, uri=named.name)) == named
    path.unlink()
    assert manifest.locate(ref(1), file(10)) is None  # recorded, but gone


def test_a_name_in_the_manifest_that_is_not_a_plain_file_name_never_leads_outside_the_folder(tmp_path):
    folder = tmp_path / "mods"
    folder.mkdir()
    (tmp_path / "outside.txt").write_text("keep me", encoding="utf-8")
    manifest = Manifest(folder)
    for bad in ("../outside.txt", "..\\outside.txt", str(tmp_path / "outside.txt"), "sub/x.zip", "", ".", ".."):
        manifest.mods = {"cyberpunk2077:1": {"name": "m", "files": {"10": {"fileName": bad, "size": 7}}}}
        assert manifest.tracked()[0].path is None
        assert manifest.locate(ref(1), file(10)) is None
        assert manifest.inspect(ref(1), file(10)).state == "missing"


def test_drop_removes_an_entry_in_memory_until_saved(tmp_path):
    manifest, _ = downloaded(tmp_path)
    assert manifest.drop("cyberpunk2077:1", 10) is True and manifest.mods == {}
    assert manifest.drop("cyberpunk2077:1", 10) is False
    assert Manifest(tmp_path).entry(ref(1), 10) is not None  # nothing was written yet
    manifest.save()
    assert Manifest(tmp_path).entry(ref(1), 10) is None


def test_forgetting_a_file_removes_it_from_the_manifest_on_disk(tmp_path):
    manifest, _ = downloaded(tmp_path)
    manifest.mark_done(ref(1), "Mod One", file(11), "other.dat", 3, [])
    manifest.forget(ref(1), 10)
    assert Manifest(tmp_path).entry(ref(1), 10) is None and Manifest(tmp_path).entry(ref(1), 11)
    manifest.forget(ref(1), 11)
    assert "cyberpunk2077:1" not in Manifest(tmp_path).mods  # a mod without files is dropped
    manifest.forget(ref(1), 11)  # forgetting twice is harmless


# -- report ----------------------------------------------------------------------------------


def test_report_lists_order_tree_offsite_dlc_and_unavailable():
    text = render_report(make_plan(), {"cyberpunk2077:2": "downloaded", "cyberpunk2077:1": "failed: timeout"})
    assert text.index("Framework") < text.index("Big Mod")  # requirements first in install order
    assert "downloaded" in text and "failed: timeout" in text
    assert "## Off-site requirements" in text and "https://github.com/a/b" in text and "grab it" in text
    assert "Phantom Liberty (needed by Needs DLC)" in text
    assert "## Not available" in text and "cyberpunk2077/mods/7" in text
    assert "- Big Mod\n  - Framework" in text  # tree


def test_report_describes_collections():
    api = FakeApi(collections=[collection("abc", pin(1, name="First"), pin(2, optional=True, name="Second"),
                                          name="My Pack", revision=3, size=5 * 1_048_576)])
    plan = Resolver(api).resolve([coll_ref("abc")])
    text = render_report(plan, {})
    assert "## Collections" in text and "[My Pack](https://www.nexusmods.com/cyberpunk2077/collections/abc/revisions/3)" in text
    assert "revision 3, 2 mods, 5 MB" in text
    assert "(optional)" in text.split("## Dependency tree")[0]
    assert "- Collection: My Pack (revision 3)\n  - First\n  - Second" in text
    skipped = render_report(Resolver(api, skip_optional=True).resolve([coll_ref("abc")]), {})
    assert "1 optional skipped" in skipped


def test_write_report(tmp_path):
    path = write_report(tmp_path / "out", make_plan(), {})
    assert path.name == "requirements_report.md" and path.read_text(encoding="utf-8").startswith("# Requirements")
