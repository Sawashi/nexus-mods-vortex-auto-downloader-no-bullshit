"""Build dist/NexusAutoDownloader.exe: the program, Python itself and the libraries in one Windows executable.

Start it through build.bat, which also finds (or installs) Python 3.14. The steps:
  1. a clean virtual environment, .build-venv, made from the Python that runs this script;
  2. requirements-build.txt installed into it: the program's libraries and PyInstaller;
  3. PyInstaller run on tools/launcher.py;
  4. the new exe started once with --self-test, to prove that Tk and Playwright's driver made it into the bundle.

PyInstaller copies the interpreter (python314.dll and the standard library) out of the Python that runs it. That is how
Python gets inside the exe: whoever runs the exe needs no Python of their own.
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "NexusAutoDownloader"
TITLE = "Nexus Mods Auto Downloader"
LAUNCHER = ROOT / "tools" / "launcher.py"
REQUIREMENTS = ROOT / "requirements-build.txt"
VENV = ROOT / ".build-venv"
BUILD = ROOT / "build"  # PyInstaller's work files and .spec, the version resource, the self-test report
DIST = ROOT / "dist"
SELF_TEST_SECONDS = 180  # a one-file exe first unpacks ~150 MB, and antivirus scans it while it is new


class BuildError(Exception):
    """A step failed; the message says what to do about it."""


def log(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def run(command: list, what: str) -> None:
    """Run a command with its output going straight to the console; stop the build if it fails."""
    result = subprocess.run([str(part) for part in command], cwd=ROOT)
    if result.returncode:
        raise BuildError(f"{what} failed (exit code {result.returncode}). The output above says why.")


# -- what goes into the exe ------------------------------------------------------------------


def app_version(root: Path = ROOT) -> str:
    """The version from nexus_dl/__init__.py, read as text: this script must not need the program's libraries."""
    text = (root / "nexus_dl" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise BuildError("Could not find __version__ in nexus_dl/__init__.py.")
    return match.group(1)


def numeric_version(version: str) -> tuple[int, ...]:
    """'0.1.0' -> (0, 1, 0, 0): a Windows file version is four numbers, so a suffix such as 'rc1' is dropped."""
    numbers = [int(found.group()) if (found := re.match(r"\d+", part)) else 0 for part in version.split(".")[:4]]
    return tuple(numbers + [0] * (4 - len(numbers)))


def version_file_text(version: str) -> str:
    """The Windows version resource in the text form PyInstaller reads (Explorer: Properties > Details)."""
    numbers = numeric_version(version)
    strings = {
        "FileDescription": TITLE,
        "FileVersion": version,
        "InternalName": APP_NAME,
        "LegalCopyright": "Unofficial tool, not affiliated with Nexus Mods",
        "OriginalFilename": f"{APP_NAME}.exe",
        "ProductName": TITLE,
        "ProductVersion": version,
    }
    entries = ",\n          ".join(f"StringStruct({key!r}, {value!r})" for key, value in strings.items())
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numbers},
    prodvers={numbers},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
          {entries}
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def pyinstaller_args(onedir: bool, console: bool, version_file: Path) -> list[str]:
    return [
        "--noconfirm",
        "--clean",
        "--noupx",  # UPX-packed programs set off antivirus scanners more often, and gain little here
        "--log-level", "WARN",
        "--name", APP_NAME,
        "--onedir" if onedir else "--onefile",
        "--console" if console else "--windowed",
        "--paths", str(ROOT),  # the launcher is in tools/; the program, nexus_dl, is one folder up
        "--collect-all", "playwright",  # its Node driver (node.exe and scripts) is data, which PyInstaller cannot see
        "--version-file", str(version_file),
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "pyinstaller"),
        "--specpath", str(BUILD),
        str(LAUNCHER),
    ]


def exe_path(onedir: bool) -> Path:
    return DIST / APP_NAME / f"{APP_NAME}.exe" if onedir else DIST / f"{APP_NAME}.exe"


def installer_version(root: Path = ROOT) -> str | None:
    """The version of the python-X.Y.Z-amd64.exe installer that lies next to build.bat, if there is one."""
    versions = [
        found.group(1)
        for path in root.glob("python-*-amd64.exe")
        if (found := re.fullmatch(r"python-(\d+\.\d+\.\d+)-amd64\.exe", path.name))
    ]
    return max(versions, key=lambda text: tuple(int(number) for number in text.split(".")), default=None)


# -- the steps -------------------------------------------------------------------------------


def check_environment() -> None:
    if sys.platform != "win32":
        raise BuildError("This builds the Windows .exe, so it has to run on Windows.")
    if sys.version_info < (3, 10):
        raise BuildError(f"Python 3.10 or newer is needed and this is {platform.python_version()}. Use build.bat.")


def same_folder(first: str | Path, second: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def venv_is_current(python: Path) -> bool:
    """Does the build environment work and come from the Python that runs this script?"""
    if not python.is_file():
        return False
    try:
        result = subprocess.run(
            [str(python), "-c", "import sys; print(sys.base_prefix)"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and same_folder(result.stdout.strip(), sys.base_prefix)


def ensure_venv() -> Path:
    """The build environment's python.exe; the environment is made again when its Python is not this one."""
    python = VENV / "Scripts" / "python.exe"
    if not venv_is_current(python):
        if VENV.exists():
            log(f"{VENV.name} was made with another Python (or the folder moved): making it again")
            shutil.rmtree(VENV)
        log(f"Making the build environment ({VENV.name})")
        run([sys.executable, "-m", "venv", VENV], "Creating the build environment")
    return python


def install_requirements(python: Path) -> None:
    log("Installing the libraries and PyInstaller (the first time takes a minute)")
    run(
        [python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", REQUIREMENTS],
        "Installing the requirements",
    )


def remove_old_output(onedir: bool) -> None:
    """Delete the previous result first, so a build that fails halfway cannot leave an old exe looking new."""
    target = DIST / APP_NAME if onedir else DIST / f"{APP_NAME}.exe"
    try:
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    except OSError as exc:
        raise BuildError(f"Could not remove the old {target} ({exc}). Close it if it is running.") from exc


def run_pyinstaller(python: Path, onedir: bool, console: bool) -> Path:
    BUILD.mkdir(exist_ok=True)
    version_file = BUILD / "version_info.txt"
    version_file.write_text(version_file_text(app_version()), encoding="utf-8")
    remove_old_output(onedir)
    log("Packing the program with PyInstaller (a minute or two)")
    run([python, "-m", "PyInstaller", *pyinstaller_args(onedir, console, version_file)], "PyInstaller")
    exe = exe_path(onedir)
    if not exe.is_file():
        raise BuildError(f"PyInstaller finished but {exe} is not there. An antivirus program may have removed it.")
    return exe


def self_test(exe: Path) -> bool:
    """Start the new exe in its self-test mode (see tools/launcher.py); True if every check passed."""
    report = BUILD / "self-test.txt"
    report.unlink(missing_ok=True)
    log("Testing the exe: it starts Tk and Playwright's driver once, then exits")
    try:
        process = subprocess.Popen([str(exe), "--self-test", str(report)])
    except OSError as exc:
        raise BuildError(
            f"Windows would not start {exe.name} ({exc}). If it says a virus was found, your antivirus program is "
            "blocking the new exe (a false alarm for programs made with PyInstaller): allow the dist folder, build again."
        ) from exc
    try:
        code = process.wait(timeout=SELF_TEST_SECONDS)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)  # and what it unpacked
        print(f"The exe was still running after {SELF_TEST_SECONDS} s and was stopped.")
        code = None
    print(textwrap.indent(report.read_text(encoding="utf-8").rstrip() if report.is_file() else "No report.", "    "))
    return code == 0 and report.is_file()


def size_of(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.is_dir() else path.stat().st_size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Build {APP_NAME}.exe: the program with Python bundled inside it.")
    parser.add_argument("--onedir", action="store_true", help="a folder with the exe in it, not one file: starts faster")
    parser.add_argument("--console", action="store_true", help="keep a console window, to see why a build will not start")
    args = parser.parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        reconfigure(errors="replace")  # a console that cannot show some character must not stop the build
    version = platform.python_version()
    try:
        check_environment()
        log(f"Bundling Python {version} ({struct.calcsize('P') * 8}-bit, from {sys.base_prefix})")
        offered = installer_version()
        if offered and offered != version:
            print(
                f"Note: the installer next to build.bat is Python {offered}, but Python {version} is the one installed "
                "here, so that is what gets bundled. Run the installer once to upgrade it, then build again."
            )
        python = ensure_venv()
        install_requirements(python)
        exe = run_pyinstaller(python, args.onedir, args.console)
        if not self_test(exe):
            raise BuildError(f"{exe.name} was built but its self-test failed (see above): do not hand it out.")
    except BuildError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    result = exe.parent if args.onedir else exe
    print(f"\nDone: {result} ({size_of(result) / 1_048_576:.0f} MB, Python {version} inside)")
    print("It needs no Python. It still needs Brave, Chrome or Edge on the computer where it runs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
