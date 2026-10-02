"""What the packaged .exe runs. Unpackaged, the program starts with `python -m nexus_dl`; PyInstaller wants a script.

`NexusAutoDownloader.exe --self-test REPORT` is for tools/build_exe.py. It checks that what the program needs while it
runs really is inside the bundle, writes the outcome to the file REPORT (the exe has no console) and exits.
"""
from __future__ import annotations

import platform
import sys
import textwrap
import traceback
from pathlib import Path


def _python() -> str:
    where = "bundled in the exe" if getattr(sys, "frozen", False) else "not bundled: running from source"
    return f"{platform.python_version()}, {where}"


def _program() -> str:
    import nexus_dl.app  # noqa: F401  (loads every module of the program and the libraries they use)
    from nexus_dl import __version__

    return f"version {__version__}"


def _tk() -> str:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()  # fails if the Tcl/Tk script files are not in the bundle
    try:
        root.withdraw()
        ttk.Button(root)  # the themed widgets load script files of their own
        return f"Tk {root.tk.call('info', 'patchlevel')}"
    finally:
        root.destroy()


def _certificates() -> str:
    import ssl

    import certifi

    path = certifi.where()
    ssl.create_default_context(cafile=path)  # fails if the CA bundle that `requests` relies on is missing
    return Path(path).name


def _playwright() -> str:
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()  # starts the bundled Node driver, as the program does to reach the browser
    playwright.stop()
    return "driver started and stopped"


CHECKS = (
    ("Python", _python),
    ("Program", _program),
    ("Tk window toolkit", _tk),
    ("HTTPS certificates", _certificates),
    ("Playwright driver", _playwright),
)


def self_test(report: str) -> int:
    """Run every check, write one line per check (a traceback for a failure) to `report`; 0 if all passed."""
    lines: list[str] = []
    failed = False
    for label, probe in CHECKS:
        try:
            lines.append(f"ok    {label}: {probe()}")
        except Exception:
            failed = True
            lines.append(f"FAIL  {label}:\n" + textwrap.indent(traceback.format_exc().rstrip(), "      "))
    Path(report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if failed else 0


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        sys.exit(self_test(sys.argv[2]))
    from nexus_dl.app import main as run_program

    run_program()


if __name__ == "__main__":
    main()
