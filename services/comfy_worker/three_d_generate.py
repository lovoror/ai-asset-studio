"""Pixal3D/TRELLIS.2 stage on a remote ComfyUI server.

Drop-in replacement for `python -m pixal3d_worker.generate` when the job's 3D backend is comfyui: same
request.json in, same master.glb + output.json out, so the Blender / validate / package stages downstream
are unchanged. `_write_output` and `phase` are imported from pixal3d_worker so the output shape and the
"[phase] x" markers the orchestrator greps are identical to the local path.

Two differences from the local path that matter:

  * No Z-up -> Y-up rotation. pixal3d_worker applies one because o_voxel exports a Z-up voxel grid; the
    ComfyUI TRELLIS.2 workflow's Save3DAdvanced already writes glTF-convention Y-up, so applying the matrix
    again would lay the model on its side.
  * Node ids come from Settings, because unlike a KSampler graph there is no unambiguous way to derive which
    PrimitiveInt feeds the master texture bake. Each role has a default input key and also accepts an
    explicit "<node id>:<input key>" override.

Nothing here needs a GPU or torch.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import struct
import sys
import time
import traceback
from pathlib import Path

from comfy_worker.comfy_client import (ComfyError, Interrupted, download, find_nodes, find_workflow_file,
                                       get_json, iter_outputs, load_workflow, log, phase, probe, retract,
                                       run_workflow, set_node_input, upload_image)
from pixal3d_worker.generate import _write_output

DEFAULT_TIMEOUT_S = float(os.environ.get("STUDIO_COMFY_TIMEOUT_S", "5400"))

# role -> the input keys to try, in order. A setting may also carry an explicit key as "<node id>:<key>".
# If none of these exist and the node has exactly one scalar input, that one is patched instead, so a
# PrimitiveInt wrapper works without being told its key is called "value".
ROLE_KEYS = {
    "branch": ("value", "boolean"),
    "seed": ("seed", "noise_seed", "value"),
    "resolution": ("target_resolution", "resolution", "value"),
    "decimate": ("target_face_count", "face_count", "target_count", "value"),
    "texture": ("value", "texture_size", "resolution", "size"),
    "normal": ("resolution", "size", "value"),
}
# The deliverable. The TRELLIS.2 workflow has several Save3D-ish nodes and only one of them is the final
# asset, so an explicit `output` setting wins; otherwise the first of these classes found is used.
OUTPUT_CLASSES = ("Save3DAdvanced", "Save3D", "SaveGLB", "SaveMesh3D")
IMAGE_CLASSES = ("LoadImage",)
# Pixal3D's multi-view conditioning takes four named views instead of the single reference image. Which
# LoadImage feeds which slot is read off the graph rather than configured, so the workflow stays the single
# source of truth for its own wiring.
MULTIVIEW_CLASS = "Pixal3DMultiViewConditioning"
VIEW_SLOTS = ("front", "left", "back", "right")


# ----------------------------------------------------------------------------------------------- glb summary

def glb_summary(path: Path) -> dict:
    """Face/vertex counts and a corruption check, read straight out of the GLB (stdlib only).

    master_faces is what the API surfaces as the asset's master triangle count, so it has to come from the
    file rather than from what we asked for - the remote decimation node may not hit the target exactly.
    Only the 12-byte header and the JSON chunk are read; a TRELLIS.2 master is tens of MB of binary buffer
    that has nothing to say about the counts.
    """
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"glTF":
            raise ComfyError(f"{path.name} is not a GLB (first bytes {head[:8]!r}) - the server may have "
                             f"returned an error page instead of a model")
        version, length = struct.unpack("<II", head[4:12])
        if length != size:
            log(f"[glb] warning: header length {length} != file size {size}")
        chunk_head = f.read(8)
        if len(chunk_head) < 8:
            raise ComfyError(f"{path.name}: truncated before the first chunk")
        chunk_len, chunk_type = struct.unpack("<II", chunk_head)
        if chunk_type != 0x4E4F534A:   # "JSON"
            raise ComfyError(f"{path.name}: first GLB chunk is not JSON (type {chunk_type:#x})")
        # Chunks are padded to a 4-byte boundary. The spec says to pad the JSON chunk with spaces, but some
        # exporters use NULs, and json.loads tolerates trailing whitespace only - so strip both.
        doc = json.loads(f.read(chunk_len).rstrip(b" \t\r\n\x00").decode("utf-8"))
    accessors = doc.get("accessors") or []
    faces = verts = n_prim = 0
    has_uv = True
    for mesh in doc.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            n_prim += 1
            attrs = prim.get("attributes") or {}
            idx = prim.get("indices")
            if isinstance(idx, int) and idx < len(accessors):
                faces += int(accessors[idx].get("count", 0)) // 3
            pos = attrs.get("POSITION")
            if isinstance(pos, int) and pos < len(accessors):
                n = int(accessors[pos].get("count", 0))
                verts += n
                if idx is None:
                    faces += n // 3
            if "TEXCOORD_0" not in attrs:
                has_uv = False
    return {"master_faces": faces, "master_vertices": verts, "primitives": n_prim,
            "has_uv": has_uv and n_prim > 0, "glb_version": version,
            "byte_length": size,
            "images": len(doc.get("images") or []), "materials": len(doc.get("materials") or []),
            "generator": (doc.get("asset") or {}).get("generator"),
            "copyright": (doc.get("asset") or {}).get("copyright")}


# ----------------------------------------------------------------------------------------------- node patching

def parse_node_ref(spec) -> tuple[str, str | None]:
    """Accept "94" or "94:target_resolution". Node ids in API-format workflows are strings."""
    s = str(spec).strip()
    if ":" in s:
        nid, _, key = s.partition(":")
        return nid.strip(), key.strip() or None
    return s, None


def _scalar_inputs(node: dict) -> list[str]:
    """Inputs holding a literal scalar (not a link), i.e. the ones safe to overwrite."""
    out = []
    for k, v in (node.get("inputs") or {}).items():
        if isinstance(v, (int, float, str, bool)) and not isinstance(v, list):
            out.append(k)
    return out


def snap_combo(server: str, class_type: str, key: str, want: int) -> str | None:
    """Pick the /object_info option closest to `want`, for combo inputs like target_resolution.

    The quality presets carry resolution as an int (1024, 1536) but TRELLIS.2's upsample node takes a
    combo string, and an invalid option is a hard /prompt validation error rather than a warning.
    """
    try:
        info = get_json(server, f"/object_info/{class_type}", timeout=30.0)
    except Exception as e:  # noqa: BLE001
        log(f"[comfy] /object_info/{class_type} failed ({e}); passing {want} through unchanged")
        return None
    spec = (info.get(class_type) or {}).get("input", {})
    opts = None
    for group in ("required", "optional"):
        entry = (spec.get(group) or {}).get(key)
        if isinstance(entry, list) and entry and isinstance(entry[0], list):
            opts = entry[0]
            break
    if not opts:
        return None
    best, best_d = None, None
    for o in opts:
        m = re.search(r"(\d{3,5})", str(o))
        if not m:
            continue
        d = abs(int(m.group(1)) - want)
        if best_d is None or d < best_d:
            best, best_d = str(o), d
    if best is not None and best_d:
        log(f"[comfy] {class_type}.{key}: snapped {want} -> {best!r} (nearest allowed option)")
    return best


def patch_role(wf: dict, server: str, role: str, spec, value, warnings: list) -> bool:
    """Write `value` into one configured node. Returns False (and warns) when it could not be applied."""
    if spec in (None, ""):
        return False
    nid, explicit = parse_node_ref(spec)
    node = wf.get(nid)
    if node is None:
        warnings.append(f"node {nid} (role {role}) is not in the workflow; {role} was left at its own value")
        return False
    class_type = node.get("class_type")
    keys = [explicit] if explicit else list(ROLE_KEYS.get(role, ("value",)))
    # A combo input needs one of the server's allowed options, not a bare int.
    if role == "resolution" and isinstance(value, int):
        for k in keys:
            if k in (node.get("inputs") or {}):
                snapped = snap_combo(server, class_type, k, value)
                if snapped is not None:
                    value = snapped
                break
    for k in keys:
        if k and set_node_input(wf, nid, k, value):
            log(f"[comfy] node {nid} ({class_type}).{k} = {value!r}   [{role}]")
            return True
    scalars = _scalar_inputs(node)
    if not explicit and len(scalars) == 1:
        set_node_input(wf, nid, scalars[0], value)
        log(f"[comfy] node {nid} ({class_type}).{scalars[0]} = {value!r}   [{role}, only scalar input]")
        return True
    warnings.append(f"node {nid} ({class_type}) has no {keys} input for role {role}; available scalars: "
                    f"{scalars}. Use the '<node id>:<input key>' form to say which one to patch.")
    return False


def pick_output_node(wf: dict, nodes: dict, warnings: list) -> str | None:
    """The node whose artifact is the deliverable.

    Tried class by class, most specific first, so a workflow that keeps an intermediate Save3D wired to a
    preview alongside the real Save3DAdvanced still resolves to one node instead of downloading both.
    """
    if nodes.get("output"):
        nid, _ = parse_node_ref(nodes["output"])
        if nid in wf:
            return nid
        warnings.append(f"configured output node {nid} is not in the workflow; falling back to class_type")
    for cls in OUTPUT_CLASSES:
        found = find_nodes(wf, cls)
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            warnings.append(f"the workflow has {len(found)} {cls} nodes {found}; downloading every 3D file and "
                            f"keeping the largest. Set the 'output' node id to pick one explicitly.")
            return None
    warnings.append(f"no save node found (looked for {OUTPUT_CLASSES}); downloading whatever 3D files appear")
    return None


def patch_image_input(wf: dict, server: str, image_path: Path, nodes: dict, warnings: list) -> None:
    """Upload the reference image and point the workflow's LoadImage at it."""
    phase("preprocess")
    name = upload_image(server, image_path)
    loaders = find_nodes(wf, *IMAGE_CLASSES)
    if nodes.get("image"):
        nid, _ = parse_node_ref(nodes["image"])
        if nid in loaders:
            loaders = [nid]
        else:
            warnings.append(f"configured image node {nid} is not a LoadImage (found {loaders})")
    if not loaders:
        raise ComfyError("the workflow has no LoadImage node, so the reference image cannot be wired in")
    if len(loaders) > 1:
        warnings.append(f"the workflow has {len(loaders)} LoadImage nodes {loaders}; patched all of them. "
                        f"Set the 'image' node id to pick one.")
    for nid in loaders:
        set_node_input(wf, nid, "image", name)
    log(f"[comfy] uploaded {image_path.name} as {name!r}; patched LoadImage {loaders}")


def multiview_node(wf: dict) -> str | None:
    """The multi-view conditioning node, if this workflow has one."""
    found = find_nodes(wf, MULTIVIEW_CLASS)
    return found[0] if found else None


def upstream_loader(wf: dict, node_id: str, seen: frozenset = frozenset()) -> str | None:
    """The LoadImage a node's image input ultimately comes from, walking back through the preprocessing."""
    node = wf.get(str(node_id))
    if node is None or str(node_id) in seen:
        return None
    if node.get("class_type") in IMAGE_CLASSES:
        return str(node_id)
    for val in (node.get("inputs") or {}).values():
        if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str):
            found = upstream_loader(wf, val[0], seen | {str(node_id)})
            if found:
                return found
    return None


def view_loaders(wf: dict, mv_id: str) -> dict[str, str]:
    """{slot: LoadImage node id} for the view slots this workflow actually wired up."""
    out: dict[str, str] = {}
    for slot in VIEW_SLOTS:
        ref = (wf[mv_id].get("inputs") or {}).get(slot)
        if isinstance(ref, list) and len(ref) == 2 and isinstance(ref[0], str):
            loader = upstream_loader(wf, ref[0])
            if loader:
                out[slot] = loader
    return out


def patch_multiview_input(wf: dict, server: str, views: dict, fov: float | None, warnings: list) -> None:
    """Upload every view and point the multi-view conditioning node's slots at them.

    `views` is {slot: local path}; the control plane owns the frame -> slot mapping (see studio.multiview),
    because that mapping depends on the caller's camera matrices rather than on the workflow. Node ids are
    deliberately not configured here: each slot is followed back through the graph to the LoadImage that
    feeds it, so editing the workflow does not silently break the wiring.

    A slot with no view is disconnected rather than left alone. The node takes its views as optional inputs and
    re-bases its rig on the first one it is given, so it reconstructs quite happily from fewer views - which is
    what the 2D stage produces when the sheet comes back with fewer usable panels. Leaving the slot wired would
    instead point its LoadImage at the filename the workflow ships, and a name with no file behind it fails the
    stage before anything renders: measured on a real job, "node 410 (LoadImage): image - Invalid image file:
    view_410.png". Nothing needs tidying up behind the disconnected slot either - ComfyUI collects the
    OUTPUT_NODE nodes and then validates only what it can reach back from them (execution.py:1128), so the
    orphaned view branch is neither validated nor executed.
    """
    phase("preprocess")
    mv_id = multiview_node(wf)
    if mv_id is None:
        raise ComfyError(f"the workflow has no {MULTIVIEW_CLASS} node, so it cannot take multi-view input")
    loaders = view_loaders(wf, mv_id)
    if not loaders:
        raise ComfyError(f"the {MULTIVIEW_CLASS} node ({mv_id}) has no view wired to a LoadImage")
    missing = [s for s in (views or {}) if s not in loaders]
    if missing:
        warnings.append(f"the workflow does not wire the {', '.join(missing)} view(s); they were ignored")
    for slot in VIEW_SLOTS:
        path = (views or {}).get(slot)
        if not path or slot not in loaders:
            continue
        name = upload_image(server, Path(path))
        set_node_input(wf, loaders[slot], "image", name)
        log(f"[comfy] {slot} view: uploaded {Path(path).name} as {name!r} -> LoadImage {loaders[slot]}")
    unused = [s for s in VIEW_SLOTS if s in loaders and not (views or {}).get(s)]
    for slot in unused:
        (wf[mv_id].get("inputs") or {}).pop(slot, None)
    if unused:
        log(f"[comfy] no view for {', '.join(unused)}: disconnected, so the rig is rebuilt from the "
            f"{len(views or {})} view(s) there are")
    if fov is not None:
        # A literal here replaces the workflow's MoGeGeometryToFOV link: the caller stated the FOV, so there
        # is nothing to measure.
        if set_node_input(wf, mv_id, "fov", float(fov)):
            log(f"[comfy] {MULTIVIEW_CLASS} {mv_id}.fov = {float(fov):.2f}   [caller-declared]")
        else:
            warnings.append(f"{MULTIVIEW_CLASS} has no fov input; the declared FOV was ignored")


# ----------------------------------------------------------------------------------------------- stage

def run(req: dict) -> None:
    t_start = time.time()
    out_dir = Path(req["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    backend = req.get("backend") or {}
    warnings: list[str] = []
    timings: dict = {}
    if backend.get("kind") != "comfyui" or not backend.get("url"):
        raise ComfyError(f"request.json has no usable comfyui 3D backend: {json.dumps(backend)[:200]}. "
                         f"Set Settings -> Generation backends.")
    server = backend["url"]
    nodes = backend.get("nodes") or {}
    exp = req.get("export") or {}

    # A multi-view job carries the four named views instead of one reference image.
    multiview = req.get("mode") == "multiview"
    views = req.get("views") or {}
    image_path = req.get("image_path")
    if multiview:
        if not views:
            raise ComfyError("request.json has mode=multiview but carries no views: the control plane maps the "
                             "frames onto the front/left/back/right slots before starting the stage")
    elif not image_path or not Path(image_path).is_file():
        raise ComfyError(f"reference image not found: {image_path!r}. The control plane uploads the stage's "
                         f"inputs before starting it; this usually means the request carried a path outside "
                         f"the job directory, which cannot be transferred to a worker on another machine.")

    phase("load")
    t0 = time.time()
    remote = {}
    try:
        remote = probe(server)
        log(f"[comfy] server {server}: ComfyUI {remote.get('comfyui_version')}, gpu {remote.get('gpu')}, "
            f"vram {remote.get('vram_total_mib')} MiB")
    except Exception as e:  # noqa: BLE001
        warnings.append(f"could not read {server}/system_stats ({e}); continuing anyway")

    template = load_workflow(find_workflow_file(backend["workflow"]))
    is_multiview = multiview_node(template) is not None
    if multiview and not is_multiview:
        raise ComfyError(f"{backend['workflow']} has no {MULTIVIEW_CLASS} node, so it cannot generate from "
                         f"multiple views; set the multi-view workflow in Settings -> Generation backends")
    log(f"[comfy] 3D workflow {backend['workflow']}: {len(template)} nodes, "
        f"{'Pixal3D multi-view' if is_multiview else ('TRELLIS.2' if backend.get('trellis2', True) else 'Pixal3D')}")
    timings["load_s"] = round(time.time() - t0, 2)

    wf = copy.deepcopy(template)
    if is_multiview:
        patch_multiview_input(wf, server, views, req.get("view_fov_degrees"), warnings)
    else:
        patch_image_input(wf, server, Path(image_path), nodes, warnings)

    # The knobs. Each is optional: an unconfigured role leaves the workflow's own value alone.
    phase("generate")
    if not is_multiview:
        # The multi-view workflow is Pixal3D-only: it has no TRELLIS.2/Pixal3D branch node to write into.
        patch_role(wf, server, "branch", nodes.get("branch"), bool(backend.get("trellis2", True)), warnings)
    if req.get("seed") is not None:
        patch_role(wf, server, "seed", nodes.get("seed"), int(req["seed"]), warnings)
    if req.get("resolution") is not None:
        patch_role(wf, server, "resolution", nodes.get("resolution"), int(req["resolution"]), warnings)
    if exp.get("decimation_target") is not None:
        patch_role(wf, server, "decimate", nodes.get("decimate"), int(exp["decimation_target"]), warnings)
    if exp.get("texture_size") is not None:
        patch_role(wf, server, "texture", nodes.get("texture"), int(exp["texture_size"]), warnings)

    timeout_s = float(req.get("timeout_s") or DEFAULT_TIMEOUT_S)
    t0 = time.time()
    entry = run_workflow(server, wf, timeout_s, client_id="asset-studio-3d")
    timings["generate_s"] = round(time.time() - t0, 2)

    phase("export")
    t0 = time.time()
    out_node = pick_output_node(wf, nodes, warnings)
    arts = [a for a in iter_outputs(entry.get("outputs", {}), only_nodes={out_node} if out_node else None)
            if a[1].lower().endswith((".glb", ".gltf", ".obj", ".stl", ".fbx", ".ply"))]
    if not arts:
        arts = [a for a in iter_outputs(entry.get("outputs", {}))
                if a[1].lower().endswith((".glb", ".gltf", ".obj", ".stl", ".fbx", ".ply"))]
        if arts:
            warnings.append("the configured output node produced nothing; used another 3D file from the run")
    if not arts:
        raise ComfyError(f"the prompt succeeded but produced no 3D file (outputs: "
                         f"{json.dumps(entry.get('outputs', {}))[:600]})")
    if len(arts) > 1:
        log(f"[comfy] {len(arts)} 3D artifacts {[(a[0], a[1]) for a in arts]}; downloading all and keeping the largest")
    master = out_dir / "master.glb"
    biggest, biggest_n = None, -1
    for nid, name, sub, ftype in arts:
        dest = master if len(arts) == 1 else out_dir / f"comfy_{nid}_{name}"
        n = download(server, name, sub, ftype, dest)
        if n > biggest_n:
            biggest, biggest_n = dest, n
    if len(arts) > 1:
        biggest.replace(master)
        for nid, name, sub, ftype in arts:
            p = out_dir / f"comfy_{nid}_{name}"
            if p != master and p.exists():
                p.unlink()
    with open(master, "rb") as f:
        magic = f.read(4)
    if magic != b"glTF":
        # Only the container format can be checked here; the orchestrator's validate stage covers the rest
        # with pygltflib. A non-GLB usually means Save3DAdvanced was set to .obj/.stl, or the server returned
        # an error page with a 200 and it was saved as the model.
        raise ComfyError(f"{master.name} starts with {magic!r}, not b'glTF': Save3DAdvanced must write .glb "
                         f"for the Blender stage to accept it")
    timings["export_s"] = round(time.time() - t0, 2)

    stats = glb_summary(master)
    log(f"[glb] {stats['master_faces']:,} faces / {stats['master_vertices']:,} verts, {stats['images']} images, "
        f"{stats['materials']} materials, uv={stats['has_uv']}, {stats['byte_length'] / 1e6:.1f} MB, "
        f"generator={stats.get('generator')}")
    if not stats["master_faces"]:
        warnings.append("the downloaded GLB reports 0 faces")
    if not stats["has_uv"]:
        warnings.append("a primitive in the master GLB has no TEXCOORD_0; the Blender bake needs UVs")
    target = exp.get("decimation_target")
    if target and stats["master_faces"] and abs(stats["master_faces"] - target) / target > 0.25:
        warnings.append(f"master has {stats['master_faces']:,} faces but {target:,} were requested "
                        f"(the remote decimation node did not hit the target)")

    result = {
        "master_glb": "master.glb",
        # No preprocessed.png: the workflow does its own background removal and camera fit internally and
        # does not export them. The orchestrator treats this key as optional.
        "camera_params": None,
        "mesh_stats": stats,
        "effective": {"resolution": req.get("resolution"), "branch": "trellis2" if backend.get("trellis2", True) else "pixal3d",
                      "seed": req.get("seed"), "low_vram": None, "max_num_tokens": None, "attn_backend": None,
                      "export": exp, "backend": "comfyui", "server": server,
                      "workflow": backend.get("workflow"),
                      "nodes": {k: v for k, v in nodes.items() if v},
                      "up_axis": "y (glTF convention; no rotation applied, unlike the local exporter)"},
    }
    # Deliberately no vram figures: the card is on another machine. The runner's own peak_gpu_used_mib is 0
    # in a GPU-less proxy container, which is what makes the portal say "remote backend".
    _write_output(out_dir, result, timings, {}, warnings, t_start)
    if remote:
        log(f"[comfy] remote gpu was: {remote.get('gpu')} ({remote.get('vram_total_mib')} MiB total)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--request", help="path to the stage request.json written by the orchestrator")
    ap.add_argument("--probe", metavar="SERVER", help="just report the server's identity and exit")
    a = ap.parse_args()
    try:
        if a.probe:
            print(json.dumps(probe(a.probe), indent=2))
            return
        run(json.loads(Path(a.request).read_text(encoding="utf-8")))
    except Interrupted:
        retract()
        sys.exit(143)
    except ComfyError as e:
        print(f"ERROR: {e}", flush=True)
        sys.exit(1)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
