"""Tkinter front end: collects the settings, starts a Job and shows what it reports."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .browser import LOGIN_URL, BrowserError, BrowserSession, ensure_browser
from .budget import OFFICIAL_DAILY, OFFICIAL_HOURLY, RequestBudget
from .cleanup import Removal, delete_everything, delete_files, summarize
from .job import Job, Row, Work
from .site_flow import SiteFlow
from .store import APP_DIR, USAGE_PATH, Config, Manifest, load_config, save_config
from .urls import parse_links
from .util import format_size, hide_home

LOG_PATH = APP_DIR / "last_run.log"
PLACEHOLDER = (
    "Paste Nexus links here, one per line:\n"
    "  a mod:         https://www.nexusmods.com/<game>/mods/<id>\n"
    "  a collection:  https://www.nexusmods.com/<game>/collections/<slug>"
)
FOOTER = (
    "Use responsibly: Nexus Mods' terms prohibit automated downloading far above normal use. This tool "
    "downloads one file at a time and waits out the site's countdown. Not affiliated with Nexus Mods."
)
OK_COLOR, WARN_COLOR, STOP_COLOR = "#2e9e4f", "#c47f00", "#d03b3b"
MAX_LISTED_DELETIONS = 20  # more than this and the log only gets the summary


class Bridge:
    """Runs callables on the Tk thread on behalf of worker threads."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.closing = threading.Event()
        self._queue: queue.Queue = queue.Queue()
        root.after(80, self._pump)

    def post(self, fn, *args) -> None:
        self._queue.put((fn, args, None))

    def call(self, fn, *args):
        """Run `fn` on the Tk thread, wait for it and return its result (None if the app is closing)."""
        done, box = threading.Event(), []
        self._queue.put((fn, args, (done, box)))
        while not done.wait(0.2):
            if self.closing.is_set():
                return None
        return box[0] if box else None

    def _pump(self) -> None:
        try:
            while True:
                fn, args, reply = self._queue.get_nowait()
                try:
                    value = fn(*args)
                except Exception:  # a UI hiccup must never kill the pump
                    value = None
                if reply:
                    reply[1].append(value)
                    reply[0].set()
        except queue.Empty:
            pass
        self.root.after(80, self._pump)


class JobUi:
    """The Ui the worker sees: everything is forwarded to the Tk thread."""

    def __init__(self, app: "App"):
        self.app = app
        self.bridge = app.bridge

    def log(self, text: str) -> None:
        self.bridge.post(self.app.append_log, text)

    def show_plan(self, rows: list[Row]) -> None:
        self.bridge.post(self.app.show_plan, rows)

    def set_status(self, row_id: str, status: str) -> None:
        self.bridge.post(self.app.set_row_status, row_id, status)

    def set_progress(self, done: int, total: int, text: str) -> None:
        self.bridge.post(self.app.set_overall, done, total, text)

    def set_file_progress(self, received: int, total: int) -> None:
        self.bridge.post(self.app.set_file_bar, received, total)

    def set_login(self, logged_in: bool | None, text: str) -> None:
        self.bridge.post(self.app.set_login, logged_in, text)

    def confirm(self, title: str, text: str) -> bool:
        return bool(self.bridge.call(messagebox.askyesno, title, text))

    def finished(self, summary: str) -> None:
        self.bridge.post(self.app.on_finished, summary)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.config = load_config()
        try:
            save_config(self.config)  # rewrites the file with only the current settings (drops old leftovers)
        except OSError:
            pass
        self.budget = RequestBudget(
            USAGE_PATH, hourly=self.config.request_limit_hour, daily=self.config.request_limit_day
        )
        self.stop = threading.Event()
        self.bridge = Bridge(root)
        self.worker: threading.Thread | None = None
        self.row_urls: dict[str, str] = {}
        self.entries: dict[str, Work] = {}  # table rows that are a file: what Delete / Download again act on
        self._table_dir: Path | None = None  # the folder the table was filled for
        self._row_count = 0
        self._placeholder_shown = False
        self._announce = True  # pop up a message when a run ends (not for quick per-file actions)
        self._job_running = self._stopping = self._cleaning = self._checking = False
        root.title("Nexus Mods Auto Downloader")
        root.geometry("1000x940")
        root.minsize(860, 800)
        self._build()
        self._refresh_usage()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # -- layout --------------------------------------------------------------------------------

    def _build(self) -> None:
        cfg = self.config
        self.folder = tk.StringVar(value=cfg.download_dir)
        self.delay = tk.IntVar(value=cfg.delay_seconds)
        self.start_timeout = tk.IntVar(value=cfg.start_timeout_seconds)
        self.include_requirements = tk.BooleanVar(value=cfg.include_requirements)
        self.skip_existing = tk.BooleanVar(value=cfg.skip_existing)
        self.skip_optional = tk.BooleanVar(value=cfg.skip_optional)
        self.scan_only = tk.BooleanVar(value=cfg.scan_only)
        self.confirm = tk.BooleanVar(value=cfg.confirm)
        self.all_main = tk.BooleanVar(value=cfg.all_main)
        self.login_text = tk.StringVar(value="Login not checked yet")
        self.overall_text = tk.StringVar(value="")
        self.usage_text = tk.StringVar(value="")

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        login = ttk.LabelFrame(outer, text="1. Nexus Mods login", padding=8)
        login.pack(fill="x")
        self.login_dot = tk.Label(login, text="●", fg="gray", font=("Segoe UI", 14))
        self.login_dot.pack(side="left")
        ttk.Label(login, textvariable=self.login_text).pack(side="left", padx=6)
        self.check_button = ttk.Button(login, text="Check login", command=self.check_login)
        self.check_button.pack(side="right")
        self.open_button = ttk.Button(login, text="Open browser / log in", command=self.open_browser)
        self.open_button.pack(side="right", padx=6)

        mods = ttk.LabelFrame(outer, text="2. Mods and collections", padding=8)
        mods.pack(fill="x", pady=(8, 0))
        self.links = tk.Text(mods, height=4, wrap="none")
        self.links.tag_configure("placeholder", foreground="gray")
        self.links.bind("<FocusIn>", self._hide_placeholder)
        self.links.bind("<FocusOut>", self._show_placeholder)
        self.links.pack(fill="x")
        self._show_placeholder()
        folder_row = ttk.Frame(mods)
        folder_row.pack(fill="x", pady=(6, 0))
        ttk.Label(folder_row, text="Download folder:").pack(side="left")
        ttk.Entry(folder_row, textvariable=self.folder).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(folder_row, text="Browse...", command=self.browse).pack(side="left")

        opts = ttk.LabelFrame(outer, text="3. Options", padding=8)
        opts.pack(fill="x", pady=(8, 0))
        checks = [
            ("Also download requirements (recursively)", self.include_requirements),
            ("Skip files already downloaded", self.skip_existing),
            ("Ask me before downloading", self.confirm),
            ("Skip optional mods of collections", self.skip_optional),
            ("Scan only (list what is needed, download nothing)", self.scan_only),
            ("All main files, not just the primary one", self.all_main),
        ]
        for index, (text, var) in enumerate(checks):
            ttk.Checkbutton(opts, text=text, variable=var).grid(row=index // 2, column=index % 2, sticky="w", padx=4)
        timing = ttk.Frame(opts)
        timing.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(timing, text="Pause between downloads (s):").pack(side="left")
        ttk.Spinbox(timing, from_=0, to=600, width=5, textvariable=self.delay).pack(side="left", padx=(4, 16))
        ttk.Label(timing, text="Give up if a download does not start within (s):").pack(side="left")
        ttk.Spinbox(timing, from_=20, to=900, width=5, textvariable=self.start_timeout).pack(side="left", padx=4)
        self.usage_label = ttk.Label(opts, textvariable=self.usage_text, wraplength=920, foreground="gray")
        self.usage_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(10, 0))
        self.start_button = ttk.Button(actions, text="Download", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="Stop", command=self.request_stop, state="disabled")
        self.stop_button.pack(side="left", padx=6)
        ttk.Label(actions, textvariable=self.overall_text).pack(side="left", padx=10)
        bars = ttk.Frame(outer)
        bars.pack(fill="x", pady=(6, 0))
        bars.columnconfigure(1, weight=1)
        ttk.Label(bars, text="All mods").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.overall_bar = ttk.Progressbar(bars, maximum=1, value=0)
        self.overall_bar.grid(row=0, column=1, sticky="we")
        ttk.Label(bars, text="Current file").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(3, 0))
        self.file_bar = ttk.Progressbar(bars, maximum=1, value=0)
        self.file_bar.grid(row=1, column=1, sticky="we", pady=(3, 0))

        # The footer is packed before the expanding panes so it can never be pushed off the window.
        ttk.Label(outer, text=FOOTER, wraplength=940, foreground="gray").pack(side="bottom", fill="x", pady=(6, 0))
        panes = ttk.PanedWindow(outer, orient="vertical")
        panes.pack(fill="both", expand=True, pady=(8, 0))

        table = ttk.Frame(panes)
        panes.add(table, weight=3)
        table_header = ttk.Frame(table)
        table_header.pack(fill="x", pady=(0, 3))
        ttk.Label(table_header, text="Files (select rows, or right-click, to delete or download them again)").pack(
            side="left")
        self.delete_all_button = ttk.Button(
            table_header, text="Delete all downloaded files", command=self.delete_all)
        self.delete_all_button.pack(side="right")
        self.delete_button = ttk.Button(
            table_header, text="Delete file", command=self.delete_selected, state="disabled")
        self.delete_button.pack(side="right", padx=6)
        self.again_button = ttk.Button(
            table_header, text="Download again", command=self.download_selected_again, state="disabled")
        self.again_button.pack(side="right")
        table_body = ttk.Frame(table)
        table_body.pack(fill="both", expand=True)
        columns = ("n", "mod", "file", "size", "status")
        self.tree = ttk.Treeview(table_body, columns=columns, show="headings", height=6)
        for name, title, width in (("n", "#", 36), ("mod", "Mod", 260), ("file", "File", 260),
                                   ("size", "Size", 80), ("status", "Status", 220)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, anchor="w", stretch=name in ("mod", "file", "status"))
        scroll = ttk.Scrollbar(table_body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.open_row_link)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._refresh_controls())
        self.tree.bind("<Delete>", self.delete_selected)
        self.tree.bind("<Button-3>", self._on_right_click)
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Download again", command=self.download_selected_again)
        self.menu.add_command(label="Delete file", command=self.delete_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Open link", command=self.open_row_link)

        log_frame = ttk.Frame(panes)
        panes.add(log_frame, weight=2)
        log_header = ttk.Frame(log_frame)
        log_header.pack(fill="x", pady=(0, 3))
        ttk.Label(log_header, text="Log").pack(side="left")
        self.clear_log_button = ttk.Button(log_header, text="Clear log", command=self.clear_log)
        self.clear_log_button.pack(side="right")
        self.log_box = scrolledtext.ScrolledText(log_frame, height=8, wrap="word", state="disabled")
        self.log_box.pack(fill="both", expand=True)

    # -- the links box and its placeholder ------------------------------------------------------

    def links_text(self) -> str:
        return "" if self._placeholder_shown else self.links.get("1.0", "end")

    def _show_placeholder(self, _event=None) -> None:
        if self._placeholder_shown or self.links.get("1.0", "end").strip():
            return
        self.links.insert("1.0", PLACEHOLDER, "placeholder")
        self._placeholder_shown = True

    def _hide_placeholder(self, _event=None) -> None:
        if self._placeholder_shown:
            self.links.delete("1.0", "end")
            self._placeholder_shown = False

    # -- settings ------------------------------------------------------------------------------

    @staticmethod
    def _int(var: tk.IntVar, default: int) -> int:
        try:
            return max(0, int(var.get()))
        except (tk.TclError, ValueError):
            return default

    def collect_config(self) -> Config:
        cfg = self.config
        cfg.download_dir = self.folder.get().strip()
        cfg.delay_seconds = self._int(self.delay, cfg.delay_seconds)
        cfg.start_timeout_seconds = max(20, self._int(self.start_timeout, cfg.start_timeout_seconds))
        cfg.include_requirements = self.include_requirements.get()
        cfg.skip_existing = self.skip_existing.get()
        cfg.skip_optional = self.skip_optional.get()
        cfg.scan_only = self.scan_only.get()
        cfg.confirm = self.confirm.get()
        cfg.all_main = self.all_main.get()
        try:
            save_config(cfg)
        except OSError:
            pass
        return cfg

    # -- which buttons may be used right now ---------------------------------------------------

    def _busy(self) -> bool:
        return self._job_running or self._cleaning or self._checking

    def _selected_entries(self) -> list[tuple[str, Work]]:
        """The selected table rows that are a file (off-site and unavailable rows have nothing to act on)."""
        return [(row, self.entries[row]) for row in self.tree.selection() if row in self.entries]

    def _refresh_controls(self) -> None:
        def state(enabled: bool) -> str:
            return "normal" if enabled else "disabled"

        idle = not self._busy()
        self.start_button.config(state=state(idle))
        self.stop_button.config(state=state(self._job_running and not self._stopping))
        self.open_button.config(state=state(idle))
        self.check_button.config(state=state(idle))
        self.delete_all_button.config(state=state(idle))
        selected = idle and bool(self._selected_entries())
        self.delete_button.config(state=state(selected))
        self.again_button.config(state=state(selected))

    def _refuse_while_busy(self) -> bool:
        if self._busy():
            self.append_log("Another action is in progress: wait for it to finish (or press Stop) first.")
        return self._busy()

    # -- running the downloader ----------------------------------------------------------------

    def browse(self) -> None:
        chosen = filedialog.askdirectory(title="Where should the mods be saved?", initialdir=self.folder.get() or None)
        if chosen:
            self.folder.set(chosen)

    def start(self) -> None:
        if self._busy():
            return
        links, rejected = parse_links(self.links_text())
        if not links:
            hint = "\n\nNot a mod or collection link:\n" + "\n".join(rejected) if rejected else ""
            messagebox.showerror(
                "No link",
                "Paste at least one mod page link or collection link, for example\n"
                "https://www.nexusmods.com/<game>/mods/<id>\n"
                "https://www.nexusmods.com/<game>/collections/<slug>" + hint,
            )
            return
        if not self.folder.get().strip():
            messagebox.showerror("No folder", "Choose the folder the mods should be downloaded to.")
            return
        config = replace(self.collect_config())
        self.clear_table()
        self._table_dir = Path(config.download_dir)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text("", encoding="utf-8")
        for line in rejected:
            self.append_log(f"Ignored (not a mod or collection link): {line}")
        self._start_job(Job(config, links, JobUi(self), self.stop, self.budget), announce=True)

    def _start_job(self, job: Job, announce: bool) -> None:
        self.stop.clear()
        self._stopping = False
        self._job_running = True
        self._announce = announce
        self._refresh_controls()
        self.worker = threading.Thread(target=job.run, name="job", daemon=True)
        self.worker.start()

    def request_stop(self) -> None:
        self.stop.set()
        self._stopping = True
        self._refresh_controls()
        self.append_log("Stopping ...")

    def open_browser(self) -> None:
        config = self.collect_config()

        def task() -> None:
            ui = JobUi(self)
            try:
                started = ensure_browser(config.debug_port, LOGIN_URL, ui.log, config.browser_path)
            except BrowserError as exc:
                self.bridge.post(messagebox.showerror, "Browser", str(exc))
                return
            ui.log(
                "The browser window is open: log in to Nexus Mods there, then press 'Check login'."
                if started
                else "The browser is already open: log in there if you have not yet, then press 'Check login'."
            )

        threading.Thread(target=task, daemon=True).start()

    def check_login(self) -> None:
        config = self.collect_config()
        self._checking = True
        self._refresh_controls()

        def task() -> None:
            ui = JobUi(self)
            try:
                ensure_browser(config.debug_port, LOGIN_URL, ui.log, config.browser_path)
                with BrowserSession(config.debug_port, budget=self.budget) as session:
                    member = SiteFlow(session, log=ui.log).current_member()
                if member:
                    ui.set_login(True, "Logged in")
                else:
                    ui.set_login(False, "Not logged in - press 'Open browser / log in' and log in there")
            except Exception as exc:
                ui.log(f"Could not check the login: {exc}")
                ui.set_login(None, "Login check failed - see the log")
            finally:
                self.bridge.post(self._login_check_done)

        threading.Thread(target=task, daemon=True).start()

    def _login_check_done(self) -> None:
        self._checking = False
        self._refresh_controls()

    # -- the file table: delete a file, download it again, delete everything -------------------

    def _folder_of_table(self) -> Path | None:
        folder = self._table_dir or (Path(self.folder.get().strip()) if self.folder.get().strip() else None)
        if folder is None:
            messagebox.showerror("No folder", "Choose the folder the mods are downloaded to.")
        return folder

    def delete_selected(self, _event=None) -> None:
        """Quick delete: remove the downloaded file of each selected row. One row needs no confirmation."""
        targets = self._selected_entries()
        if not targets or self._refuse_while_busy():
            return
        if len(targets) > 1 and not messagebox.askyesno(
            "Delete files",
            f"Delete the downloaded files of the {len(targets)} selected entries?\n\nThis cannot be undone.",
            icon="warning", default="no",
        ):
            return
        folder = self._folder_of_table()
        if folder is None:
            return
        pairs = [(work.item.ref, work.file) for _row, work in targets]
        self._run_cleanup(folder, lambda manifest: delete_files(manifest, pairs))

    def delete_all(self) -> None:
        """Delete every file this tool downloaded into the folder (the ones its manifest lists), after asking."""
        if self._refuse_while_busy():
            return
        text = self.folder.get().strip()
        if not text or not Path(text).is_dir():
            messagebox.showerror("No folder", "Choose the download folder first.")
            return
        folder = Path(text)
        files = [item for item in Manifest(folder).tracked() if item.path is not None and item.path.is_file()]
        if not files:
            messagebox.showinfo("Delete all downloaded files",
                                "No downloaded files are recorded for this folder, so there is nothing to delete.")
            return
        size = sum(item.path.stat().st_size for item in files)
        if not messagebox.askyesno(
            "Delete all downloaded files",
            f"Delete the {len(files)} file(s) ({format_size(size)}) that this tool downloaded into\n{folder}\n\n"
            "Other files in that folder are not touched. This cannot be undone.",
            icon="warning", default="no",
        ):
            return
        self._run_cleanup(folder, delete_everything)

    def _run_cleanup(self, folder: Path, task) -> None:
        """Run a deletion off the Tk thread (a big folder can take a while) and report back."""
        self._cleaning = True
        self._refresh_controls()

        def work() -> None:
            try:
                removals = task(Manifest(folder))
            except Exception as exc:
                self.bridge.post(self._cleanup_failed, exc)
                return
            self.bridge.post(self._cleanup_done, removals)

        threading.Thread(target=work, name="cleanup", daemon=True).start()

    def _cleanup_done(self, removals: list[Removal]) -> None:
        self._cleaning = False
        for removal in removals:
            if removal.state == "deleted":
                status = "deleted"
            elif removal.state == "missing":
                status = "not on disk"
            else:
                status = f"could not delete: {removal.reason}"
            self.set_row_status(removal.row_id, status, reveal=False)
        deleted = [r for r in removals if r.state == "deleted"]
        if len(removals) == 1 and deleted:  # a quick single delete: one clear line is enough
            self.append_log(f"Deleted {deleted[0].name} ({format_size(deleted[0].size)})")
        else:
            if 0 < len(deleted) <= MAX_LISTED_DELETIONS:
                for removal in deleted:
                    self.append_log(f"Deleted {removal.name} ({format_size(removal.size)})")
            for removal in removals:
                if removal.state == "failed":
                    self.append_log(f"Could not delete {removal.name or removal.row_id}: {removal.reason}")
            self.append_log(summarize(removals))
        self._refresh_controls()

    def _cleanup_failed(self, exc: Exception) -> None:
        self._cleaning = False
        self.append_log(f"Deleting failed: {exc}")
        self._refresh_controls()

    def download_selected_again(self) -> None:
        """Download the selected files again, replacing whatever copy is on disk. Needs no new scan."""
        targets = self._selected_entries()
        if not targets or self._refuse_while_busy():
            return
        folder = self._folder_of_table()
        if folder is None:
            return
        config = replace(self.collect_config(), download_dir=str(folder))
        works = [work for _row, work in targets]
        self.append_log(f"Downloading {len(works)} file(s) again ...")
        self._start_job(Job(config, [], JobUi(self), self.stop, self.budget, redo=works), announce=False)

    def _on_right_click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        self._refresh_controls()
        can_act = bool(self._selected_entries()) and not self._busy()
        for label in ("Download again", "Delete file"):
            self.menu.entryconfig(label, state="normal" if can_act else "disabled")
        self.menu.entryconfig("Open link", state="normal" if self.row_urls.get(row) else "disabled")
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def open_row_link(self, _event=None) -> None:
        selected = self.tree.selection()
        if selected and self.row_urls.get(selected[0]):
            webbrowser.open(self.row_urls[selected[0]])

    def clear_log(self) -> None:
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")

    def on_close(self) -> None:
        self.bridge.closing.set()
        self.stop.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=8)  # lets a running download be cancelled in the browser first
        self.collect_config()
        self.budget.flush()
        self.root.destroy()

    # -- updates coming from the worker (always on the Tk thread) ------------------------------

    def _refresh_usage(self) -> None:
        usage = self.budget.usage()
        self.usage_text.set(
            f"Nexus allows {OFFICIAL_HOURLY} requests an hour and {OFFICIAL_DAILY:,} a day. This tool counts the "
            f"requests it makes and pauses at 90 % of that. Used: {usage.hour_used} of {usage.hour_limit} this hour, "
            f"{usage.day_used:,} of {usage.day_limit:,} in 24 hours."
        )
        share = max(usage.hour_used / usage.hour_limit, usage.day_used / usage.day_limit)
        self.usage_label.config(foreground=STOP_COLOR if share >= 1 else WARN_COLOR if share >= 0.8 else "gray")
        self.root.after(2000, self._refresh_usage)

    def append_log(self, text: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}] {text}\n"
        self.log_box.config(state="normal")
        self.log_box.insert("end", line)
        self.log_box.see("end")
        self.log_box.config(state="disabled")
        try:
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(hide_home(line))  # the file may end up in a bug report: no Windows user name in it
        except OSError:
            pass

    def clear_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.row_urls.clear()
        self.entries.clear()
        self._row_count = 0
        self.overall_bar.config(value=0)
        self.file_bar.config(value=0)
        self.overall_text.set("")
        self._refresh_controls()

    def show_plan(self, rows: list[Row]) -> None:
        self.clear_table()
        for row in rows:
            self._row_count += 1
            self.tree.insert("", "end", iid=row.id,
                             values=(self._row_count, row.mod, row.file, format_size(row.size), row.status))
            if row.url:
                self.row_urls[row.id] = row.url
            if row.work is not None:
                self.entries[row.id] = row.work

    def set_row_status(self, row_id: str, status: str, reveal: bool = True) -> None:
        if self.tree.exists(row_id):
            self.tree.set(row_id, "status", status)
            if reveal:
                self.tree.see(row_id)

    def set_overall(self, done: int, total: int, text: str) -> None:
        self.overall_bar.config(maximum=max(total, 1), value=done)
        self.overall_text.set(f"{done} of {total} done - {text}")
        self.file_bar.config(value=0)

    def set_file_bar(self, received: int, total: int) -> None:
        self.file_bar.config(maximum=max(total, 1), value=received if total else 0)

    def set_login(self, logged_in: bool | None, text: str) -> None:
        self.login_text.set(text)
        self.login_dot.config(fg="gray" if logged_in is None else (OK_COLOR if logged_in else STOP_COLOR))

    def on_finished(self, summary: str) -> None:
        self._job_running = False
        self._stopping = False
        self._refresh_controls()
        self.overall_text.set(summary)
        self.append_log(summary)
        if self._announce and not self.bridge.closing.is_set():
            messagebox.showinfo("Nexus Mods Auto Downloader", summary)


def main() -> None:
    try:  # crisp text on high-DPI screens
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()
