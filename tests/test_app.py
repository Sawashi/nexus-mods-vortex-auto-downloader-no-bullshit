"""The window itself: the new buttons and the per-file actions. Skipped where Tk or a display is not available."""
import threading
import time
from types import SimpleNamespace

import pytest

tk = pytest.importorskip("tkinter")

from nexus_dl import app as app_module  # noqa: E402
from nexus_dl.job import Row, Work  # noqa: E402
from nexus_dl.resolver import PlanItem  # noqa: E402
from nexus_dl.store import Config, Existing, Manifest  # noqa: E402

from .helpers import file, make_zip, ref  # noqa: E402

ONE, TWO, TOOL = "cyberpunk2077:1#10", "cyberpunk2077:2#20", "ext:0"


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A real App whose settings, usage file and log live in a temp folder and whose message boxes never open."""
    monkeypatch.setattr(app_module, "load_config", lambda: Config(download_dir=str(tmp_path / "mods")))
    monkeypatch.setattr(app_module, "save_config", lambda *args, **kwargs: None)  # never touch the real settings
    monkeypatch.setattr(app_module, "USAGE_PATH", tmp_path / "usage.json")
    monkeypatch.setattr(app_module, "LOG_PATH", tmp_path / "last_run.log")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    dialogs, answers = [], {"yes": True}
    box = app_module.messagebox
    monkeypatch.setattr(box, "askyesno", lambda title, text, **kw: dialogs.append(("ask", title, text)) or answers["yes"])
    monkeypatch.setattr(box, "showinfo", lambda title, text, **kw: dialogs.append(("info", title, text)))
    monkeypatch.setattr(box, "showerror", lambda title, text, **kw: dialogs.append(("error", title, text)))
    window = app_module.App(root)
    window.dialogs, window.answers = dialogs, answers
    yield window
    root.destroy()


def pump(window, until, timeout=5.0):
    """Let Tk and the worker threads run until `until()` is true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        window.root.update()
        if until():
            return True
        time.sleep(0.01)
    return False


def populate(window, tmp_path, ids=(1, 2)):
    """Fill the table the way a finished run would: real files in the folder, their manifest entries and rows."""
    folder = tmp_path / "mods"
    folder.mkdir(exist_ok=True)
    manifest = Manifest(folder)
    rows, works = [], {}
    for mod_id in ids:
        info = file(mod_id * 10)
        name = f"{mod_id}-{info.file_id}.zip"
        path = make_zip(folder / name)
        manifest.mark_done(ref(mod_id), f"mod {mod_id}", info, name, path.stat().st_size, [])
        item = PlanItem(ref(mod_id), f"mod {mod_id}", "1.0", [info])
        work = Work(item, info, Existing("ok", path), True)
        works[work.row_id] = work
        rows.append(Row(work.row_id, item.name, "Main file v1.0", 1000, "already downloaded", work=work))
    rows.append(Row(TOOL, "Some tool", "off-site", None, "download it yourself", "https://example.org/tool"))
    window._table_dir = folder
    window.folder.set(str(folder))
    window.show_plan(rows)
    return folder, works


def left_in(folder):
    return sorted(p.name for p in folder.iterdir() if p.name != Manifest.FILENAME)


def state(button):
    return str(button.cget("state"))


def asked(window):
    return [dialog for dialog in window.dialogs if dialog[0] == "ask"]


def log_text(window):
    return window.log_box.get("1.0", "end")


# -- clear log ---------------------------------------------------------------------------------------


def test_the_clear_log_button_empties_the_log(app):
    app.append_log("one")
    app.append_log("two")
    assert "one" in log_text(app) and "two" in log_text(app)
    app.clear_log_button.invoke()
    assert log_text(app).strip() == ""
    app.append_log("three")  # and it keeps working afterwards
    assert "three" in log_text(app)


# -- delete all downloaded files -----------------------------------------------------------------------


def test_delete_all_removes_only_what_the_tool_downloaded(app, tmp_path):
    folder, _ = populate(app, tmp_path)
    (folder / "vacation.jpg").write_bytes(b"precious")
    app.delete_all_button.invoke()
    assert pump(app, lambda: not app._cleaning and app.tree.set(ONE, "status") == "deleted")
    assert left_in(folder) == ["vacation.jpg"] and Manifest(folder).mods == {}
    assert app.tree.set(TWO, "status") == "deleted" and app.tree.set(TOOL, "status") == "download it yourself"
    (_, title, text), = asked(app)
    assert title == "Delete all downloaded files" and "2 file(s)" in text and str(folder) in text
    assert "Deleted 2 files" in log_text(app)


def test_delete_all_does_nothing_when_you_say_no(app, tmp_path):
    folder, _ = populate(app, tmp_path)
    app.answers["yes"] = False
    app.delete_all()
    assert not app._cleaning and left_in(folder) == ["1-10.zip", "2-20.zip"]
    assert len(Manifest(folder).tracked()) == 2


def test_delete_all_says_so_when_nothing_is_recorded(app, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    app.folder.set(str(empty))
    app.delete_all()
    assert asked(app) == [] and any("nothing to delete" in text for kind, _title, text in app.dialogs if kind == "info")


def test_delete_all_needs_an_existing_folder(app, tmp_path):
    app.folder.set("")
    app.delete_all()
    app.folder.set(str(tmp_path / "does-not-exist"))
    app.delete_all()
    assert [kind for kind, *_ in app.dialogs] == ["error", "error"]


# -- quick delete of chosen entries --------------------------------------------------------------------


def test_quick_delete_of_one_entry_asks_nothing(app, tmp_path):
    folder, _ = populate(app, tmp_path)
    app.tree.selection_set(ONE)
    app.delete_selected()
    assert pump(app, lambda: not app._cleaning and app.tree.set(ONE, "status") == "deleted")
    assert asked(app) == [] and left_in(folder) == ["2-20.zip"]
    assert app.tree.set(TWO, "status") == "already downloaded"  # the other entry is untouched
    assert Manifest(folder).entry(ref(1), 10) is None and Manifest(folder).entry(ref(2), 20) is not None
    assert "Deleted 1-10.zip" in log_text(app)


def test_the_delete_key_deletes_the_selected_entry(app, tmp_path):
    folder, _ = populate(app, tmp_path)
    assert app.tree.bind("<Delete>")  # the key is wired to the table
    app.tree.selection_set(TWO)
    app.delete_selected(SimpleNamespace())  # what the key press calls
    assert pump(app, lambda: not app._cleaning) and left_in(folder) == ["1-10.zip"]


def test_deleting_several_entries_asks_first(app, tmp_path):
    folder, _ = populate(app, tmp_path)
    app.tree.selection_set((ONE, TWO))
    app.root.update()  # lets the "selection changed" event enable the buttons, as it does for a real click
    app.answers["yes"] = False
    app.delete_button.invoke()
    assert len(asked(app)) == 1 and "2 selected entries" in asked(app)[0][2]
    assert left_in(folder) == ["1-10.zip", "2-20.zip"]  # declined: nothing happened
    app.answers["yes"] = True
    app.delete_button.invoke()
    assert pump(app, lambda: not app._cleaning and left_in(folder) == [])


def test_an_entry_with_no_file_on_disk_is_reported_not_an_error(app, tmp_path):
    folder, _ = populate(app, tmp_path)
    (folder / "1-10.zip").unlink()
    app.tree.selection_set(ONE)
    app.delete_selected()
    assert pump(app, lambda: not app._cleaning and app.tree.set(ONE, "status") == "not on disk")


def test_a_file_that_cannot_be_deleted_is_reported_and_stays_recorded(app, tmp_path, monkeypatch):
    folder, _ = populate(app, tmp_path)
    real_unlink = type(folder).unlink
    monkeypatch.setattr(type(folder), "unlink", lambda self, *a, **k: (_ for _ in ()).throw(
        PermissionError(13, "in use")) if self.name == "1-10.zip" else real_unlink(self, *a, **k))
    app.tree.selection_set(ONE)
    app.delete_selected()
    assert pump(app, lambda: not app._cleaning)
    assert app.tree.set(ONE, "status") == "could not delete: in use"
    assert (folder / "1-10.zip").exists() and Manifest(folder).entry(ref(1), 10) is not None


# -- which buttons can be used when ------------------------------------------------------------------------


def test_the_per_file_buttons_follow_the_selection(app, tmp_path):
    populate(app, tmp_path)
    app.root.update()
    assert state(app.delete_button) == state(app.again_button) == "disabled"  # nothing selected
    app.tree.selection_set(ONE)
    app.root.update()
    assert state(app.delete_button) == state(app.again_button) == "normal"
    app.tree.selection_set(TOOL)  # an off-site row has no file
    app.root.update()
    assert state(app.delete_button) == state(app.again_button) == "disabled"
    assert state(app.delete_all_button) == "normal"


def test_nothing_can_be_deleted_or_redownloaded_while_something_is_running(app, tmp_path):
    folder, _ = populate(app, tmp_path)
    app.tree.selection_set(ONE)
    app._job_running = True
    app._refresh_controls()
    assert {state(b) for b in (app.start_button, app.delete_button, app.again_button, app.delete_all_button,
                               app.open_button, app.check_button)} == {"disabled"}
    assert state(app.stop_button) == "normal"
    app.delete_selected()  # e.g. through the Delete key or the menu
    app.delete_all()
    app.download_selected_again()
    assert left_in(folder) == ["1-10.zip", "2-20.zip"] and app.dialogs == []
    assert "Another action is in progress" in log_text(app)


def test_the_table_is_forgotten_when_a_new_run_starts(app, tmp_path):
    populate(app, tmp_path)
    assert set(app.entries) == {ONE, TWO}  # only file rows can be acted on, not the off-site row
    app.clear_table()
    assert app.entries == {}


# -- download again ----------------------------------------------------------------------------------


class FakeJob:
    created: list = []
    release = threading.Event()

    def __init__(self, config, links, ui, stop, budget, redo=None):
        FakeJob.created.append({"config": config, "links": links, "redo": redo, "budget": budget})
        self.ui = ui

    def run(self):
        FakeJob.release.wait(5)
        self.ui.finished("Done: 1 downloaded, 0 already there, 0 failed.")


def test_download_again_starts_a_job_for_the_selected_entries_and_leaves_the_table_alone(app, tmp_path, monkeypatch):
    FakeJob.created, FakeJob.release = [], threading.Event()
    monkeypatch.setattr(app_module, "Job", FakeJob)
    folder, works = populate(app, tmp_path)
    app.folder.set("")  # even if the folder box is changed afterwards, the table's folder is used
    app.tree.selection_set(ONE)
    app.root.update()  # lets the "selection changed" event enable the buttons, as it does for a real click

    app.again_button.invoke()

    (created,) = FakeJob.created
    assert created["redo"] == [works[ONE]] and created["links"] == [] and created["budget"] is app.budget
    assert created["config"].download_dir == str(folder)
    assert app._job_running and state(app.again_button) == state(app.start_button) == "disabled"
    assert set(app.tree.get_children()) == {ONE, TWO, TOOL}  # nothing was cleared or re-scanned

    FakeJob.release.set()
    assert pump(app, lambda: not app._job_running)
    assert not [d for d in app.dialogs if d[0] == "info"]  # a quick action does not pop up a message
    assert app.overall_text.get().startswith("Done: 1 downloaded")
    assert state(app.again_button) == "normal" and state(app.start_button) == "normal"


def test_download_again_with_several_entries_selected_passes_them_all(app, tmp_path, monkeypatch):
    FakeJob.created, FakeJob.release = [], threading.Event()
    FakeJob.release.set()
    monkeypatch.setattr(app_module, "Job", FakeJob)
    _, works = populate(app, tmp_path)
    app.tree.selection_set((ONE, TWO, TOOL))  # the off-site row is ignored
    app.download_selected_again()
    assert pump(app, lambda: not app._job_running)
    assert FakeJob.created[0]["redo"] == [works[ONE], works[TWO]]


# -- the right-click menu ------------------------------------------------------------------------------


def test_right_clicking_a_file_row_selects_it_and_offers_both_actions(app, tmp_path, monkeypatch):
    populate(app, tmp_path)
    popped = []
    monkeypatch.setattr(app.tree, "identify_row", lambda y: TWO)
    monkeypatch.setattr(app.menu, "tk_popup", lambda x, y: popped.append((x, y)))
    app._on_right_click(SimpleNamespace(y=5, x_root=30, y_root=40))
    assert app.tree.selection() == (TWO,) and popped == [(30, 40)]
    assert [app.menu.entrycget(label, "state") for label in ("Download again", "Delete file", "Open link")] == [
        "normal", "normal", "disabled"]


def test_right_clicking_an_off_site_row_offers_only_its_link(app, tmp_path, monkeypatch):
    populate(app, tmp_path)
    monkeypatch.setattr(app.tree, "identify_row", lambda y: TOOL)
    monkeypatch.setattr(app.menu, "tk_popup", lambda x, y: None)
    app._on_right_click(SimpleNamespace(y=5, x_root=1, y_root=1))
    assert [app.menu.entrycget(label, "state") for label in ("Download again", "Delete file", "Open link")] == [
        "disabled", "disabled", "normal"]


def test_right_clicking_empty_space_does_nothing(app, tmp_path, monkeypatch):
    populate(app, tmp_path)
    popped = []
    monkeypatch.setattr(app.tree, "identify_row", lambda y: "")
    monkeypatch.setattr(app.menu, "tk_popup", lambda x, y: popped.append(1))
    app._on_right_click(SimpleNamespace(y=999, x_root=1, y_root=1))
    assert popped == []
