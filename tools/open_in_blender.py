r"""Open a job's output folder in the host Blender as a labelled comparison scene:
front row = textured (master, optimized, LOD1.., collision), back row = the same meshes with a plain grey material.

Usage:  python tools/open_in_blender.py samples/crate [--blender PATH]      or      assetctl view JOB_ID
Importable: open_sample(dir). Blender is located via STUDIO_BLENDER, an explicit path, PATH, or the newest
"Blender Foundation" install under Program Files (Windows) / the usual macOS and Linux locations.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

SCENE_SCRIPT = r'''
import bpy, struct, json, traceback
bpy.ops.wm.read_factory_settings(use_empty=True)
base = BASE
items = ITEMS

def tris(path):
    with open(path, "rb") as f:
        f.read(12); clen, _ = struct.unpack("<II", f.read(8)); j = json.loads(f.read(clen))
    n = 0
    for m in j.get("meshes", []):
        for p in m["primitives"]:
            n += j["accessors"][p["indices"]]["count"] // 3 if "indices" in p else j["accessors"][p["attributes"]["POSITION"]]["count"] // 3
    return n

grey = bpy.data.materials.new("untextured_grey"); grey.use_nodes = True
grey.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.6, 0.6, 0.6, 1)

def add_label(text, x, y, z, size):
    c = bpy.data.curves.new(text, "FONT"); c.body = text; c.size = size; c.align_x = "CENTER"
    o = bpy.data.objects.new(text, c); bpy.context.scene.collection.objects.link(o)
    o.location = (x, y, z); o.rotation_euler = (1.5708, 0, 0)

# import everything first so the master can be shown at the optimized asset's real-world size
loaded = []
for i, (label, f) in enumerate(items):
    path = base + "/" + f
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    new = [o for o in bpy.data.objects if o not in before]
    loaded.append((f"{label} {tris(path):,} tris", new, [o for o in new if o.type == "MESH"], label))
bpy.context.view_layer.update()
def height(meshes):
    import numpy as np
    zs = []
    for o in meshes:
        n = len(o.data.vertices)
        if not n:
            continue
        co = np.empty(n * 3, dtype=np.float32); o.data.vertices.foreach_get("co", co); co = co.reshape(-1, 3)
        M = np.array(o.matrix_world, dtype=np.float64)
        w = co @ M[:3, :3].T + M[:3, 3]
        zs += [float(w[:, 2].min()), float(w[:, 2].max())]
    return (max(zs) - min(zs)) if zs else 1.0
ref = next((m for _, _, m, lab in loaded if lab == "OPTIMIZED" and m), None) or loaded[0][2]
ref_h = height(ref) or 1.0
spacing, off = ref_h * 1.8, ref_h * 1.3
add_label(TITLE, (len(loaded) - 1) * spacing / 2, -off * 0.7, off * 1.9, spacing * 0.16)
for i, (label, new, meshes, kind) in enumerate(loaded):
    x = i * spacing
    scale = 1.0
    if kind == "MASTER" and meshes:
        h = height(meshes)
        if h > 0 and abs(h - ref_h) / ref_h > 0.02:
            scale = ref_h / h
            label += f" (display-scaled x{scale:.3g})"
    for o in [o for o in new if o.parent is None]:
        o.name = "TEX_" + label
        o.scale = (o.scale[0] * scale, o.scale[1] * scale, o.scale[2] * scale)
        o.location.x += x
    add_label(label, x, -off * 0.7, off * 1.15 + (0 if i % 2 == 0 else off * 0.25), spacing * 0.07)
    for m in meshes:
        c = m.copy(); c.data = m.data.copy(); c.name = "UNTEX_" + label
        bpy.context.scene.collection.objects.link(c); c.parent = None
        ws = m.matrix_world.to_scale()
        c.scale = (ws[0] * scale, ws[1] * scale, ws[2] * scale)
        c.rotation_euler = m.matrix_world.to_euler()
        c.location = (x, off * 1.6, 0.0)
        c.data.materials.clear(); c.data.materials.append(grey)
    add_label(label + " (no textures)", x, off * 1.6 + off * 0.9, off * 1.15 + (0 if i % 2 == 0 else off * 0.25), spacing * 0.07)

def set_shading():
    for a in bpy.context.screen.areas:
        if a.type == "VIEW_3D":
            for sp in a.spaces:
                if sp.type == "VIEW_3D":
                    sp.shading.type = "MATERIAL"; sp.shading.use_scene_lights = False; sp.shading.use_scene_world = False
            for r in a.regions:
                if r.type == "WINDOW":
                    with bpy.context.temp_override(area=a, region=r):
                        try: bpy.ops.view3d.view_all()
                        except Exception: pass
    return None
bpy.app.timers.register(set_shading, first_interval=0.5)
'''


def find_blender(explicit: str | None = None) -> str | None:
    cands = [explicit, os.environ.get("STUDIO_BLENDER"), shutil.which("blender")]
    for pf in (os.environ.get("ProgramW6432", ""), os.environ.get("ProgramFiles", r"C:\Program Files")):
        if pf:
            cands += sorted(glob.glob(os.path.join(pf, "Blender Foundation", "Blender*", "blender.exe")), reverse=True)
    cands += ["/Applications/Blender.app/Contents/MacOS/Blender", "/usr/bin/blender", "/snap/bin/blender"]
    for c in cands:
        if c and Path(c).exists():
            return str(c)
    return None


def collect_items(d: Path) -> list[tuple[str, str]]:
    items = [("MASTER", "master.glb")] if (d / "master.glb").exists() else []
    mp = d / "manifest.json"
    if mp.exists():
        asset = json.loads(mp.read_text(encoding="utf-8"))["asset"]
        if asset.get("optimized"):
            items.append(("OPTIMIZED", asset["optimized"]))
        items += [(f"LOD{i + 1}", lod["file"]) for i, lod in enumerate(asset.get("lods") or [])]
        if asset.get("collision"):
            items.append(("COLLISION", asset["collision"]))
    else:
        for p in sorted(d.glob("*.glb")):
            if p.name == "master.glb":
                continue
            stem = p.stem
            label = "COLLISION" if stem.endswith("_collision") else (stem.rsplit("_", 1)[-1].upper() if "_LOD" in stem else "OPTIMIZED")
            items.append((label, p.name))
        order = {"OPTIMIZED": 1, "COLLISION": 9}
        items.sort(key=lambda it: (0 if it[0] == "MASTER" else order.get(it[0], 5), it[0]))
    return items


def open_sample(sample_dir, blender: str | None = None) -> dict:
    d = Path(sample_dir).resolve()
    exe = find_blender(blender)
    if not exe:
        raise SystemExit("Blender not found: install Blender or set STUDIO_BLENDER=<path to blender executable>")
    items = collect_items(d)
    if not items:
        raise SystemExit(f"no GLB files in {d}")
    title = d.name.upper()
    mp = d / "manifest.json"
    if mp.exists():
        req = json.loads(mp.read_text(encoding="utf-8")).get("request", {})
        title = f"{d.name.upper()}: {req.get('prompt', '')[:70]}"
    script = SCENE_SCRIPT.replace("BASE", repr(d.as_posix())).replace("ITEMS", repr(items)).replace("TITLE", repr(title))
    tmp = Path(tempfile.gettempdir()) / f"asset_studio_view_{d.name}.py"
    tmp.write_text(script, encoding="utf-8")
    subprocess.Popen([exe, "--python", str(tmp)], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return {"blender": exe, "sample_dir": str(d), "files": [f for _, f in items]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sample_dir")
    ap.add_argument("--blender", default=None)
    a = ap.parse_args()
    print(json.dumps(open_sample(a.sample_dir, a.blender)))
