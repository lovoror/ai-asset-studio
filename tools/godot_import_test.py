"""Real engine import test: import GLB files into a throwaway Godot 4 project headlessly and report the outcome.

  python tools/godot_import_test.py path/to/asset.glb [more.glb ...]
Godot 4.7.2 console binary is expected in tools/godot/ (downloaded by the session; see README).
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GODOT = Path(__file__).resolve().parent / "godot" / "Godot_v4.7.2-stable_win64_console.exe"


def main(files):
    out = {}
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        (proj / "assets").mkdir(parents=True)
        (proj / "project.godot").write_text('config_version=5\n[application]\nconfig/name="importtest"\nconfig/features=PackedStringArray("4.7")\n')
        for f in files:
            shutil.copy2(f, proj / "assets" / Path(f).name)
        r = subprocess.run([str(GODOT), "--headless", "--import", "--path", str(proj)], capture_output=True, text=True, timeout=900)
        log = (r.stdout or "") + (r.stderr or "")
        for f in files:
            name = Path(f).name
            imp = proj / "assets" / (name + ".import")
            scene = None
            if imp.exists():
                m = re.search(r'path="(res://[^"]+)"', imp.read_text())
                scene = m.group(1) if m else None
                imported = (proj / ".godot" / "imported").glob(name + "-*.scn")
                ok = any(True for _ in imported)
            else:
                ok = False
            errs = [l for l in log.splitlines() if name in l and ("ERROR" in l or "error" in l.lower())]
            out[name] = {"imported": ok, "import_file": imp.exists(), "scene": scene, "errors": errs[:10]}
        clean = re.sub(r"\[[0-9;]*m", "", log)
        out["_godot"] = {"exit": r.returncode, "log_tail": [l for l in clean.splitlines() if "ERROR" in l or "WARNING" in l][-20:]}
    print(json.dumps(out, indent=1))
    return 0 if all(v.get("imported") for k, v in out.items() if not k.startswith("_")) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
