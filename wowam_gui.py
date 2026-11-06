#!/usr/bin/env python3
"""Tkinter front-end for wowam. Run: python wowam_gui.py  (or: python wowam.py gui)"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import webbrowser
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # e.g. Fedora/Bazzite without python3-tkinter
    tk = None

import wowam

# Colors are plain data so both themes stay easy to tweak. Text/background pairs are all >= 4.5:1 contrast.
PALETTES = {
    "light": dict(bg="#f3f3f3", fg="#1f1f1f", field="#ffffff", btn="#e4e4e4", btn_active="#d4d4d4",
                  border="#c4c4c4", sel="#cfe3f7", sel_fg="#000000", accent="#1a6fd1", muted="#5f5f5f",
                  error="#b00020", update="#9a5b00", pinned="#707070"),
    "dark": dict(bg="#1e1f22", fg="#e6e6e6", field="#2b2d31", btn="#3a3c41", btn_active="#4b4e55",
                 border="#4b4e55", sel="#2d4f7c", sel_fg="#ffffff", accent="#5b9cf5", muted="#9a9da3",
                 error="#ff7b7b", update="#e5a63a", pinned="#8f9298"),
}


def set_dark_titlebar(win, dark: bool) -> None:
    """Windows 10/11: make the native title bar match the theme. Silently does nothing elsewhere."""
    if os.name != "nt":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (19 on early Windows 10 builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception:
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = wowam.load_config()
        self.theme_name = None
        self.pal = PALETTES["light"]
        self.mgr: wowam.Manager | None = None
        self.pending: dict[str, dict] = {}
        self.busy = False
        self.q: queue.Queue = queue.Queue()
        self.next_check = 0.0

        root.title(f"WoW Addon Manager {wowam.__version__}")
        root.geometry("1200x620")
        root.minsize(760, 420)

        self.update_info: dict | None = None
        self.next_app_check = 0.0
        self.banner = ttk.Frame(root, style="Banner.TFrame", padding=(10, 6))  # shown only when wowam has an update
        self.bar = bar = ttk.Frame(root, padding=(8, 8, 8, 4))
        bar.pack(fill="x")
        self.buttons = []
        # Packed first so a narrow window clips the other buttons, never this one.
        self.settings_btn = ttk.Button(bar, text="Settings", command=self.open_settings)
        self.settings_btn.pack(side="right")
        for text, cmd in [("Check for updates", self.check), ("Update all", self.update_all),
                          ("Update selected", self.update_selected), ("Browse / Install…", self.open_install),
                          ("Install from file…", self.install_from_file), ("Manual downloads…", self.open_manual), ("Remove", self.remove), ("Pin / Unpin", self.toggle_pin),
                          ("Scan folder", self.scan), ("Open page", self.open_page)]:
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=(0, 4))
            self.buttons.append(b)
        bar.bind("<Configure>", self._fit_settings_button)

        pane = ttk.PanedWindow(root, orient="vertical")
        pane.pack(fill="both", expand=True, padx=8)
        tree_frame = ttk.Frame(pane)
        cols = ("version", "status", "channel")
        self.tree = ttk.Treeview(tree_frame, columns=cols, selectmode="extended")
        self.tree.heading("#0", text="Addon")
        self.tree.heading("version", text="Installed version")
        self.tree.heading("status", text="Status")
        self.tree.heading("channel", text="Channel")
        self.tree.column("#0", width=260)
        self.tree.column("version", width=360)
        self.tree.column("status", width=150)
        self.tree.column("channel", width=90)
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        pane.add(tree_frame, weight=4)

        log_frame = ttk.Frame(pane)
        self.logbox = tk.Text(log_frame, height=7, state="disabled", wrap="word")
        self.logbox.pack(fill="both", expand=True)
        pane.add(log_frame, weight=1)

        self.status = tk.StringVar(value="Ready")
        ttk.Label(root, textvariable=self.status, anchor="w", padding=(8, 4)).pack(fill="x")

        self.apply_theme()
        root.after(100, self._drain)
        root.after(1000, self._tick)
        if self._init_manager():
            self.refresh_list()
            self.check()
        else:
            self.status.set("Not set up yet.")
            root.after(300, self.setup)

    # ------------------------------------------------------------ theme
    def resolve_theme(self) -> str:
        t = str(self.cfg.get("theme") or "auto").lower()
        if t == "auto":
            return "dark" if wowam.system_prefers_dark() else "light"
        return t if t in PALETTES else "light"

    def style_window(self, win):
        """Give a dialog the current theme's background and title bar (call right after creating it)."""
        win.configure(background=self.pal["bg"])
        set_dark_titlebar(win, self.theme_name == "dark")

    def apply_theme(self):
        """(Re)style every widget. Cheap no-op when the resolved theme hasn't changed."""
        name = self.resolve_theme()
        if name == self.theme_name:
            return
        self.theme_name = name
        p = self.pal = PALETTES[name]
        s = ttk.Style()
        try:
            s.theme_use("clam")  # the only built-in theme whose colors we can fully control
        except tk.TclError:
            pass
        s.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["field"],
                    troughcolor=p["field"], bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"],
                    selectbackground=p["sel"], selectforeground=p["sel_fg"], insertcolor=p["fg"],
                    focuscolor=p["accent"])
        s.map(".", foreground=[("disabled", p["muted"])])
        s.configure("TButton", background=p["btn"], lightcolor=p["btn"], darkcolor=p["btn"], padding=(8, 3))
        s.map("TButton", background=[("disabled", p["bg"]), ("active", p["btn_active"])],
              bordercolor=[("focus", p["accent"])])
        for w in ("TEntry", "TCombobox"):
            s.configure(w, fieldbackground=p["field"], foreground=p["fg"], lightcolor=p["field"],
                        darkcolor=p["field"], arrowcolor=p["fg"])
        s.map("TEntry", fieldbackground=[("disabled", p["bg"])])
        s.map("TCombobox", fieldbackground=[("readonly", p["field"]), ("disabled", p["bg"])],
              foreground=[("readonly", p["fg"])], background=[("active", p["btn_active"])])
        s.configure("TCombobox", background=p["btn"])
        for w in ("TCheckbutton", "TRadiobutton"):
            # clam draws the tick/dot with indicatorforeground (black by default: invisible on dark)
            s.configure(w, indicatorbackground=p["field"], indicatorforeground=p["accent"],
                        upperbordercolor=p["border"], lowerbordercolor=p["border"])
            s.map(w, indicatorforeground=[("disabled", p["muted"])],
                  indicatorbackground=[("pressed", p["btn_active"]), ("disabled", p["bg"])],
                  background=[("active", p["bg"])])
        s.configure("TLabelframe", background=p["bg"], bordercolor=p["border"])
        s.configure("TLabelframe.Label", background=p["bg"], foreground=p["fg"])
        s.configure("Treeview", background=p["field"], fieldbackground=p["field"], foreground=p["fg"],
                    rowheight=24)
        s.map("Treeview", background=[("selected", p["sel"])], foreground=[("selected", p["sel_fg"])])
        s.configure("Treeview.Heading", background=p["btn"], foreground=p["fg"], relief="flat",
                    lightcolor=p["btn"], darkcolor=p["btn"])
        s.map("Treeview.Heading", background=[("active", p["btn_active"])])
        s.configure("TScrollbar", background=p["btn"], arrowcolor=p["fg"], lightcolor=p["btn"],
                    darkcolor=p["btn"], troughcolor=p["field"])
        s.map("TScrollbar", background=[("active", p["btn_active"])])
        s.configure("Banner.TFrame", background=p["sel"])
        s.configure("Banner.TLabel", background=p["sel"], foreground=p["sel_fg"])
        s.configure("TPanedwindow", background=p["bg"])
        s.configure("Sash", background=p["border"])
        # classic tk widgets and the combobox drop-down list ignore ttk styles
        self.root.option_add("*TCombobox*Listbox.background", p["field"])
        self.root.option_add("*TCombobox*Listbox.foreground", p["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", p["sel"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", p["sel_fg"])
        self.logbox.configure(background=p["field"], foreground=p["fg"], insertbackground=p["fg"],
                              selectbackground=p["sel"], selectforeground=p["sel_fg"],
                              highlightbackground=p["border"], highlightcolor=p["accent"], relief="flat")
        self.tree.tag_configure("update", foreground=p["update"])
        self.tree.tag_configure("pinned", foreground=p["pinned"])
        self.style_window(self.root)
        for w in self.root.winfo_children():
            if isinstance(w, tk.Toplevel):
                self.style_window(w)

    # ------------------------------------------------------------ plumbing
    def log(self, msg):
        self.q.put(("log", str(msg)))

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", payload + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def ui(self, fn):
        self.q.put(("call", fn))

    def run_bg(self, label, work, done=None):
        if self.busy:
            return
        if not self.mgr and not self._init_manager():
            self.setup()
            return
        self.busy = True
        self.status.set(label + "…")
        for b in self.buttons:
            b.state(["disabled"])

        def runner():
            result, err = None, None
            try:
                result = work()
            except wowam.WowamError as e:
                err = str(e)
            except Exception as e:  # keep the GUI alive on unexpected errors
                err = f"Unexpected error: {e!r}"

            def finish():
                self.busy = False
                for b in self.buttons:
                    b.state(["!disabled"])
                if err:
                    msg = err + ("  — open Settings to check your CurseForge key." if "(403)" in err else "")
                    self.log(f"! {msg}")
                    self.status.set(msg)
                else:
                    self.status.set("Ready")
                    if done:
                        done(result)
                self.refresh_list()
            self.ui(finish)

        threading.Thread(target=runner, daemon=True).start()

    def _init_manager(self) -> bool:
        try:
            self.mgr = wowam.Manager(self.cfg, log=self.log)
            return True
        except wowam.WowamError as e:
            self.mgr = None
            self.status.set(str(e))
            return False

    # ------------------------------------------------------------ wowam's own updates
    def _fit_settings_button(self, _event=None):
        """Show "Settings" in full; only when the toolbar can't fit, collapse it to a gear icon."""
        btn = self.settings_btn
        left = sum(b.winfo_reqwidth() + 4 for b in self.buttons)
        full = btn.cget("text") == "Settings"
        full_w = btn.winfo_reqwidth() if full else 86
        avail = self.bar.winfo_width() - 16
        want = "Settings" if left + full_w <= avail else "⚙"
        if want != btn.cget("text"):
            btn.configure(text=want, width=0 if want == "Settings" else 3)

    def check_app_update(self, force=False):
        """The quiet daily check (or Settings' "Check now" when force): shows the banner if a newer wowam exists."""
        def bg():
            info = err = None
            try:
                info = wowam.newer_release() if force else wowam.check_self_update(self.cfg)
            except wowam.WowamError as e:
                err = str(e)
            self.ui(lambda: self._app_update_checked(info, err, force))
        threading.Thread(target=bg, daemon=True).start()

    def _app_update_checked(self, info, err, force):
        if err or not info:
            if force:
                if err:
                    messagebox.showerror("wowam", err)
                else:
                    messagebox.showinfo("wowam", f"wowam {wowam.__version__} is the latest version.")
            return
        self.update_info = info
        self.show_banner(info)

    def show_banner(self, info):
        for w in self.banner.winfo_children():
            w.destroy()
        ttk.Label(self.banner, style="Banner.TLabel",
                  text=f"wowam {info['version']} is available (you have {wowam.__version__}).").pack(side="left")
        ttk.Button(self.banner, text="✕", width=3, command=self.hide_banner).pack(side="right")
        ttk.Button(self.banner, text="Skip this version", command=self.skip_app_update).pack(side="right", padx=4)
        if wowam.FROZEN and info.get("exe_url") and info.get("sha_url"):
            ttk.Button(self.banner, text="Release notes", command=lambda: webbrowser.open(info["page"])).pack(
                side="right", padx=4)
            ttk.Button(self.banner, text="Update now", command=self.do_app_update).pack(side="right", padx=4)
        else:  # running from source: replacing our own files isn't something we do
            ttk.Button(self.banner, text="Open download page", command=lambda: webbrowser.open(info["page"])).pack(
                side="right", padx=4)
        self.banner.pack(fill="x", before=self.bar)

    def hide_banner(self):
        self.banner.pack_forget()

    def skip_app_update(self):
        if self.update_info:
            self.cfg["skipped_version"] = self.update_info["version"]
            wowam.update_config_file(skipped_version=self.cfg["skipped_version"])
        self.hide_banner()

    def _lock(self, on: bool):
        self.busy = on
        for b in self.buttons:
            b.state(["disabled" if on else "!disabled"])

    def do_app_update(self):
        info = self.update_info
        if not info:
            return
        if self.busy:
            return messagebox.showinfo("wowam", "Please wait for the current task to finish first.")
        if not messagebox.askyesno("Update wowam", f"Download wowam {info['version']} and restart now?\n\n"
                                   "Your settings and installed addons are not affected."):
            return
        self._lock(True)
        self.status.set("Updating wowam…")

        def bg():
            err = None
            try:
                wowam.apply_self_update(info, log=self.log)
            except wowam.WowamError as e:
                err = str(e)
            except Exception as e:  # keep the GUI alive on unexpected errors
                err = f"Unexpected error: {e!r}"
            self.ui(lambda: self._app_update_done(err))
        threading.Thread(target=bg, daemon=True).start()

    def _app_update_done(self, err):
        self._lock(False)
        if err:
            self.log(f"! {err}")
            self.status.set(err)
            return messagebox.showerror("Update failed", f"{err}\n\nThe version you have was kept.")
        wowam.relaunch()
        self.root.destroy()

    def _tick(self):
        if time.time() >= self.next_app_check and not self.update_info:
            self.next_app_check = time.time() + 3600  # the 24h throttle lives in wowam.check_self_update
            self.check_app_update()
        if str(self.cfg.get("theme") or "auto").lower() == "auto":
            self.apply_theme()  # follows the OS if it flips between light and dark while we're open
        mins = int(self.cfg.get("auto_check_minutes") or 0)
        if mins > 0 and self.mgr and not self.busy and time.time() >= self.next_check:
            if self.cfg.get("auto_update"):
                self.update_all(auto=True)
            else:
                self.check()
        self.root.after(30_000, self._tick)

    def _schedule_next(self):
        mins = int(self.cfg.get("auto_check_minutes") or 0)
        self.next_check = time.time() + mins * 60 if mins > 0 else float("inf")

    # ------------------------------------------------------------ list
    def refresh_list(self):
        sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        if not self.mgr:
            return
        items = sorted(self.mgr.installed.items(), key=lambda kv: kv[1].get("name", "").lower())
        n_upd = 0
        for mid, e in items:
            if e.get("pinned"):
                status, tag = "Pinned", "pinned"
            elif mid in self.pending:
                manual = self.pending[mid].get("blocked")
                status, tag = ("Update: download manually" if manual else "Update available"), "update"
                n_upd += 1
            elif e.get("file_id") is None and not e.get("manual") and e.get("source") != "github":
                status, tag = "Needs sync", "update"
            else:
                status, tag = "Up to date", ""
            self.tree.insert("", "end", iid=mid, text=e.get("name", mid),
                             values=(e.get("version", ""), status, e.get("channel") or "default"), tags=(tag,))
        for iid in sel:
            if self.tree.exists(iid):
                self.tree.selection_add(iid)
        self.root.title(f"WoW Addon Manager — {len(items)} addons" + (f", {n_upd} updates" if n_upd else ""))

    def selected(self):
        return list(self.tree.selection()) if self.mgr else []

    # ------------------------------------------------------------ actions
    def check(self):
        def work():
            return self.mgr.check_updates()

        def done(pending):
            self.pending = {p["id"]: p for p in pending}
            self.log(f"[{time.strftime('%H:%M')}] {len(pending)} update(s) available."
                     if pending else f"[{time.strftime('%H:%M')}] Everything is up to date.")
            for p in pending:
                if p["blocked"]:
                    self.log("  ! " + self.mgr.manual_notice(p).replace("`wowam install-file <zip>`", "“Install from file…”"))
            self.manual_refresh()
        self._schedule_next()
        self.run_bg("Checking for updates", work, done)

    def update_all(self, auto=False):
        self._schedule_next()
        self.run_bg("Updating" + (" (auto)" if auto else ""), lambda: self.mgr.update(),
                    lambda _: self._after_update(None, auto))

    def update_selected(self):
        ids = self.selected()
        if not ids:
            return messagebox.showinfo("Update", "Select one or more addons first.")
        self.run_bg("Updating selected", lambda: self.mgr.update(ids),
                    lambda _: self._after_update(ids))

    def _after_update(self, ids=None, auto=False):
        """Automatic updates are done; hand-download ones are still pending, so keep them and show them."""
        for i in list(self.pending):
            if (ids is None or i in ids) and not self.pending[i].get("blocked"):
                del self.pending[i]
        if not auto and any(p.get("blocked") for p in self.pending.values()):
            self.open_manual()

    def remove(self):
        ids = self.selected()
        if not ids:
            return
        names = ", ".join(self.mgr.installed[i]["name"] for i in ids)
        if messagebox.askyesno("Remove", f"Uninstall {names}?\nTheir folders will be deleted."):
            self.run_bg("Removing", lambda: [self.mgr.remove(i) for i in ids])

    def toggle_pin(self):
        for i in self.selected():
            self.mgr.set_pinned(i, not self.mgr.installed[i].get("pinned"))
        self.refresh_list()

    def scan(self, then_check=False):
        self.run_bg("Scanning AddOns folder", self.mgr.scan, (lambda _: self.check()) if then_check else None)

    def setup(self):
        """Guided first-run setup (key -> WoW folder -> finish). Settings stays available for details."""
        SetupWizard(self)

    def install_from_file(self):
        """For addons whose author blocks third-party downloads: install the zip the user downloaded."""
        if not self.mgr and not self._init_manager():
            return self.setup()
        paths = filedialog.askopenfilenames(title="Pick the downloaded addon zip(s)",
                                            filetypes=[("Addon zip", "*.zip"), ("All files", "*.*")])
        if paths:
            self.run_bg("Installing from file", lambda: [self.mgr.install_local(p) for p in paths])

    def open_manual(self):
        """Addons whose authors block third-party downloads: open each file's page, then install the zip here."""
        if not self.mgr and not self._init_manager():
            return self.setup()
        if getattr(self, "manual_win", None) and self.manual_win.winfo_exists():
            self.manual_win.lift()
            return self.manual_refresh()
        win = self.manual_win = tk.Toplevel(self.root)
        self.style_window(win)
        win.title("Manual downloads")
        win.geometry("760x380")
        win.minsize(560, 260)
        f = ttk.Frame(win, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, wraplength=720, justify="left",
                  text="These authors don't allow other programs to download their addons. Open each page, download "
                       "the zip, then choose “Install zip(s)…” and wowam installs and tracks it for you.").pack(anchor="w")
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True, pady=8)
        tree = self.manual_tree = ttk.Treeview(box, columns=("have", "new"), selectmode="extended", height=8)
        for col, text, w in (("#0", "Addon", 240), ("have", "Installed", 200), ("new", "Download this file", 240)):
            tree.heading(col, text=text)
            tree.column(col, width=w, stretch=(col == "#0"))
        sb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.manual_msg = tk.StringVar()
        ttk.Label(f, textvariable=self.manual_msg).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Open download page", command=self.manual_open_pages).pack(side="left")
        ttk.Button(row, text="Open all", command=lambda: self.manual_open_pages(all_rows=True)).pack(side="left", padx=4)
        ttk.Button(row, text="Install zip(s)…", command=self.manual_install).pack(side="left", padx=(16, 0))
        ttk.Button(row, text="Close", command=win.destroy).pack(side="right")
        tree.bind("<Double-1>", lambda e: self.manual_open_pages())
        self.manual_refresh()
        if not self.pending and not self.busy:  # nothing checked yet this session
            self.check()

    def _manual_items(self):
        return {i: p for i, p in self.pending.items() if p.get("blocked")}

    def manual_refresh(self):
        if not hasattr(self, "manual_tree"):
            return
        win = getattr(self, "manual_win", None)
        if not win or not win.winfo_exists():
            return
        tree = self.manual_tree
        tree.delete(*tree.get_children())
        for i, p in self._manual_items().items():
            tree.insert("", "end", iid=i, text=p["entry"].get("name", i),
                        values=(p["entry"].get("version", ""), p["latest_version"]))
        n = len(tree.get_children())
        self.manual_msg.set("Everything is up to date." if not n else
                            f"{n} addon(s) need a manual download. Double-click a row to open its page.")

    def manual_open_pages(self, all_rows=False):
        items = self._manual_items()
        ids = list(items) if all_rows else list(self.manual_tree.selection())
        if not ids:
            return self.manual_msg.set("Select an addon first (or use “Open all”).")
        for i in ids[:10]:
            if i in items:
                webbrowser.open(self.mgr.manual_url(items[i]))

    def manual_install(self):
        paths = filedialog.askopenfilenames(parent=self.manual_win, title="Pick the downloaded addon zip(s)",
                                            filetypes=[("Addon zip", "*.zip"), ("All files", "*.*")])
        if not paths or self.busy:
            return

        def work():
            for p in paths:
                try:
                    self.mgr.install_local(p)
                except wowam.WowamError as e:  # one bad zip shouldn't stop the rest
                    self.log(f"! {os.path.basename(p)}: {e}")
            return self.mgr.check_updates()  # what is still outdated after installing

        def done(pending):
            self.pending = {p["id"]: p for p in pending}
            self.manual_refresh()
        self.run_bg("Installing from file", work, done)

    def _install_refs(self, refs):
        """Worker-thread install; a blocked addon opens its CurseForge page for a manual download."""
        for r in refs:
            try:
                if "github.com/" in r:
                    self.mgr.install_github(r)
                else:
                    self.mgr.install(r)
            except wowam.ManualDownload as e:
                self.log(f"! {e}".replace("wowam install-file <downloaded.zip>", "“Install from file…”"))
                url = next((w for w in str(e).split() if w.startswith("https://")), "").rstrip(",.")
                if url:
                    webbrowser.open(url)

    def open_page(self):
        for i in self.selected()[:3]:
            url = self.mgr.installed[i].get("url") or f"https://www.curseforge.com/wow/addons/{self.mgr.installed[i].get('slug')}"
            webbrowser.open(url)

    # ------------------------------------------------------------ install dialog
    def open_install(self):
        if not self.mgr and not self._init_manager():
            return self.setup()
        win = tk.Toplevel(self.root)
        self.style_window(win)
        win.title("Browse and install addons")
        win.geometry("780x540")
        win.transient(self.root)
        top = ttk.Frame(win, padding=(8, 8, 8, 2))
        top.pack(fill="x")
        term = tk.StringVar()
        ent = ttk.Entry(top, textvariable=term)
        ent.pack(side="left", fill="x", expand=True)
        ent.focus_set()
        sorts = {"Most popular": "popular", "Recently updated": "updated", "Most downloaded": "downloads"}
        browse_bar = ttk.Frame(win, padding=(8, 2, 8, 4))
        browse_bar.pack(fill="x")
        ttk.Label(browse_bar, text="Browse:").pack(side="left")
        sort_var = tk.StringVar(value="Most popular")
        sort_box = ttk.Combobox(browse_bar, textvariable=sort_var, values=list(sorts), state="readonly", width=18)
        sort_box.pack(side="left", padx=6)
        tree = ttk.Treeview(win, columns=("dl", "summary"), selectmode="extended")
        tree.heading("#0", text="Name"); tree.heading("dl", text="Downloads"); tree.heading("summary", text="Summary")
        tree.column("#0", width=230); tree.column("dl", width=90, anchor="e"); tree.column("summary", width=420)
        tree.pack(fill="both", expand=True, padx=8)
        info = tk.StringVar(value="Loading popular addons…")
        ttk.Label(win, textvariable=info, padding=8, wraplength=740, justify="left").pack(fill="x")
        state = {"gen": 0, "index": 0}  # gen drops results from a request the user has since replaced
        shown_mods: dict = {}           # item id -> mod, for the page button and right-click menu

        def fill(mods, append):
            if not append:
                tree.delete(*tree.get_children())
                shown_mods.clear()
            for h in mods:
                iid = str(h["id"])
                if tree.exists(iid):
                    continue
                shown_mods[iid] = h
                mark = "  ✓" if iid in self.mgr.installed else ""
                if h.get("allowModDistribution") is False:
                    mark += "  (manual download)"
                tree.insert("", "end", iid=iid, text=h["name"] + mark,
                            values=(f"{h.get('downloadCount', 0):,.0f}", h.get("summary", "")))

        def run(work, append, describe):
            state["gen"] += 1
            gen = state["gen"]
            more_btn.state(["disabled"])
            info.set("Loading…")

            def bg():
                try:
                    r = work()
                except wowam.WowamError as e:
                    return self.ui(lambda: win.winfo_exists() and gen == state["gen"] and info.set(str(e)))

                def show():
                    if not win.winfo_exists() or gen != state["gen"]:
                        return
                    fill(r["mods"], append)
                    state["index"] += len(r["mods"])
                    more_btn.state(["!disabled" if r.get("more") else "disabled"])
                    note = (" Your CurseForge key can't browse the whole catalogue, so this is CurseForge's "
                            "featured list." if r.get("limited") else "")
                    if r.get("local"):
                        note += (f" Your key can't search by name, so I looked through the {r['scanned']} most "
                                 f"popular addons." + (" Less popular ones may be missing: paste an ID or "
                                                       "CurseForge URL and press Install." if r.get("truncated") else ""))
                    info.set(f"{describe(len(tree.get_children()))}{note}  “Manual download” addons open their "
                             f"CurseForge page; download the zip, then use “Install from file…”.")
                self.ui(show)
            threading.Thread(target=bg, daemon=True).start()

        def browse(append=False):
            if not append:
                state["index"] = 0
            idx, kind = state["index"], sorts[sort_var.get()]
            run(lambda: self.mgr.cf.browse(kind, self.mgr.flavor, 25, idx), append,
                lambda n: f"{n} addons shown ({sort_var.get().lower()}).")

        def do_search(*_):
            t = term.get().strip()
            if not t:
                return browse()
            run(lambda: {**self.mgr.cf.search_any(t, flavor=self.mgr.flavor), "more": False}, False,
                lambda n: f"{n} result(s) for “{t}”.")

        def on_sort(*_):
            term.set("")
            browse()

        def do_install():
            refs = list(tree.selection()) or ([term.get().strip()] if term.get().strip() else [])
            if not refs:
                return
            win.destroy()
            self.run_bg("Installing", lambda: self._install_refs(refs))

        def page_url(iid):
            m = shown_mods.get(iid) or {}
            return (m.get("links") or {}).get("websiteUrl") or (
                f"https://www.curseforge.com/wow/addons/{m['slug']}" if m.get("slug") else None)

        def open_pages():
            urls = [u for u in (page_url(i) for i in tree.selection()[:3]) if u]  # a few at most, not a tab flood
            if not urls:
                info.set("Select an addon first.")
            for u in urls:
                webbrowser.open(u)

        def copy_ids():
            win.clipboard_clear()
            win.clipboard_append(" ".join(tree.selection()))

        more_btn = ttk.Button(browse_bar, text="Load more", command=lambda: browse(append=True))
        more_btn.pack(side="left")
        ttk.Button(browse_bar, text="CurseForge page", command=open_pages).pack(side="right")

        p = self.pal
        menu = tk.Menu(win, tearoff=0, background=p["field"], foreground=p["fg"], activebackground=p["sel"],
                       activeforeground=p["sel_fg"], borderwidth=0)
        menu.add_command(label="Open CurseForge page", command=open_pages)
        menu.add_command(label="Install", command=do_install)
        menu.add_command(label="Copy project ID", command=copy_ids)

        def on_right_click(event):
            iid = tree.identify_row(event.y)
            if not iid:
                return
            if iid not in tree.selection():
                tree.selection_set(iid)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
        for seq in (("<Button-2>", "<Control-Button-1>", "<Button-3>") if sys.platform == "darwin" else ("<Button-3>",)):
            tree.bind(seq, on_right_click)  # macOS reports the right/secondary click differently

        ent.bind("<Return>", do_search)
        sort_box.bind("<<ComboboxSelected>>", on_sort)
        tree.bind("<Double-1>", lambda _: do_install())
        ttk.Button(top, text="Search", command=do_search).pack(side="left", padx=4)
        ttk.Button(top, text="Install", command=do_install).pack(side="left")
        browse()

    # ------------------------------------------------------------ settings
    def open_settings(self):
        win = tk.Toplevel(self.root)
        self.style_window(win)
        win.title("Settings")
        win.transient(self.root)
        f = ttk.Frame(win, padding=12)
        f.pack(fill="both", expand=True)
        v = {
            "theme": tk.StringVar(value=str(self.cfg.get("theme") or "auto")),
            "api_key": tk.StringVar(value=self.cfg.get("api_key", "")),
            "addons_dir": tk.StringVar(value=self.cfg.get("addons_dir", "")),
            "flavor": tk.StringVar(value=str(self.cfg.get("flavor", "auto"))),
            "channel": tk.StringVar(value=self.cfg.get("channel", "release")),
            "auto_check_minutes": tk.StringVar(value=str(self.cfg.get("auto_check_minutes", 60))),
            "auto_update": tk.BooleanVar(value=bool(self.cfg.get("auto_update"))),
            "install_dependencies": tk.BooleanVar(value=bool(self.cfg.get("install_dependencies", True))),
            "check_for_updates": tk.BooleanVar(value=bool(self.cfg.get("check_for_updates", True))),
        }
        r = 0

        def row(label, widget, extra=None):
            nonlocal r
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3)
            widget.grid(row=r, column=1, sticky="ew", pady=3)
            if extra:
                extra.grid(row=r, column=2, padx=(4, 0))
            r += 1

        row("CurseForge API key", ttk.Entry(f, textvariable=v["api_key"], show="•", width=56),
            ttk.Button(f, text="Get key", command=lambda: webbrowser.open("https://console.curseforge.com")))

        def browse():
            d = filedialog.askdirectory(title="Select your Interface/AddOns folder")
            if d:
                v["addons_dir"].set(d)

        def detect():
            found = wowam.detect_addons_dirs()
            if not found:
                return messagebox.showinfo("Detect", "No WoW install found automatically. Use Browse.", parent=win)
            pick = next((d for d in found if "_forever_" in d.parts),
                        next((d for d in found if "_retail_" in d.parts), found[0]))
            v["addons_dir"].set(str(pick))
            if len(found) > 1:
                messagebox.showinfo("Detect", "Found:\n" + "\n".join(map(str, found)) + f"\n\nUsing {pick}", parent=win)

        dir_row = ttk.Frame(f)
        ttk.Entry(dir_row, textvariable=v["addons_dir"], width=46).pack(side="left", fill="x", expand=True)
        ttk.Button(dir_row, text="Browse", command=browse).pack(side="left", padx=2)
        row("AddOns folder", dir_row, ttk.Button(f, text="Detect", command=detect))
        row("Game flavor", ttk.Combobox(f, textvariable=v["flavor"], values=["auto", "forever", "retail", "classic"]))
        row("Release channel", ttk.Combobox(f, textvariable=v["channel"], values=list(wowam.CHANNELS), state="readonly"))
        row("Theme", ttk.Combobox(f, textvariable=v["theme"], values=list(wowam.THEMES), state="readonly"))
        row("Auto-check every (min, 0 = off)", ttk.Entry(f, textvariable=v["auto_check_minutes"], width=8))
        row("", ttk.Checkbutton(f, text="Install updates automatically (not just notify)", variable=v["auto_update"]))
        row("", ttk.Checkbutton(f, text="Install required dependencies", variable=v["install_dependencies"]))
        row("", ttk.Checkbutton(f, text="Check for new versions of wowam (once a day)", variable=v["check_for_updates"]),
            ttk.Button(f, text="Check now", command=lambda: self.check_app_update(force=True)))

        bg_frame = ttk.LabelFrame(f, text="Background updates (when this window is closed)", padding=8)
        bg_frame.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(10, 4)); r += 1

        def sched():
            try:
                mins = max(15, int(v["auto_check_minutes"].get() or 60))
                messagebox.showinfo("Scheduled", wowam.schedule(mins), parent=win)
            except Exception as e:
                messagebox.showerror("Schedule failed", str(e), parent=win)

        def unsched():
            messagebox.showinfo("Removed", wowam.unschedule(), parent=win)
        ttk.Button(bg_frame, text="Enable system auto-update", command=sched).pack(side="left")
        ttk.Button(bg_frame, text="Disable", command=unsched).pack(side="left", padx=4)

        def save():
            for k, var in v.items():
                val = var.get()
                if k == "auto_check_minutes":
                    try:
                        val = max(0, int(val))
                    except ValueError:
                        val = 60
                self.cfg[k] = (wowam.clean_api_key(val) if k == "api_key" else val.strip()) if isinstance(val, str) else val
            wowam.save_config(self.cfg)
            win.destroy()
            self.apply_theme()
            if self._init_manager():
                self.pending.clear()
                self.refresh_list()
                if not self.mgr.installed:
                    if messagebox.askyesno("Scan", "Adopt the addons already in this AddOns folder?"):
                        return self.scan()
                self.check()
            else:
                messagebox.showerror("Settings", self.status.get())

        ttk.Button(f, text="Setup wizard…", command=lambda: (win.destroy(), self.setup())).grid(
            row=r, column=0, sticky="w", pady=(10, 0))
        ttk.Button(f, text="Save", command=save).grid(row=r, column=1, sticky="e", pady=(10, 0))
        f.columnconfigure(1, weight=1)


FLAVOR_LABELS = {"forever": "WoW Forever", "retail": "Retail", "classic": "Classic Era"}


class SetupWizard:
    """First-run helper: CurseForge key -> pick your WoW install -> finish. Runs on the Tk thread;
    only the key check goes to a worker thread, and it reports back through app.ui()."""
    KEY_URL = "https://console.curseforge.com/"

    def __init__(self, app: "App"):
        self.app = app
        self.win = tk.Toplevel(app.root)
        app.style_window(self.win)
        self.win.title("Welcome to wowam")
        self.win.geometry("660x480")
        self.win.transient(app.root)
        self.win.protocol("WM_DELETE_WINDOW", self.win.destroy)
        self.key = tk.StringVar(value=app.cfg.get("api_key", ""))
        self.dir = tk.StringVar(value=app.cfg.get("addons_dir", ""))
        self.adopt = tk.BooleanVar(value=True)
        self.autocheck = tk.BooleanVar(value=int(app.cfg.get("auto_check_minutes") or 0) > 0)
        self.autoinstall = tk.BooleanVar(value=bool(app.cfg.get("auto_update")))
        self.shortcut = tk.BooleanVar(value=True)
        self.msg = tk.StringVar()
        self.body = ttk.Frame(self.win, padding=(20, 16, 20, 4))
        self.body.pack(fill="both", expand=True)
        nav = ttk.Frame(self.win, padding=(20, 4, 20, 14))
        nav.pack(fill="x")
        ttk.Button(nav, text="Cancel", command=self.win.destroy).pack(side="left")
        self.next_btn = ttk.Button(nav, text="Next", command=self.next)
        self.next_btn.pack(side="right")
        self.back_btn = ttk.Button(nav, text="Back", command=self.back)
        self.back_btn.pack(side="right", padx=6)
        self.page = 0
        self.show(0)

    # -- layout helpers
    def show(self, page: int):
        self.page = page
        for w in self.body.winfo_children():
            w.destroy()
        self.msg.set("")
        self.back_btn.state(["!disabled" if page else "disabled"])
        self.next_btn.configure(text="Finish" if page == 2 else "Next")
        self.next_btn.state(["!disabled"])
        [self.page_key, self.page_wow, self.page_finish][page]()

    def heading(self, text):
        ttk.Label(self.body, text=text, font=("TkDefaultFont", 14, "bold")).pack(anchor="w", pady=(0, 8))

    def para(self, text, **kw):
        ttk.Label(self.body, text=text, wraplength=600, justify="left", **kw).pack(anchor="w", pady=2)

    def footer_msg(self):
        ttk.Label(self.body, textvariable=self.msg, foreground=self.app.pal["error"], wraplength=600,
                  justify="left").pack(anchor="w", pady=(10, 0))

    # -- pages
    def page_key(self):
        self.heading("Step 1 of 3 — Connect to CurseForge")
        self.para("wowam downloads addons from CurseForge. That needs a free personal key, which you only "
                  "have to set up once:")
        self.para("1.  Click “Open CurseForge” and sign in (or create a free account).\n"
                  "2.  Go to “API keys”, create a key and copy it.  New keys can take a little while to be approved.\n"
                  "3.  Paste it below.")
        ttk.Button(self.body, text="Open CurseForge", command=lambda: webbrowser.open(self.KEY_URL)).pack(
            anchor="w", pady=8)
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=4)
        ent = ttk.Entry(row, textvariable=self.key, show="•")
        ent.pack(side="left", fill="x", expand=True)
        ent.focus_set()
        ent.bind("<Return>", lambda _: self.next())
        shown = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Show", variable=shown,
                        command=lambda: ent.configure(show="" if shown.get() else "•")).pack(side="left", padx=6)
        self.para("Your key is stored only on this computer.", foreground=self.app.pal["muted"])
        self.footer_msg()

    def page_wow(self):
        self.heading("Step 2 of 3 — Find World of Warcraft")
        found = wowam.detect_addons_dirs()
        cur = self.dir.get()
        if not (cur and Path(cur).is_dir()):
            pick = next((d for d in found if "_forever_" in d.parts),
                        next((d for d in found if "_retail_" in d.parts), found[0] if found else None))
            self.dir.set(str(pick) if pick else "")
        if found:
            self.para("Pick the game version you want to manage:")
        else:
            self.para("I couldn't find World of Warcraft automatically. Click “Browse…” and choose the "
                      "AddOns folder inside your game, for example\n"
                      "World of Warcraft\\_retail_\\Interface\\AddOns   (or _forever_ for WoW Forever).")
        self.radio_frame = ttk.Frame(self.body)
        self.radio_frame.pack(fill="x", pady=6)
        for d in found:
            self._add_radio(d)
        if self.dir.get() and Path(self.dir.get()) not in found:
            self._add_radio(Path(self.dir.get()))
        ttk.Button(self.body, text="Browse…", command=self.browse).pack(anchor="w", pady=4)
        self.footer_msg()

    def _add_radio(self, d: Path):
        flavor = wowam.flavor_from_path(d)
        label = f"{FLAVOR_LABELS.get(flavor, flavor.title() if flavor else 'Other')}    ({d})"
        ttk.Radiobutton(self.radio_frame, text=label, value=str(d), variable=self.dir).pack(anchor="w", pady=2)

    def browse(self):
        d = filedialog.askdirectory(parent=self.win, title="Select your Interface/AddOns folder")
        if d:
            self.dir.set(str(Path(d)))
            self.show(1)

    def page_finish(self):
        self.heading("Step 3 of 3 — Almost done")
        self.para(f"wowam will manage:\n{self.dir.get()}")
        for var, text in [
                (self.adopt, "Include the addons I already have installed (recommended)"),
                (self.autocheck, "Check for updates automatically while wowam is open"),
                (self.autoinstall, "Also install those updates automatically"),
                (self.shortcut, "Put a shortcut on my desktop")]:
            ttk.Checkbutton(self.body, text=text, variable=var).pack(anchor="w", pady=3)
        self.para("Addons whose authors block outside downloads can still be added with “Install from file…” "
                  "after you download them from CurseForge yourself.", foreground=self.app.pal["muted"])
        self.footer_msg()

    # -- navigation
    def back(self):
        if self.page:
            self.show(self.page - 1)

    def next(self):
        if self.page == 0:
            self.check_key()
        elif self.page == 1:
            d = self.dir.get().strip()
            if not d or not Path(d).is_dir():
                self.msg.set("Pick a folder first.")
            elif Path(d).name.lower() != "addons" and not messagebox.askyesno(
                    "Are you sure?", f"“{Path(d).name}” isn't named AddOns. Use it anyway?", parent=self.win):
                pass
            else:
                self.show(2)
        else:
            self.finish()

    def check_key(self):
        key = wowam.clean_api_key(self.key.get())
        if not key:
            self.msg.set("Paste your CurseForge key first.")
            return
        self.msg.set("Checking your key…")
        self.next_btn.state(["disabled"])

        def bg():
            try:
                wowam.CurseForge(key).version_types()
                err = None
            except wowam.WowamError as e:
                err = str(e)
            self.app.ui(lambda: self.key_checked(key, err))
        threading.Thread(target=bg, daemon=True).start()

    def key_checked(self, key: str, err: str | None):
        if not self.win.winfo_exists():
            return
        if err is None:
            self.app.cfg["api_key"] = key
            self.show(1)
            return
        self.next_btn.state(["!disabled"])
        if "403" in err:
            self.msg.set("CurseForge didn't accept that key. " + (
                wowam.key_problem(key) or "It looks like a normal key, so check that you copied all of it (not "
                "your password), and that CurseForge has approved it: a brand-new key can take a while."))
        else:
            self.msg.set(f"Couldn't check the key: {err}")

    def finish(self):
        app, cfg = self.app, self.app.cfg
        cfg["addons_dir"] = self.dir.get().strip()
        cfg["flavor"] = "auto"
        cfg["auto_check_minutes"] = 60 if self.autocheck.get() else 0
        cfg["auto_update"] = bool(self.autocheck.get() and self.autoinstall.get())
        wowam.save_config(cfg)
        shortcut = self.shortcut.get()
        adopt = self.adopt.get()
        self.win.destroy()
        if not app._init_manager():
            return messagebox.showerror("Setup", app.status.get())
        if shortcut:
            try:
                app.log(f"Created shortcut: {wowam.create_shortcut()}")
            except wowam.WowamError as e:
                app.log(f"! {e}")
        app.pending.clear()
        app.refresh_list()
        if adopt:
            app.scan(then_check=True)
        else:
            app.check()


def run_gui() -> int:
    if tk is None:
        print("Tkinter isn't installed. On Bazzite/Fedora run the CLI instead, or install it "
              "in a toolbox/distrobox:  sudo dnf install python3-tkinter")
        return 1
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_gui())
