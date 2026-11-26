"""GUI: installing a hand-downloaded zip with "Install from file…" clears its "Update available" row.

Run from run_tests.sh (mock server up, WOWAM_CONFIG_DIR isolated, NoDist installed at an old version and a newer
file published). argv[1] is a zip of the newer version. Prints "ok" on success, "skip" when Tk has no display."""
import os
import sys
import time

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
app = wowam_gui.App(root)


def wait_idle():
    end = time.time() + 30
    root.update()
    while (app.busy or time.time() < end - 29.5) and time.time() < end:
        root.update()
        time.sleep(0.02)
    root.update()


wait_idle()  # the startup check
assert "300" in app.pending and app.pending["300"]["blocked"], f"expected a manual update pending: {app.pending}"
wowam_gui.filedialog.askopenfilenames = lambda **k: (sys.argv[1],)
app.install_from_file()
wait_idle()
status = app.tree.item("300", "values")[1]
assert "300" not in app.pending and status == "Up to date", f"still flagged after install: {status!r}, {app.pending}"
root.destroy()
print("ok")
