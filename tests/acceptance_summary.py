"""Build samples/acceptance_report.json from the manifests of real completed jobs in samples/<name>/manifest.json.
Records measured timings, VRAM (torch max_reserved + nvidia-smi peak), RSS, effective settings and validation.

The `host` string describes the machine the archived samples were produced on (the earlier container layout); it is
kept as measured rather than rewritten when the samples are regenerated on a different setup."""
import json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
out = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "host": "RTX 5090 32 GB, 46 GiB RAM for the workers", "jobs": []}
for d in sorted((ROOT / "samples").iterdir()):
    mp = d / "manifest.json"
    if not mp.exists() or d.name.startswith("_"):
        continue
    m = json.loads(mp.read_text(encoding="utf-8"))
    st, res = m["stages"], m["resources"]
    ref, px, bl = st.get("reference") or {}, st.get("pixal3d") or {}, st.get("blender") or {}
    a = bl.get("stats", {}).get("asset", {})
    out["jobs"].append({
        "name": d.name, "job_id": m["job_id"], "prompt": m["request"]["prompt"], "quality": m["request"]["quality"],
        "stage_timings_s": m["timings_s"],
        "reference": {"candidates": len((ref.get("selection") or {}).get("ranking", [])), "per_candidate_s": ref.get("stats", {}).get("timings_s", {}).get("per_candidate_s"),
                      "torch_max_reserved_mib": ref.get("stats", {}).get("vram", {}).get("generate_max_reserved_mib"),
                      "nvidia_smi_peak_mib": res.get("reference", {}).get("peak_gpu_used_mib"), "peak_rss_mb": res.get("reference", {}).get("peak_rss_mb"),
                      "settings": ref.get("effective")},
        "pixal3d": {"resolution": (px.get("effective") or {}).get("resolution"), "grid_resolution": (px.get("effective") or {}).get("grid_resolution"),
                    "timings_s": px.get("stats", {}).get("timings_s"), "torch_max_reserved_mib": {k: v.get("max_reserved_mib") for k, v in px.get("stats", {}).get("vram", {}).items()},
                    "nvidia_smi_peak_mib": res.get("pixal3d", {}).get("peak_gpu_used_mib"), "peak_rss_mb": res.get("pixal3d", {}).get("peak_rss_mb"),
                    "mesh_stats": px.get("mesh_stats")},
        "blender": {"timings_s": bl.get("stats", {}).get("timings_s"), "method": a.get("method"), "triangles": a.get("triangles"), "reduction": a.get("reduction"),
                    "proxy": a.get("proxy"), "maps": a.get("maps"), "lods": [(l["file"], l["triangles"]) for l in (bl.get("outputs") or {}).get("lods", [])],
                    "collision_triangles": bl.get("stats", {}).get("collision", {}).get("triangles"), "peak_rss_mb": res.get("blender", {}).get("peak_rss_mb")},
        "validation": {k: v["ok"] for k, v in (st.get("validation") or {}).items()},
        "warnings": m["warnings"], "fallbacks_applied": m["fallbacks_applied"],
    })
p = ROOT / "samples" / "acceptance_report.json"
p.write_text(json.dumps(out, indent=2), encoding="utf-8")
print("wrote", p, "jobs:", [j["name"] for j in out["jobs"]])
