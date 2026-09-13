"""Thin MCP wrapper (stdio) over the asset-studio HTTP API. Same actions as the CLI: generate/status/wait/download/cancel.

Run:  pip install "mcp>=1.2"   then   python mcp/server.py
Claude Code:  claude mcp add asset-studio -- python C:/Users/Zorro/asset-studio/mcp/server.py
Env: STUDIO_URL (default http://127.0.0.1:8090), STUDIO_API_TOKEN (optional bearer token).
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))
import assetctl  # noqa: E402  (stdlib HTTP helpers)

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("asset-studio", instructions="Local text-to-3D asset generation (Qwen-Image-2512 -> Pixal3D -> Blender). "
              "Submit a prompt with generate, poll with status or wait, then download artifacts.")


@mcp.tool()
def capabilities() -> dict:
    """Service capabilities: styles, quality presets, limits, GPU, cached models."""
    return assetctl._req("GET", "/capabilities")


@mcp.tool()
def generate(prompt: str, style: str = "mobile_factory", quality: str = "balanced", seed: int | None = None,
             height_m: float | None = None, target_triangles: int | None = None, texture_size: int | None = None,
             generate_lods: bool = True, generate_collision: bool = True, allow_quality_fallback: bool = True,
             materials: str | None = None, palette: list[str] | None = None, reference_image_path: str | None = None,
             extra: dict | None = None) -> dict:
    """Submit a text-to-3D asset job. Returns the job id immediately; poll with status/wait."""
    body = {"prompt": prompt, "style": style, "quality": quality, "generate_lods": generate_lods,
            "generate_collision": generate_collision, "allow_quality_fallback": allow_quality_fallback}
    for k, v in (("seed", seed), ("height_m", height_m), ("target_triangles", target_triangles), ("texture_size", texture_size),
                 ("materials", materials), ("palette", palette)):
        if v is not None:
            body[k] = v
    if reference_image_path:
        body["reference_image_b64"] = base64.b64encode(Path(reference_image_path).read_bytes()).decode()
    if extra:
        body.update(extra)
    return assetctl._req("POST", "/v1/jobs", body)


@mcp.tool()
def status(job_id: str, events: int = 10) -> dict:
    """Job status: stage, progress, warnings, effective settings, timings, error, recent events."""
    return assetctl._req("GET", f"/v1/jobs/{job_id}?events={events}")


@mcp.tool()
def wait(job_id: str, timeout_s: float = 3600, poll_s: float = 5) -> dict:
    """Block until the job finishes (completed/failed/cancelled) or timeout; returns the final status."""
    t0 = time.time()
    while True:
        j = assetctl._req("GET", f"/v1/jobs/{job_id}?events=3")
        if j["status"] in ("completed", "failed", "cancelled") or time.time() - t0 > timeout_s:
            return j
        time.sleep(poll_s)


@mcp.tool()
def artifacts(job_id: str) -> dict:
    """List downloadable artifacts (master.glb, optimized asset, LODs, collision, previews, manifest.json)."""
    return assetctl._req("GET", f"/v1/jobs/{job_id}/artifacts")


@mcp.tool()
def download(job_id: str, dest_dir: str) -> dict:
    """Download all artifacts of a job into dest_dir. Returns the local paths."""
    arts = assetctl._req("GET", f"/v1/jobs/{job_id}/artifacts")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for a in arts["artifacts"]:
        p = dest / a["name"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(assetctl._req("GET", a["url"], raw=True, timeout=600))
        out.append(str(p))
    return {"job_id": job_id, "files": out}


@mcp.tool()
def manifest(job_id: str) -> dict:
    """The machine-readable manifest of a completed job (settings, statistics, warnings, timings, artifacts)."""
    return json.loads(assetctl._req("GET", f"/v1/jobs/{job_id}/artifacts/manifest.json", raw=True))


@mcp.tool()
def cancel(job_id: str) -> dict:
    """Cancel a queued or running job."""
    return assetctl._req("POST", f"/v1/jobs/{job_id}/cancel")


@mcp.tool()
def retry(job_id: str, from_stage: str | None = None, optimize: dict | None = None) -> dict:
    """Requeue a failed/cancelled job (completed stages reused), or reprocess a finished job from a stage
    (from_stage='blender' with optimize={'target_triangles': 20000, ...} re-optimises the same master).
    Jobs that are running, queued or held are rejected with HTTP 409 and left untouched: cancel them first or wait."""
    body = {}
    if from_stage:
        body["from_stage"] = from_stage
    if optimize:
        body["optimize"] = optimize
    return assetctl._req("POST", f"/v1/jobs/{job_id}/retry", body or None)


@mcp.tool()
def open_in_blender(job_id: str, dest_dir: str | None = None) -> dict:
    """Download (if needed) and open a job's outputs in the host Blender as a labelled comparison
    (textured row + untextured row: master, optimized, LODs, collision)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    from open_in_blender import open_sample

    dest = Path(dest_dir) if dest_dir else Path("out") / job_id
    if not (dest / "manifest.json").exists():
        download(job_id, str(dest))
    return open_sample(dest)


@mcp.tool()
def generate_variations(prompt: str, style: str = "mobile_factory", variations: int = 4, seed: int | None = None,
                        height_m: float | None = None, materials: str | None = None, palette: list[str] | None = None,
                        title: str | None = None) -> dict:
    """Step 1 of the two-step flow: generate image variations of one idea (no 3D). Poll with status/wait, then inspect
    candidates via status(job_id)['candidates'] and call make_asset for the ones you want."""
    body = {"prompt": prompt, "style": style, "variations": variations}
    for k, v in (("seed", seed), ("height_m", height_m), ("materials", materials), ("palette", palette), ("title", title)):
        if v is not None:
            body[k] = v
    return assetctl._req("POST", "/v1/image-jobs", body)


@mcp.tool()
def make_asset(image_job_id: str, candidate: str, quality: str = "balanced", start: bool = True, height_m: float | None = None,
               target_triangles: int | None = None, texture_size: int | None = None, generate_lods: bool = True,
               generate_collision: bool = True, allow_quality_fallback: bool = True, title: str | None = None) -> dict:
    """Step 2: turn one variation (e.g. 'cand_01.png') of an image job into a 3D asset. start=False holds it for review."""
    body = {"image_job_id": image_job_id, "candidate": candidate, "quality": quality, "hold": not start,
            "generate_lods": generate_lods, "generate_collision": generate_collision, "allow_quality_fallback": allow_quality_fallback}
    for k, v in (("height_m", height_m), ("target_triangles", target_triangles), ("texture_size", texture_size), ("title", title)):
        if v is not None:
            body[k] = v
    return assetctl._req("POST", "/v1/asset-jobs", body)


@mcp.tool()
def release(job_id: str) -> dict:
    """Start a job that is held for review."""
    return assetctl._req("POST", f"/v1/jobs/{job_id}/release")


@mcp.tool()
def queue() -> dict:
    """Held / queued / running jobs plus GPU state and the auto_process setting."""
    return assetctl._req("GET", "/v1/queue")


@mcp.tool()
def library(kind: str | None = None, q: str | None = None, limit: int = 30) -> dict:
    """Browse the library of image sessions and 3D assets (thumbs, status, lineage)."""
    qs = f"?limit={limit}" + (f"&kind={kind}" if kind else "") + (f"&q={q}" if q else "")
    return assetctl._req("GET", "/v1/library" + qs)


if __name__ == "__main__":
    mcp.run()
