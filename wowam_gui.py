#!/usr/bin/env python3
"""Tkinter front-end for wowam. Run: python wowam_gui.py  (or: python wowam.py gui)"""
from __future__ import annotations

import queue
import threading
import time
import webbrowser

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # e.g. Fedora/Bazzite without python3-tkinter
    tk = None

import wowam


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = wowam.load_config()
        self.mgr: wowam.Manager | None = None
        self.pending: dict[str, dict] = {}
        self.busy = False
        self.q: queue.Queue = queue.Queue()
        self.next_check = 0.0

        root.title(f"WoW Addon Manager {wowam.__version__}")
        root.geometry("980x620")
        root.minsize(760, 420)

        bar = ttk.Frame(root, padding=(8, 8, 8, 4))
        bar.pack(fill="x")
        self.buttons = []
        for text, cmd in [("Check for updates", self.check), ("Update all", self.update_all),
                          ("Update selected", self.update_selected), ("Install…", self.open_install),
                          ("Remove", self.remove), ("Pin / Unpin", self.toggle_pin),
                          ("Scan folder", self.scan), ("Open page", self.open_page)]:
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=(0, 4))
            self.buttons.append(b)
        ttk.Button(bar, text="Settings", command=self.open_settings).pack(side="right")

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
        self.tree.tag_configure("update", foreground="#c47a00")
        self.tree.tag_configure("pinned", foreground="#777777")
        pane.add(tree_frame, weight=4)

        log_frame = ttk.Frame(pane)
        self.logbox = tk.Text(log_frame, height=7, state="disabled", wrap="word")
        self.logbox.pack(fill="both", expand=True)
        pane.add(log_frame, weight=1)

        self.status = tk.StringVar(value="Ready")
        ttk.Label(root, textvariable=self.status, anchor="w", padding=(8, 4)).pack(fill="x")

        root.after(100, self._drain)
        root.after(1000, self._tick)
        if self._init_manager():
            self.refresh_list()
            self.check()
        else:
            root.after(300, self.open_settings)

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
            self.open_settings()
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
                    self.log(f"! {err}")
                    self.status.set(err)
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

    def _tick(self):
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
                status, tag = "Update available", "update"
                n_upd += 1
            elif e.get("file_id") is None:
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
        self._schedule_next()
        self.run_bg("Checking for updates", work, done)

    def update_all(self, auto=False):
        self._schedule_next()
        self.run_bg("Updating" + (" (auto)" if auto else ""), lambda: self.mgr.update(),
                    lambda _: self.pending.clear())

    def update_selected(self):
        ids = self.selected()
        if not ids:
            return messagebox.showinfo("Update", "Select one or more addons first.")
        self.run_bg("Updating selected", lambda: self.mgr.update(ids),
                    lambda _: [self.pending.pop(i, None) for i in ids])

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

    def scan(self):
        self.run_bg("Scanning AddOns folder", self.mgr.scan)

    def open_page(self):
        for i in self.selected()[:3]:
            url = self.mgr.installed[i].get("url") or f"https://www.curseforge.com/wow/addons/{self.mgr.installed[i].get('slug')}"
            webbrowser.open(url)

    # ------------------------------------------------------------ install dialog
    def open_install(self):
        if not self.mgr and not self._init_manager():
            return self.open_settings()
        win = tk.Toplevel(self.root)
        win.title("Install addon")
        win.geometry("720x460")
        win.transient(self.root)
        top = ttk.Frame(win, padding=8)
        top.pack(fill="x")
        term = tk.StringVar()
        ent = ttk.Entry(top, textvariable=term)
        ent.pack(side="left", fill="x", expand=True)
        ent.focus_set()
        tree = ttk.Treeview(win, columns=("dl", "summary"), selectmode="extended")
        tree.heading("#0", text="Name"); tree.heading("dl", text="Downloads"); tree.heading("summary", text="Summary")
        tree.column("#0", width=200); tree.column("dl", width=90, anchor="e"); tree.column("summary", width=400)
        tree.pack(fill="both", expand=True, padx=8)
        info = tk.StringVar(value="Search by name, or paste a CurseForge URL / slug / ID and press Install.")
        ttk.Label(win, textvariable=info, padding=8).pack(fill="x")

        def do_search(*_):
            t = term.get().strip()
            if not t:
                return
            info.set("Searching…")

            def bg():
                try:
                    hits = self.mgr.cf.search(t, flavor=self.mgr.flavor)
                except wowam.WowamError as e:
                    return self.ui(lambda: info.set(str(e)))

                def show():
                    tree.delete(*tree.get_children())
                    for h in hits:
                        mark = "  ✓" if str(h["id"]) in self.mgr.installed else ""
                        tree.insert("", "end", iid=str(h["id"]), text=h["name"] + mark,
                                    values=(f"{h.get('downloadCount', 0):,.0f}", h.get("summary", "")))
                    info.set(f"{len(hits)} result(s). Select and press Install.")
                self.ui(show)
            threading.Thread(target=bg, daemon=True).start()

        def do_install():
            refs = list(tree.selection()) or ([term.get().strip()] if term.get().strip() else [])
            if not refs:
                return
            win.destroy()
            self.run_bg("Installing", lambda: [self.mgr.install(r) for r in refs])

        ent.bind("<Return>", do_search)
        tree.bind("<Double-1>", lambda _: do_install())
        ttk.Button(top, text="Search", command=do_search).pack(side="left", padx=4)
        ttk.Button(top, text="Install", command=do_install).pack(side="left")

    # ------------------------------------------------------------ settings
    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.transient(self.root)
        f = ttk.Frame(win, padding=12)
        f.pack(fill="both", expand=True)
        v = {
            "api_key": tk.StringVar(value=self.cfg.get("api_key", "")),
            "addons_dir": tk.StringVar(value=self.cfg.get("addons_dir", "")),
            "flavor": tk.StringVar(value=str(self.cfg.get("flavor", "auto"))),
            "channel": tk.StringVar(value=self.cfg.get("channel", "release")),
            "auto_check_minutes": tk.StringVar(value=str(self.cfg.get("auto_check_minutes", 60))),
            "auto_update": tk.BooleanVar(value=bool(self.cfg.get("auto_update"))),
            "install_dependencies": tk.BooleanVar(value=bool(self.cfg.get("install_dependencies", True))),
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
        row("Auto-check every (min, 0 = off)", ttk.Entry(f, textvariable=v["auto_check_minutes"], width=8))
        row("", ttk.Checkbutton(f, text="Install updates automatically (not just notify)", variable=v["auto_update"]))
        row("", ttk.Checkbutton(f, text="Install required dependencies", variable=v["install_dependencies"]))

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
                self.cfg[k] = val.strip() if isinstance(val, str) else val
            wowam.save_config(self.cfg)
            win.destroy()
            if self._init_manager():
                self.pending.clear()
                self.refresh_list()
                if not self.mgr.installed:
                    if messagebox.askyesno("Scan", "Adopt the addons already in this AddOns folder?"):
                        return self.scan()
                self.check()
            else:
                messagebox.showerror("Settings", self.status.get())

        ttk.Button(f, text="Save", command=save).grid(row=r, column=1, sticky="e", pady=(10, 0))
        f.columnconfigure(1, weight=1)


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
