import importlib.util
import sys
from pathlib import Path

import pytest

import nexus_dl

ROOT = Path(__file__).resolve().parent.parent


def load_tool(name: str):
    """Import a script of tools/ by its path: they are build scripts, not part of the package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_exe = load_tool("build_exe")
launcher = load_tool("launcher")


# -- tools/build_exe.py ----------------------------------------------------------------------


def test_the_version_is_read_from_the_package_without_importing_it():
    assert build_exe.app_version() == nexus_dl.__version__


@pytest.mark.parametrize(
    "text, expected",
    [
        ("0.1.0", (0, 1, 0, 0)),
        ("2.10.3.4", (2, 10, 3, 4)),
        ("1.2", (1, 2, 0, 0)),
        ("0.2.0rc1", (0, 2, 0, 0)),
        ("1.2.3.4.5", (1, 2, 3, 4)),
        ("1.x.3", (1, 0, 3, 0)),  # a part that is not a number keeps its place instead of shifting the others
    ],
)
def test_a_windows_file_version_is_always_four_numbers(text, expected):
    assert build_exe.numeric_version(text) == expected


def test_the_version_resource_is_one_python_expression_that_names_the_program():
    text = build_exe.version_file_text("0.1.0")
    compile(text, "version_info.txt", "eval")  # PyInstaller eval()s this file
    assert "filevers=(0, 1, 0, 0)" in text
    assert "StringStruct('FileVersion', '0.1.0')" in text
    assert "StringStruct('OriginalFilename', 'NexusAutoDownloader.exe')" in text


def test_pyinstaller_bundles_playwrights_driver_and_runs_the_launcher():
    args = build_exe.pyinstaller_args(onedir=False, console=False, version_file=Path("v.txt"))
    assert "--onefile" in args and "--windowed" in args
    assert args[args.index("--collect-all") + 1] == "playwright"  # the Node driver is data, invisible to the analysis
    assert args[args.index("--paths") + 1] == str(build_exe.ROOT)  # so that `nexus_dl` can be found from tools/
    assert args[-1] == str(build_exe.LAUNCHER)
    assert build_exe.LAUNCHER.is_file()


def test_the_options_choose_a_folder_and_a_console():
    args = build_exe.pyinstaller_args(onedir=True, console=True, version_file=Path("v.txt"))
    assert "--onedir" in args and "--onefile" not in args
    assert "--console" in args and "--windowed" not in args


def test_the_exe_is_where_the_build_says():
    assert build_exe.exe_path(onedir=False) == build_exe.DIST / "NexusAutoDownloader.exe"
    assert build_exe.exe_path(onedir=True) == build_exe.DIST / "NexusAutoDownloader" / "NexusAutoDownloader.exe"


def test_the_newest_installer_wins_by_number_not_by_text(tmp_path):
    for name in ("python-3.14.7-amd64.exe", "python-3.14.10-amd64.exe", "python-3.13.1-amd64.exe", "python-3.14.99.exe"):
        (tmp_path / name).touch()
    assert build_exe.installer_version(tmp_path) == "3.14.10"  # as text "3.14.7" would sort after "3.14.10"
    assert build_exe.installer_version(tmp_path / "nowhere") is None


# -- tools/launcher.py -----------------------------------------------------------------------


def boom():
    raise RuntimeError("the driver is missing")


def test_self_test_writes_a_line_per_check_and_fails_when_one_does(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "CHECKS", (("Fine", lambda: "yes"), ("Broken", boom), ("After", lambda: "still ran")))
    report = tmp_path / "report.txt"
    assert launcher.self_test(str(report)) == 1
    text = report.read_text(encoding="utf-8")
    assert "ok    Fine: yes" in text
    assert "FAIL  Broken:" in text and "RuntimeError: the driver is missing" in text
    assert "ok    After: still ran" in text  # one failure does not hide the rest


def test_self_test_passes_when_every_check_does(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "CHECKS", (("Fine", lambda: "yes"),))
    report = tmp_path / "report.txt"
    assert launcher.self_test(str(report)) == 0
    assert report.read_text(encoding="utf-8") == "ok    Fine: yes\n"


def test_the_launcher_starts_the_program_when_given_no_arguments(monkeypatch):
    started = []
    monkeypatch.setattr(sys, "argv", ["NexusAutoDownloader.exe"])
    monkeypatch.setattr("nexus_dl.app.main", lambda: started.append(True))
    launcher.main()
    assert started == [True]


def test_the_launcher_exits_with_the_self_test_result_and_starts_nothing(monkeypatch, tmp_path):
    report = str(tmp_path / "report.txt")
    seen = []
    monkeypatch.setattr(sys, "argv", ["NexusAutoDownloader.exe", "--self-test", report])
    monkeypatch.setattr(launcher, "self_test", lambda path: seen.append(path) or 7)
    monkeypatch.setattr("nexus_dl.app.main", lambda: pytest.fail("the program must not start during the self-test"))
    with pytest.raises(SystemExit) as stopped:
        launcher.main()
    assert stopped.value.code == 7
    assert seen == [report]
