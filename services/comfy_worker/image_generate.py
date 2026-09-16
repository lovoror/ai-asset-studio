"""Reference-image stage on a remote ComfyUI server.

Drop-in replacement for `python -m image_worker.generate` when the job's backend is comfyui: same
request.json in, same candidates/ + selection.json + output.json out. Scoring and selection are imported
from image_worker rather than reimplemented, so a remote candidate is judged by exactly the same technical
checks as a local one and `finish()` writes a byte-identical output shape.

One ComfyUI prompt per candidate (not batch_size), because variations need different seeds and a per-seed
score. That matches the local path, which is also serial.

Nothing here needs a GPU or torch: the work happens on the other machine and this process only does HTTP.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import traceback
from pathlib import Path

from comfy_worker.comfy_client import (ComfyError, Interrupted, derive_sampler_graph, download, find_workflow_file,
                                       iter_outputs, load_workflow, log, phase, probe, retract, run_workflow,
                                       set_node_input)
# Scoring and the output.json/selection.json shape come from the local worker rather than being reimplemented,
# so a remote candidate is judged identically and the orchestrator sees the same contract either way.
# All three are torch-free at import time (image_worker.generate imports torch inside its functions).
from image_worker.generate import analyze_candidate, external_eval, finish

DEFAULT_TIMEOUT_S = float(os.environ.get("STUDIO_COMFY_TIMEOUT_S", "1800"))


def describe(req: dict, backend: dict, graph: dict) -> dict:
    """The manifest's record of what actually produced these images (mirrors the local backends' describe())."""
    return {"backend": "comfyui", "server": backend.get("url"), "workflow": backend.get("workflow"),
            "model_id": req.get("model_id"), "family": "comfyui",
            "nodes": {k: v for k, v in graph.items() if k != "n_samplers"}}


def _patch(wf: dict, graph: dict, req: dict, seed: int, warnings: list) -> None:
    """Write this candidate's prompt / seed / size / sampler settings into a copy of the workflow."""
    sampler = graph["sampler"]
    steps = int(req.get("steps", 8))
    cfg = float(req.get("cfg", req.get("guidance_scale", req.get("true_cfg_scale", 1.0))) or 1.0)
    w, h = int(req["width"]), int(req["height"])

    if graph.get("positive"):
        if not set_node_input(wf, graph["positive"], "text", req["prompt"]):
            warnings.append(f"could not write the prompt into node {graph['positive']}")
    else:
        warnings.append("the workflow's sampler has no positive conditioning link; the prompt was not applied")
    if graph.get("negative"):
        set_node_input(wf, graph["negative"], "text", req.get("negative_prompt") or "")

    # KSampler uses `seed`; KSamplerAdvanced uses `noise_seed`. Patch whichever the node actually has.
    if not (set_node_input(wf, sampler, "seed", seed) or set_node_input(wf, sampler, "noise_seed", seed)):
        warnings.append(f"sampler node {sampler} exposes neither seed nor noise_seed")
    for key, value in (("steps", steps), ("cfg", cfg)):
        if not set_node_input(wf, sampler, key, value):
            warnings.append(f"sampler node {sampler} has no {key!r} input; left at the workflow's value")
    for key in ("sampler_name", "scheduler"):
        if req.get(key) is not None:
            set_node_input(wf, sampler, key, req[key])

    latent = graph.get("latent")
    if latent:
        for key, value in (("width", w), ("height", h), ("batch_size", 1)):
            set_node_input(wf, latent, key, value)
    else:
        warnings.append("the sampler's latent_image is not a node link; the requested "
                        f"{w}x{h} could not be applied")


def _save_candidate(entry: dict, server: str, dest: Path, warnings: list) -> None:
    """Download this prompt's image. Prefers SaveImage over PreviewImage when a workflow has both."""
    arts = list(iter_outputs(entry.get("outputs", {}), exts=(".png", ".jpg", ".jpeg", ".webp")))
    if not arts:
        raise ComfyError(f"the prompt succeeded but produced no image (outputs: "
                         f"{json.dumps(entry.get('outputs', {}))[:400]})")
    if len(arts) > 1:
        # /history entries carry the submitted graph at prompt[2]: [number, client_id, workflow, extra, outputs]
        hist = entry.get("prompt")
        submitted = hist[2] if isinstance(hist, list) and len(hist) > 2 and isinstance(hist[2], dict) else {}
        saved = [a for a in arts if (submitted.get(a[0]) or {}).get("class_type") == "SaveImage"]
        if saved:
            arts = saved
        if len(arts) > 1:
            warnings.append(f"the workflow produced {len(arts)} images; kept {arts[0][1]} and ignored the rest "
                            f"(point Settings -> Generation backends at a workflow with one SaveImage)")
    nid, name, sub, ftype = arts[0]
    download(server, name, sub, ftype, dest)


def generate_candidates(req: dict, out_dir: Path, backend: dict, warnings: list) -> tuple[list, dict, dict]:
    server = backend["url"]
    template = load_workflow(find_workflow_file(backend["workflow"]))
    graph = derive_sampler_graph(template)
    log(f"[comfy] image workflow {backend['workflow']}: sampler {graph['sampler']}, prompt node "
        f"{graph.get('positive')}, negative {graph.get('negative')}, latent {graph.get('latent')} "
        f"({graph.get('latent_kind')})")
    timeout_s = float(req.get("timeout_s") or DEFAULT_TIMEOUT_S)
    cdir = out_dir / "candidates"
    cdir.mkdir(parents=True, exist_ok=True)

    remote = {}
    try:
        remote = probe(server)
        log(f"[comfy] server {server}: ComfyUI {remote.get('comfyui_version')}, gpu {remote.get('gpu')}, "
            f"vram {remote.get('vram_total_mib')} MiB")
    except Exception as e:  # noqa: BLE001
        warnings.append(f"could not read {server}/system_stats ({e}); continuing anyway")

    cands = []
    phase("generate")
    for i, seed in enumerate(req["seeds"]):
        t0 = time.time()
        wf = copy.deepcopy(template)
        _patch(wf, graph, req, int(seed), warnings)
        entry = run_workflow(server, wf, timeout_s, client_id=f"asset-studio-{req.get('model_id', 'comfyui')}")
        f = cdir / f"cand_{i:02d}.png"
        _save_candidate(entry, server, f, warnings)
        dt = round(time.time() - t0, 2)
        metrics = analyze_candidate(f)
        ext = external_eval(req.get("evaluator_url", ""), f)
        if ext and "score" in ext:
            metrics["external"] = ext
            metrics["score"] = 0.5 * metrics["score"] + 0.5 * float(ext["score"])
        elif ext:
            metrics["external"] = ext
        hist = entry.get("prompt")
        cands.append({"file": f"candidates/{f.name}", "seed": int(seed), "time_s": dt, "metrics": metrics,
                      # ComfyUI's own prompt number, so a failed candidate can be looked up in its UI/history
                      "comfy_prompt_number": hist[0] if isinstance(hist, list) and hist else None})
        log(f"[candidate {i}] seed={seed} {dt}s score={metrics.get('score'):.2f} "
            f"{metrics.get('reasons') or metrics.get('reject') or ''}")
    return cands, graph, remote


def run(req: dict) -> None:
    out_dir = Path(req["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    backend = req.get("backend") or {}
    if backend.get("kind") != "comfyui" or not backend.get("url"):
        raise ComfyError(f"request.json has no usable comfyui image backend: {json.dumps(backend)[:200]}. "
                         f"Set Settings -> Generation backends, or pick a local model.")
    t_all = time.time()
    warnings: list[str] = []
    timings: dict = {}
    phase("load")
    cands, graph, remote = generate_candidates(req, out_dir, backend, warnings)
    timings["generate_s"] = round(sum(c["time_s"] for c in cands), 2)
    timings["per_candidate_s"] = [c["time_s"] for c in cands]
    # No local VRAM to report: the card is on another machine. Left empty on purpose so the portal says
    # "remote backend" instead of showing a number that looks like this host's usage. The remote card's own
    # figures go under stats.remote, where the manifest reader can see them without misreading them as local.
    vram: dict = {}
    extra = describe(req, backend, graph)
    extra["remote_gpu"] = remote
    finish(req, out_dir, cands, timings, vram, warnings, t_all, extra)


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
        retract(None, None)
        sys.exit(143)
    except ComfyError as e:
        print(f"ERROR: {e}", flush=True)
        sys.exit(1)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
