#!/usr/bin/env python3
"""assetctl: dependency-free CLI for the asset-studio service (stdlib only; works with the host's Python 3).

  assetctl generate "prompt" [--style mobile_factory] [--quality quality] [--seed 1] [--height-m 2] [--triangles 12000]
                    [--texture 2048] [--no-lods] [--no-collision] [--no-fallback] [--reference image.png] [--wait] [--download DIR]
  assetctl images "prompt" [--variations 4 --style S --wait]          (step 1: image variations only)
  assetctl make3d IMAGE_JOB cand_00.png [cand_02.png] [--start --triangles N]   (step 2: 3D from chosen variations)
  assetctl release JOB_ID | hold JOB_ID | queue | library [--kind image|asset]
  assetctl status JOB_ID [--events N]
  assetctl wait JOB_ID [--timeout S]
  assetctl download JOB_ID DIR
  assetctl view JOB_ID [--dir DIR]     (open in Blender: labelled textured + untextured rows of master/optimized/LODs/collision)
  assetctl cancel JOB_ID
  assetctl retry JOB_ID [--wait] [--from-stage blender --triangles N --texture N]   (resume, or re-optimise a finished job)
  assetctl logs JOB_ID STAGE
  assetctl list
  assetctl capabilities | health
  assetctl doctor [--deep]

Environment: STUDIO_URL (default http://127.0.0.1:8090), STUDIO_API_TOKEN (bearer token when the API requires auth).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("STUDIO_URL", "http://127.0.0.1:8090").rstrip("/")
TOKEN = os.environ.get("STUDIO_API_TOKEN", "")


def _req(method: str, path: str, body=None, raw=False, timeout=60):
    data = None
    headers = {"Accept": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            msg = json.loads(payload)
        except Exception:  # noqa: BLE001
            msg = payload.decode("utf-8", "replace")
        raise SystemExit(f"HTTP {e.code} {path}: {json.dumps(msg) if not isinstance(msg, str) else msg}")
    except urllib.error.URLError as e:
        raise SystemExit(f"cannot reach {BASE}: {e.reason}. Is the service running? (scripts/start.ps1)")
    return payload if raw else json.loads(payload)


def cmd_generate(a):
    body = {"prompt": a.prompt, "style": a.style, "quality": a.quality, "generate_lods": not a.no_lods,
            "generate_collision": not a.no_collision, "allow_quality_fallback": not a.no_fallback}
    for k in ("seed", "height_m", "width_m", "depth_m", "target_triangles", "texture_size", "materials", "negative_extra",
              "reference_candidates", "collision_triangles", "preview_size"):
        v = getattr(a, k, None)
        if v is not None:
            body[k] = v
    if a.palette:
        body["palette"] = [p.strip() for p in a.palette.split(",") if p.strip()]
    if a.lod_fractions:
        body["lod_fractions"] = [float(x) for x in a.lod_fractions.split(",")]
    if a.reference:
        body["reference_image_b64"] = base64.b64encode(Path(a.reference).read_bytes()).decode()
    if a.multiview:
        body["multiview"] = _load_multiview(Path(a.multiview))
    if a.no_previews:
        body["render_previews"] = False
    if a.json_extra:
        body.update(json.loads(a.json_extra))
    j = _req("POST", "/v1/jobs", body)
    print(json.dumps(j, indent=2))
    if a.wait or a.download or a.open:
        final = _wait(j["job_id"], a.timeout)
        dest = Path(a.download) if a.download else Path("out") / j["job_id"]
        if (a.download or a.open) and final["status"] == "completed":
            _download(j["job_id"], dest)
        if a.open and final["status"] == "completed":
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
            from open_in_blender import open_sample
            open_sample(dest)
        return 0 if final["status"] == "completed" else 1
    return 0


def _load_multiview(views_dir: Path) -> dict:
    meta = json.loads((views_dir / "transforms.json").read_text(encoding="utf-8"))
    images = {}
    for fr in meta["frames"]:
        images[fr["file_path"]] = base64.b64encode((views_dir / fr["file_path"]).read_bytes()).decode()
    return {"images_b64": images, "camera_angle_x": meta["camera_angle_x"], "mesh_scale": meta.get("mesh_scale", 1.0),
            "frames": meta["frames"], "camera_source": meta.get("camera_source", "rig")}


def _wait(job_id: str, timeout: float | None):
    t0 = time.time()
    last = None
    while True:
        j = _req("GET", f"/v1/jobs/{job_id}?events=3")
        line = f"{j['status']:10s} stage={j['stage']:10s} progress={j['progress']:.2f}"
        if j.get("events"):
            line += f"  | {j['events'][-1]['message'][:100]}"
        if line != last:
            print(f"[{time.strftime('%H:%M:%S')}] {line}", flush=True)
            last = line
        if j["status"] in ("completed", "failed", "cancelled"):
            if j["status"] == "failed":
                print("ERROR:", json.dumps(j.get("error"), indent=2))
            if j.get("warnings"):
                print("warnings:", *[" - " + w for w in j["warnings"]], sep="\n")
            return j
        if timeout and time.time() - t0 > timeout:
            raise SystemExit("timeout waiting for job")
        time.sleep(3)


def _download(job_id: str, dest: Path):
    arts = _req("GET", f"/v1/jobs/{job_id}/artifacts")
    dest.mkdir(parents=True, exist_ok=True)
    for a in arts["artifacts"]:
        p = dest / a["name"]
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size == a["bytes"]:
            continue  # already downloaded
        data = _req("GET", a["url"], raw=True, timeout=600)
        tmp = p.with_name(p.name + ".part")
        tmp.write_bytes(data)
        try:
            os.replace(tmp, p)
        except OSError as e:  # file locked by a viewer: keep the new copy next to it
            alt = p.with_name(p.stem + ".new" + p.suffix)
            os.replace(tmp, alt)
            print(f"  {a['name']} is locked ({e.strerror}); saved as {alt.name}")
            continue
        print(f"  {a['name']} ({a['bytes']} bytes)")
    print(f"downloaded {len(arts['artifacts'])} artifacts to {dest}")


def cmd_view(a):
    """Download (if needed) and open the job's outputs in the host Blender: labelled textured + untextured rows."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    from open_in_blender import open_sample

    dest = Path(a.dir) if a.dir else Path("out") / a.job_id
    if not (dest / "manifest.json").exists():
        _download(a.job_id, dest)
    print(json.dumps(open_sample(dest, a.blender)))


def cmd_images(a):
    """Two-step flow, step 1: generate N image variations of an idea (no 3D)."""
    body = {"prompt": a.prompt, "style": a.style, "variations": a.variations}
    for k in ("seed", "height_m", "materials", "negative_extra", "title"):
        v = getattr(a, k, None)
        if v is not None:
            body[k] = v
    if a.palette:
        body["palette"] = [p.strip() for p in a.palette.split(",") if p.strip()]
    j = _req("POST", "/v1/image-jobs", body)
    print(json.dumps(j, indent=2))
    if a.wait:
        final = _wait(j["job_id"], a.timeout)
        if final["status"] == "completed":
            for c in _req("GET", f"/v1/jobs/{j['job_id']}").get("candidates", []):
                print(f"  {c['file']}  seed={c['seed']}  framing={c['score']}")
        return 0 if final["status"] == "completed" else 1
    return 0


def cmd_make3d(a):
    """Two-step flow, step 2: turn selected variations of an image job into 3D assets (held unless --start)."""
    out = []
    for cand in a.candidates:
        body = {"image_job_id": a.image_job_id, "candidate": cand, "quality": a.quality, "hold": not a.start,
                "generate_lods": not a.no_lods, "generate_collision": not a.no_collision, "allow_quality_fallback": not a.no_fallback}
        for k in ("height_m", "target_triangles", "texture_size", "collision_triangles", "title"):
            v = getattr(a, k, None)
            if v is not None:
                body[k] = v
        out.append(_req("POST", "/v1/asset-jobs", body))
    print(json.dumps(out, indent=2))
    if a.wait:
        rc = 0
        for j in out:
            if _wait(j["job_id"], a.timeout)["status"] != "completed":
                rc = 1
        return rc
    return 0


def cmd_release(a):
    print(json.dumps(_req("POST", f"/v1/jobs/{a.job_id}/release"), indent=2))


def cmd_hold(a):
    print(json.dumps(_req("POST", f"/v1/jobs/{a.job_id}/hold"), indent=2))


def cmd_queue(a):
    q = _req("GET", "/v1/queue")
    g = q["gpu"]["gpus"][0] if q["gpu"].get("available") else None
    if g:
        print(f"GPU {g['name']}: {g['used_mib']}/{g['total_mib']} MiB used   auto_process={q['settings']['auto_process']}")
    for j in q["items"]:
        print(f"{j['id']}  {j['status']:8s} {j['stage']:10s} {j['kind']:5s} {(j.get('title') or j['request']['prompt'])[:60]}")
    if not q["items"]:
        print("(queue is empty)")


def cmd_library(a):
    r = _req("GET", f"/v1/library?limit={a.limit}" + (f"&kind={a.kind}" if a.kind else "") + (f"&q={a.q}" if a.q else ""))
    for j in r["items"]:
        extra = f"{j.get('candidate_count', 0)} images, {j.get('children_count', 0)} assets" if j["kind"] == "image" else f"{j['request'].get('target_triangles') or ''} tris"
        print(f"{j['id']}  {j['kind']:5s} {j['status']:10s} {(j.get('title') or j['request']['prompt'])[:50]:50s} {extra}")


def cmd_status(a):
    print(json.dumps(_req("GET", f"/v1/jobs/{a.job_id}?events={a.events}"), indent=2))


def cmd_wait(a):
    j = _wait(a.job_id, a.timeout)
    return 0 if j["status"] == "completed" else 1


def cmd_download(a):
    _download(a.job_id, Path(a.dir))


def cmd_cancel(a):
    print(json.dumps(_req("POST", f"/v1/jobs/{a.job_id}/cancel"), indent=2))


def cmd_retry(a):
    body = {}
    if a.from_stage:
        body["from_stage"] = a.from_stage
    opt = {}
    for k in ("target_triangles", "texture_size", "collision_triangles", "height_m"):
        v = getattr(a, k, None)
        if v is not None:
            opt[k] = v
    if a.lod_fractions:
        opt["lod_fractions"] = [float(x) for x in a.lod_fractions.split(",")]
    if opt:
        body["optimize"] = opt
    print(json.dumps(_req("POST", f"/v1/jobs/{a.job_id}/retry", body or None), indent=2))
    if a.wait:
        return 0 if _wait(a.job_id, a.timeout)["status"] == "completed" else 1


def cmd_logs(a):
    data = _req("GET", f"/v1/jobs/{a.job_id}/logs/{a.stage}?tail={a.tail}", raw=True)
    sys.stdout.buffer.write(data)
    sys.stdout.flush()


def cmd_list(a):
    for j in _req("GET", f"/v1/jobs?limit={a.limit}"):
        print(f"{j['id']}  {j['status']:10s} {j['stage']:10s} {j['request']['prompt'][:60]}")


def cmd_capabilities(a):
    print(json.dumps(_req("GET", "/capabilities"), indent=2))


def cmd_health(a):
    print(json.dumps(_req("GET", "/health"), indent=2))


def cmd_doctor(a):
    """Environment and extension checks: host docker, images, volumes, API/runners, GPU, cached models, extensions."""
    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"[{'OK ' if cond else 'FAIL'}] {label}{(': ' + str(detail)) if detail else ''}")

    def sh(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout.strip()
        except Exception as e:  # noqa: BLE001
            return f"error: {e}"

    ver = sh(["docker", "version", "--format", "{{.Server.Version}}"])
    check("docker daemon", ver and not ver.startswith("error"), ver)
    imgs = sh(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"]).splitlines()
    for im in ("asset-studio/studio:local", "asset-studio/image-worker:local", "asset-studio/pixal3d-worker:local", "asset-studio/blender:local"):
        check(f"image {im}", im in imgs)
    vols = sh(["docker", "volume", "ls", "--format", "{{.Name}}"]).splitlines()
    for v in ("studio-models", "studio-jobs", "studio-data"):
        check(f"volume {v}", v in vols)
    try:
        h = _req("GET", "/health")
        check("api health", h.get("ok"), json.dumps({k: v.get('ok') for k, v in h["runners"].items()}))
        c = _req("GET", "/capabilities")
        g = c.get("gpu", {})
        if g.get("available"):
            gp = g["gpus"][0]
            check("gpu visible to workers", True, f"{gp['name']} {gp['used_mib']}/{gp['total_mib']} MiB used, uuid {gp['uuid']}")
            check("gpu uuid matches config", gp["uuid"] == c.get("gpu_uuid"), c.get("gpu_uuid"))
        else:
            check("gpu visible to workers", False, g.get("error"))
        check("multi-view checkpoints cached", c["features"]["multiview_checkpoints_cached"] or True,
              "yes" if c["features"]["multiview_checkpoints_cached"] else "no (optional; scripts/prefetch.ps1 -mv)")
    except SystemExit as e:
        check("api reachable", False, str(e))
    if a.deep:
        print("--- deep checks (loads models on the GPU; several minutes) ---")
        out = sh(["docker", "compose", "exec", "-T", "pixal3d-worker", "python", "-m", "pixal3d_worker.prefetch", "--verify"])
        tail = out[-2500:]
        check("pixal3d worker offline load + NAF/NATTEN forward", '"naf_forward"' in tail and "error" not in tail.lower(), tail.splitlines()[-1] if tail else "")
        out = sh(["docker", "compose", "exec", "-T", "image-worker", "python", "-m", "image_worker.generate", "--selftest"])
        check("image worker offline load", "selftest ok" in out, out.strip().splitlines()[-1] if out.strip() else "")
        out = sh(["docker", "compose", "exec", "-T", "blender", "python", "-c", "import bpy;print('bpy',bpy.app.version_string)"])
        check("blender (bpy)", out.startswith("bpy"), out)
    print("doctor:", "all checks passed" if ok else "some checks FAILED")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="assetctl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("prompt")
    g.add_argument("--style", default="mobile_factory")
    g.add_argument("--quality", default="balanced", choices=["balanced", "quality"])
    g.add_argument("--seed", type=int)
    g.add_argument("--height-m", dest="height_m", type=float)
    g.add_argument("--width-m", dest="width_m", type=float)
    g.add_argument("--depth-m", dest="depth_m", type=float)
    g.add_argument("--triangles", dest="target_triangles", type=int)
    g.add_argument("--texture", dest="texture_size", type=int)
    g.add_argument("--materials")
    g.add_argument("--palette", help="comma separated")
    g.add_argument("--negative", dest="negative_extra")
    g.add_argument("--candidates", dest="reference_candidates", type=int)
    g.add_argument("--collision-triangles", dest="collision_triangles", type=int)
    g.add_argument("--lod-fractions", dest="lod_fractions")
    g.add_argument("--preview-size", dest="preview_size", type=int)
    g.add_argument("--no-lods", action="store_true")
    g.add_argument("--no-collision", action="store_true")
    g.add_argument("--no-previews", action="store_true")
    g.add_argument("--no-fallback", action="store_true", help="allow_quality_fallback=false")
    g.add_argument("--reference", help="PNG/JPEG to use instead of generating one")
    g.add_argument("--multiview", help="directory with transforms.json + views (calibrated multi-view input)")
    g.add_argument("--json", dest="json_extra", help="extra request fields as JSON")
    g.add_argument("--wait", action="store_true")
    g.add_argument("--download", metavar="DIR")
    g.add_argument("--open", action="store_true", help="after completion, open the result in the host Blender (labelled comparison)")
    g.add_argument("--timeout", type=float)
    g.set_defaults(fn=cmd_generate)
    vw = sub.add_parser("view", help="open a job's outputs in Blender (textured + untextured rows, labelled)")
    vw.add_argument("job_id")
    vw.add_argument("--dir", help="download/open directory (default out/<job_id>)")
    vw.add_argument("--blender", help="path to blender executable (default: STUDIO_BLENDER, PATH, newest install)")
    vw.set_defaults(fn=cmd_view)
    im = sub.add_parser("images", help="step 1: generate image variations of an idea (no 3D)")
    im.add_argument("prompt")
    im.add_argument("--style", default="mobile_factory")
    im.add_argument("--variations", type=int, default=4)
    im.add_argument("--seed", type=int)
    im.add_argument("--height-m", dest="height_m", type=float)
    im.add_argument("--materials")
    im.add_argument("--palette")
    im.add_argument("--negative", dest="negative_extra")
    im.add_argument("--title")
    im.add_argument("--wait", action="store_true")
    im.add_argument("--timeout", type=float)
    im.set_defaults(fn=cmd_images)
    mk = sub.add_parser("make3d", help="step 2: turn chosen variations into 3D assets (held for review unless --start)")
    mk.add_argument("image_job_id")
    mk.add_argument("candidates", nargs="+", help="e.g. cand_00.png cand_02.png")
    mk.add_argument("--start", action="store_true", help="queue immediately instead of holding for review")
    mk.add_argument("--quality", default="balanced", choices=["balanced", "quality"])
    mk.add_argument("--height-m", dest="height_m", type=float)
    mk.add_argument("--triangles", dest="target_triangles", type=int)
    mk.add_argument("--texture", dest="texture_size", type=int)
    mk.add_argument("--collision-triangles", dest="collision_triangles", type=int)
    mk.add_argument("--title")
    mk.add_argument("--no-lods", action="store_true")
    mk.add_argument("--no-collision", action="store_true")
    mk.add_argument("--no-fallback", action="store_true")
    mk.add_argument("--wait", action="store_true")
    mk.add_argument("--timeout", type=float)
    mk.set_defaults(fn=cmd_make3d)
    rl = sub.add_parser("release", help="start a held job")
    rl.add_argument("job_id")
    rl.set_defaults(fn=cmd_release)
    hd = sub.add_parser("hold", help="take a queued job out of the queue")
    hd.add_argument("job_id")
    hd.set_defaults(fn=cmd_hold)
    sub.add_parser("queue", help="show held/queued/running jobs and GPU state").set_defaults(fn=cmd_queue)
    lb = sub.add_parser("library", help="list image sessions and assets")
    lb.add_argument("--kind", choices=["image", "asset"])
    lb.add_argument("--q")
    lb.add_argument("--limit", type=int, default=30)
    lb.set_defaults(fn=cmd_library)
    s = sub.add_parser("status")
    s.add_argument("job_id")
    s.add_argument("--events", type=int, default=20)
    s.set_defaults(fn=cmd_status)
    w = sub.add_parser("wait")
    w.add_argument("job_id")
    w.add_argument("--timeout", type=float)
    w.set_defaults(fn=cmd_wait)
    d = sub.add_parser("download")
    d.add_argument("job_id")
    d.add_argument("dir")
    d.set_defaults(fn=cmd_download)
    c = sub.add_parser("cancel")
    c.add_argument("job_id")
    c.set_defaults(fn=cmd_cancel)
    rt = sub.add_parser("retry", help="requeue a failed/cancelled job, or reprocess a completed one from a stage (--from-stage). "
                                      "Running/queued/held jobs are rejected (HTTP 409): cancel first or wait.")
    rt.add_argument("job_id")
    rt.add_argument("--from-stage", dest="from_stage", choices=["reference", "pixal3d", "blender"])
    rt.add_argument("--triangles", dest="target_triangles", type=int)
    rt.add_argument("--texture", dest="texture_size", type=int)
    rt.add_argument("--collision-triangles", dest="collision_triangles", type=int)
    rt.add_argument("--height-m", dest="height_m", type=float)
    rt.add_argument("--lod-fractions", dest="lod_fractions")
    rt.add_argument("--wait", action="store_true")
    rt.add_argument("--timeout", type=float)
    rt.set_defaults(fn=cmd_retry)
    lg = sub.add_parser("logs")
    lg.add_argument("job_id")
    lg.add_argument("stage", choices=["reference", "pixal3d", "blender"])
    lg.add_argument("--tail", type=int, default=20000)
    lg.set_defaults(fn=cmd_logs)
    ls = sub.add_parser("list")
    ls.add_argument("--limit", type=int, default=20)
    ls.set_defaults(fn=cmd_list)
    sub.add_parser("capabilities").set_defaults(fn=cmd_capabilities)
    sub.add_parser("health").set_defaults(fn=cmd_health)
    dr = sub.add_parser("doctor")
    dr.add_argument("--deep", action="store_true")
    dr.set_defaults(fn=cmd_doctor)
    a = ap.parse_args(argv)
    return a.fn(a) or 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
