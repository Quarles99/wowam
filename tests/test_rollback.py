"""A failed folder swap must leave the previous version fully intact (no network, any OS)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import wowam


def make(root: Path, name: str, text: str):
    (root / name).mkdir(parents=True)
    (root / name / f"{name}.toc").write_text(text)
    (root / name / "core.lua").write_text(text)


def main() -> int:
    base = Path(tempfile.mkdtemp())
    addons = base / "_retail_" / "Interface" / "AddOns"
    addons.mkdir(parents=True)
    m = wowam.Manager({"api_key": "x", "addons_dir": str(addons), "flavor": "retail"}, log=lambda *_: None)
    make(addons, "Alpha", "old")
    make(addons, "Beta", "old")
    staging = base / "_retail_" / "Interface" / ".wowam-stage"
    make(staging, "Alpha", "new")
    make(staging, "Beta", "new")

    real_rename = os.rename

    def flaky(src, dst, *a, **k):  # the second folder moving into place is "locked"
        if Path(src).name == "Beta" and Path(src).parent == staging:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real_rename(src, dst, *a, **k)

    os.rename = flaky
    try:
        try:
            m._swap_in("1", staging, ["Alpha", "Beta"], ["Alpha", "Beta"])
        except wowam.WowamError as e:
            msg = str(e)
        else:
            print("FAIL: swap should have failed"); return 1
    finally:
        os.rename = real_rename

    problems = []
    for f in ("Alpha", "Beta"):
        for fn in (f"{f}.toc", "core.lua"):
            p = addons / f / fn
            if not p.exists() or p.read_text() != "old":
                problems.append(f"{p.name} not restored")
    if "Close WoW" not in msg:
        problems.append("no hint about closing WoW")
    if staging.exists():
        problems.append("staging not cleaned up")
    print("FAIL: " + "; ".join(problems) if problems else "ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
