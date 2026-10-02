import json
from dataclasses import fields

from nexus_dl.resolver import Resolver
from nexus_dl.store import Config, Manifest, load_config, render_report, save_config, write_report

from .helpers import FakeApi, coll_ref, collection, file, mod, need, offsite, pin, ref


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

    (tmp_path / "mod-1.zip").write_bytes(b"12345")
    manifest.mark_done(ref(1), "Mod One", f, "mod-1.zip", 5, [ref(9)])

    reloaded = Manifest(tmp_path)  # persisted to disk
    assert reloaded.is_done(ref(1), f)
    assert reloaded.entry(ref(1), 10)["requiredBy"] == ["cyberpunk2077:9"]
    assert not reloaded.is_done(ref(1), file(11))  # a different (newer) file is not "done"


def test_manifest_ignores_missing_or_changed_files(tmp_path):
    f = file(10, size=5)
    manifest = Manifest(tmp_path)
    (tmp_path / "mod-1.zip").write_bytes(b"12345")
    manifest.mark_done(ref(1), "Mod One", f, "mod-1.zip", 5, [])

    (tmp_path / "mod-1.zip").write_bytes(b"123")  # truncated afterwards
    assert not manifest.is_done(ref(1), f)
    (tmp_path / "mod-1.zip").unlink()
    assert not manifest.is_done(ref(1), f)


def test_manifest_tolerates_garbage(tmp_path):
    (tmp_path / Manifest.FILENAME).write_text("[]", encoding="utf-8")
    assert Manifest(tmp_path).mods == {}


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
