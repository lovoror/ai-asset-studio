#!/usr/bin/env python3
"""Real GPU acceptance run (NOT a mocked test): submits the acceptance jobs to the running service, waits for them,
validates the results and writes samples/acceptance_report.json with measured timings / VRAM / RAM.

  python tests/acceptance_real.py                 # crate (balanced), pump (quality/1536, no fallback), mug (balanced)
  python tests/acceptance_real.py --only crate    # one job
  python tests/acceptance_real.py --cancel-test   # submit a job, cancel it mid-stage, expect status 'cancelled'
  python tests/acceptance_real.py --restart-test  # submit a job, restart the worker container mid-stage, expect completion with stage reuse
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cli"))
import assetctl  # noqa: E402

JOBS = {
    "crate": json.loads((ROOT / "examples" / "cargo_crate.json").read_text()),
    "pump": json.loads((ROOT / "examples" / "pump_station.json").read_text()),
    "mug": json.loads((ROOT / "examples" / "mug.json").read_text()),
}


def submit(body):
    return assetctl._req("POST", "/v1/jobs", body)["job_id"]


def wait(job_id, timeout=7200):
    t0 = time.time()
    last = None
    while True:
        j = assetctl._req("GET", f"/v1/jobs/{job_id}?events=1")
        line = f"{j['status']} {j['stage']} {j['progress']:.2f} " + (j["events"][-1]["message"][:90] if j.get("events") else "")
        if line != last:
            print(f"  [{time.strftime('%H:%M:%S')}] {job_id} {line}", flush=True)
            last = line
        if j["status"] in ("completed", "failed", "cancelled"):
            return j
        if time.time() - t0 > timeout:
            raise SystemExit("timeout")
        time.sleep(5)


def summarize(job_id, name, dest: Path):
    man = json.loads(assetctl._req("GET", f"/v1/jobs/{job_id}/artifacts/manifest.json", raw=True))
    assetctl._download(job_id, dest)
    st = man["stages"]
    res = man["resources"]
    v = st["validation"]
    summary = {
        "name": name, "job_id": job_id, "sample_dir": str(dest),
        "timings_s": man["timings_s"],
        "reference": {"candidates": len(st["reference"].get("selection", {}).get("ranking", [])) if st.get("reference") and st["reference"].get("selection") else None,
                      "per_candidate_s": (st["reference"] or {}).get("stats", {}).get("timings_s", {}).get("per_candidate_s"),
                      "peak_gpu_used_mib_nvidia_smi": res["reference"].get("peak_gpu_used_mib"),
                      "torch_max_reserved_mib": (st["reference"] or {}).get("stats", {}).get("vram", {}).get("generate_max_reserved_mib"),
                      "peak_rss_mb": res["reference"].get("peak_rss_mb")},
        "pixal3d": {"effective": (st["pixal3d"] or {}).get("effective"), "timings_s": (st["pixal3d"] or {}).get("stats", {}).get("timings_s"),
                    "vram_torch": (st["pixal3d"] or {}).get("stats", {}).get("vram"),
                    "peak_gpu_used_mib_nvidia_smi": res["pixal3d"].get("peak_gpu_used_mib"), "peak_rss_mb": res["pixal3d"].get("peak_rss_mb"),
                    "mesh_stats": (st["pixal3d"] or {}).get("mesh_stats")},
        "blender": {"timings_s": (st["blender"] or {}).get("stats", {}).get("timings_s"), "asset": (st["blender"] or {}).get("stats", {}).get("asset"),
                    "peak_rss_mb": res["blender"].get("peak_rss_mb")},
        "validation": {k: {"ok": r["ok"], "triangles": r.get("triangles"), "vertices": r.get("vertices"), "images": [(i["mimeType"], i.get("size")) for i in r.get("images", [])],
                           "height_y": r.get("height_y"), "origin": r.get("origin"), "errors": r["errors"]} for k, r in v.items()},
        "warnings": man["warnings"], "fallbacks_applied": man["fallbacks_applied"],
    }
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append")
    ap.add_argument("--cancel-test", action="store_true")
    ap.add_argument("--restart-test", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "samples"))
    a = ap.parse_args()
    out = Path(a.out)
    report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "jobs": [], "tests": {}}
    for name in (a.only or list(JOBS)):
        print(f"=== {name}", flush=True)
        job = submit(JOBS[name])
        j = wait(job)
        if j["status"] != "completed":
            report["jobs"].append({"name": name, "job_id": job, "status": j["status"], "error": j.get("error"), "warnings": j.get("warnings")})
            print(f"  {name} {j['status']}: {j.get('error')}")
            continue
        s = summarize(job, name, out / name)
        s["status"] = "completed"
        report["jobs"].append(s)
        print(f"  {name} OK total {s['timings_s'].get('total')}s validation ok={all(v['ok'] for v in s['validation'].values())}")
    if a.cancel_test:
        print("=== cancel test", flush=True)
        job = submit({**JOBS["crate"], "seed": 99})
        for _ in range(120):  # wait until the reference stage is actually running a GPU process
            j = assetctl._req("GET", f"/v1/jobs/{job}")
            if j["status"] == "running" and j["stage"] == "reference":
                break
            time.sleep(2)
        time.sleep(20)
        t0 = time.time()
        r = assetctl._req("POST", f"/v1/jobs/{job}/cancel")
        j = wait(job, timeout=300)
        gpu = assetctl._req("GET", "/capabilities")["gpu"]
        report["tests"]["cancel"] = {"job_id": job, "cancel_response": r, "final_status": j["status"], "seconds_to_stop": round(time.time() - t0, 1),
                                     "gpu_used_mib_after": gpu["gpus"][0]["used_mib"] if gpu.get("available") else None}
        print("  cancel:", report["tests"]["cancel"])
    if a.restart_test:
        print("=== restart recovery test", flush=True)
        job = submit({**JOBS["crate"], "seed": 5})
        for _ in range(200):
            j = assetctl._req("GET", f"/v1/jobs/{job}")
            if j["status"] == "running" and j["stage"] == "pixal3d":
                break
            time.sleep(3)
        time.sleep(15)
        subprocess.run(["docker", "compose", "restart", "worker"], cwd=ROOT, check=True)
        j = wait(job)
        ev = [e["message"] for e in assetctl._req("GET", f"/v1/jobs/{job}?events=200")["events"]]
        report["tests"]["restart_recovery"] = {"job_id": job, "final_status": j["status"], "attempt": j["attempt"],
                                               "requeued": any("requeued" in m for m in ev), "reference_reused": any("reference stage already complete" in m for m in ev)}
        print("  restart:", report["tests"]["restart_recovery"])
    report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    out.mkdir(parents=True, exist_ok=True)
    p = out / "acceptance_report.json"
    existing = json.loads(p.read_text()) if p.exists() else {"runs": []}
    existing.setdefault("runs", []).append(report)
    p.write_text(json.dumps(existing, indent=2))
    print("report ->", p)


if __name__ == "__main__":
    main()
