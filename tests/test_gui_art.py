"""GUI: the Warcraft theme loads the textures from the mock, installed addons get icons, and the frog easter egg
counts a click (and saves it). Run from run_tests.sh (mock server up, WOWAM_CONFIG_DIR isolated, addons installed).
Prints "ok" on success, "skip" when Tk has no display."""
import json
import os
import sys
import time

os.environ["WOWAM_FROG_SECONDS"] = "0.3"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import wowam  # noqa: E402

try:
    import tkinter as tk
    import wowam_gui
    root = tk.Tk()
except Exception as e:  # headless Linux without a display
    print(f"skip ({e})")
    sys.exit(0)

wowam.check_self_update = lambda *a, **k: None  # no network beyond the mock
problems = []

# a config from before 1.5 that saved theme "auto" (the old default) moves to the Warcraft look, once
raw = json.loads(wowam.CONFIG_PATH.read_text(encoding="utf-8"))
raw.pop("look_upgraded", None)
raw.update(theme="auto", frogs=0)
wowam.CONFIG_PATH.write_text(json.dumps(raw), encoding="utf-8")
app = wowam_gui.App(root)
saved = json.loads(wowam.CONFIG_PATH.read_text(encoding="utf-8"))
if app.theme_name != "warcraft" or saved.get("theme") != "warcraft" or not saved.get("look_upgraded"):
    problems.append(f"old 'auto' config not moved to the Warcraft look: {app.theme_name}, {saved.get('theme')}")
again = {"theme": "auto", "look_upgraded": True}
if wowam_gui.upgrade_look(again) or again["theme"] != "auto":
    problems.append("a later choice of 'auto' was overridden")


def pump_until(cond, seconds=30):
    end = time.time() + seconds
    while time.time() < end and not cond():
        root.update()
        time.sleep(0.02)
    root.update()
    return cond()


if not pump_until(lambda: app.wow_theme_ready and not app.busy):
    problems.append("Warcraft theme never loaded")
elif wowam_gui.ttk.Style().theme_use() != "wowam-warcraft":
    problems.append(f"ttk theme is {wowam_gui.ttk.Style().theme_use()}")
ids = list(app.mgr.installed)
if not ids or not pump_until(lambda: all(i in app.icon_photos for i in ids)):
    problems.append(f"icons missing: {[i for i in ids if i not in app.icon_photos]}")
elif any(str(app.tree.item(i, "image")) in ("", str(app.blank_icon)) for i in ids):
    problems.append("rows don't show their icons")
if not app.header.winfo_manager():
    problems.append("title plaque not shown")

if not pump_until(lambda: app.frog.active, 10):
    problems.append("the frog never showed up")
else:
    app.frog.clicked()
    app.frog.clicked()  # a double click on the same frog counts once
    pump_until(lambda: not app.frog.active, 5)
    saved = json.loads(wowam.CONFIG_PATH.read_text(encoding="utf-8")).get("frogs")
    if app.frog.count != 1 or saved != 1 or "1" not in str(app.frog_count.cget("text")):
        problems.append(f"frog count wrong: {app.frog.count}, saved {saved}")

wowam.update_config_file(theme="dark")
app.cfg["theme"] = "dark"
app.apply_theme()
if wowam_gui.ttk.Style().theme_use() != "clam" or app.header.winfo_manager():
    problems.append("switching to dark didn't restore the plain look")
root.destroy()
print("ok" if not problems else "FAILED: " + "; ".join(problems))
