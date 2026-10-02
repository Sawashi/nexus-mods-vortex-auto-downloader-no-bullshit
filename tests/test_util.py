import json
from pathlib import Path

import pytest

from nexus_dl.util import hide_home, write_json


def test_hide_home_replaces_the_home_folder_in_any_slash_style_and_case(monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("C:/Users/SomeUser")))
    text = (r"saved to C:\Users\SomeUser\Downloads\mods and c:/users/someuser/AppData/Local/x "
            r"but not C:\Users\SomeUserOther\x? and keep D:\games")
    cleaned = hide_home(text)
    assert "SomeUser" not in cleaned.replace("SomeUserOther", "")
    assert cleaned.startswith(r"saved to ~\Downloads\mods and ~/AppData/Local/x")
    assert r"D:\games" in cleaned


def test_hide_home_leaves_other_text_alone(monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("C:/Users/SomeUser")))
    assert hide_home("nothing personal here") == "nothing personal here"


def test_write_json_is_atomic_and_creates_folders(tmp_path):
    target = tmp_path / "deep" / "file.json"
    write_json(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    with pytest.raises(TypeError):
        write_json(target, {"bad": object()})  # not serialisable: the old file stays intact, no temp files left
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert [p.name for p in target.parent.iterdir()] == ["file.json"]
