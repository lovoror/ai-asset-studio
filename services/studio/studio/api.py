"""FastAPI service: job submission, status, artifacts, cancel, health, capabilities."""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from typing import Literal, Optional

from . import config
from .db import JobStore
from .models import JobCreated, JobRequest
from .presets import load_presets, resolve_settings, style_ids
from .runner_client import Runner

app = FastAPI(title="asset-studio", version=config.VERSION,
              description="Local text-to-3D asset generation service (Qwen-Image-2512 -> Pixal3D -> Blender).")
_store: JobStore | None = None


def store() -> JobStore:
    global _store
    if _store is None:
        _store = JobStore()
    return _store


async def auth(request: Request):
    if not config.API_TOKEN:
        return
    hdr = request.headers.get("authorization", "")
    tok = hdr[7:] if hdr.lower().startswith("bearer ") else ""
    if not secrets.compare_digest(tok, config.API_TOKEN):
        raise HTTPException(401, "missing or invalid bearer token")


@app.get("/health")
def health():
    runners = {k: Runner(k).health() for k in ("image", "pixal3d", "blender")}
    ok = all(r.get("ok") for r in runners.values())
    try:
        store().con.execute("SELECT 1")
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    running = store().list_jobs(limit=1, status="running")
    queued = store().con.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
    return {"ok": ok and db_ok, "version": config.VERSION, "db": db_ok, "runners": runners,
            "queue": {"running": running[0]["id"] if running else None, "queued": queued}, "time": time.time()}


@app.get("/capabilities")
def capabilities():
    p = load_presets()
    gpu = Runner("pixal3d").gpu()
    models_root = Path("/models/hf/hub")
    mv_ready = False
    if models_root.exists():
        for snap in (models_root / "models--TencentARC--Pixal3D" / "snapshots").glob("*/ckpts"):
            mv_ready = mv_ready or any(snap.glob("*_mv.safetensors"))
    deps = {}
    dm = config.MANIFESTS_DIR / "dependency-manifest.json"
    if dm.exists():
        deps = json.loads(dm.read_text(encoding="utf-8"))
    return {
        "version": config.VERSION,
        "pipeline": ["prompt->template", "Qwen-Image-2512 reference (diffusers, local)", "Pixal3D preprocessing + MoGe-2 camera",
                     "Pixal3D geometry+PBR (TRELLIS.2 backbone)", "master GLB (PNG textures)", "Blender optimisation/LODs/collision/previews",
                     "validation + manifest"],
        "styles": {k: {"label": v.get("label"), "description": v.get("description"), "optimize_defaults": v.get("optimize_defaults")}
                   for k, v in p["styles"].items()},
        "quality": {k: {"reference": v["reference"], "pixal3d": v["pixal3d"], "master": v["master"], "fallback": v.get("fallback")}
                    for k, v in p["quality"].items()},
        "request_schema": JobRequest.model_json_schema(),
        "limits": {"target_triangles": [200, 2_000_000], "texture_size": [256, 512, 1024, 2048, 4096], "reference_candidates": [1, 6],
                   "lod_fractions_max": 4},
        "features": {"text_to_asset": True, "reference_image_input": True, "multiview_input": True,
                     "multiview_checkpoints_cached": mv_ready, "automatic_novel_view_generation": False,
                     "vision_evaluator": bool(os.environ.get("STUDIO_VISION_EVALUATOR_URL"))},
        "gpu": gpu, "gpu_uuid": config.GPU_UUID,
        "dependencies": deps.get("summary", deps),
        "auth_required": bool(config.API_TOKEN),
    }


@app.post("/v1/jobs", response_model=JobCreated, dependencies=[Depends(auth)])
def create_job(req: JobRequest):
    data = req.model_dump()
    try:
        settings = resolve_settings(data)
    except ValueError as e:
        raise HTTPException(422, str(e))
    job_id = store().create(data, settings)
    return JobCreated(job_id=job_id, status="queued", status_url=f"/v1/jobs/{job_id}", artifacts_url=f"/v1/jobs/{job_id}/artifacts")


@app.get("/v1/jobs", dependencies=[Depends(auth)])
def list_jobs(limit: int = 50, status: str | None = None):
    limit = max(1, min(limit, 500))
    return [_public(j) for j in store().list_jobs(limit=limit, status=status)]


def _public(j: dict, events=None) -> dict:
    out = {k: v for k, v in j.items() if k not in ("request",)}
    req = dict(j["request"])
    req.pop("reference_image_b64", None)
    if req.get("multiview"):
        req["multiview"] = {"num_views": len(req["multiview"].get("frames", [])), "camera_source": req["multiview"].get("camera_source")}
    out["request"] = req
    out["settings"] = {k: v for k, v in (j.get("settings") or {}).items() if not k.startswith("_")}
    out["fallbacks_applied"] = (j.get("settings") or {}).get("_fallbacks_applied", [])
    if events is not None:
        out["events"] = events
    return out


def _get_job(job_id: str) -> dict:
    if not _safe_id(job_id):
        raise HTTPException(400, "invalid job id")
    j = store().get(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return j


def _safe_id(job_id: str) -> bool:
    return bool(job_id) and len(job_id) < 64 and all(c.isalnum() or c == "-" for c in job_id)


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(auth)])
def get_job(job_id: str, events: int = 20):
    j = _get_job(job_id)
    ev = store().events(job_id, limit=max(0, min(events, 500))) if events else None
    out = _public(j, ev)
    out["artifacts_url"] = f"/v1/jobs/{job_id}/artifacts"
    mp = config.JOBS_DIR / job_id / "manifest.json"
    out["manifest_url"] = f"/v1/jobs/{job_id}/artifacts/manifest.json" if mp.exists() else None
    return out


@app.get("/v1/jobs/{job_id}/artifacts", dependencies=[Depends(auth)])
def list_artifacts(job_id: str):
    j = _get_job(job_id)
    art = config.JOBS_DIR / job_id / "artifacts"
    items = []
    if art.exists():
        for p in sorted(art.rglob("*")):
            if p.is_file():
                rel = p.relative_to(art).as_posix()
                items.append({"name": rel, "bytes": p.stat().st_size, "url": f"/v1/jobs/{job_id}/artifacts/{rel}"})
    return {"job_id": job_id, "status": j["status"], "artifacts": items}


@app.get("/v1/jobs/{job_id}/artifacts/{name:path}", dependencies=[Depends(auth)])
def get_artifact(job_id: str, name: str):
    _get_job(job_id)
    art = (config.JOBS_DIR / job_id / "artifacts").resolve()
    target = (art / name).resolve()
    if art not in target.parents or not target.is_file():
        raise HTTPException(404, "artifact not found")
    media = "application/octet-stream"
    if target.suffix == ".glb":
        media = "model/gltf-binary"
    elif target.suffix == ".png":
        media = "image/png"
    elif target.suffix == ".json":
        media = "application/json"
    return FileResponse(str(target), media_type=media, filename=target.name)


@app.get("/v1/jobs/{job_id}/logs/{stage}", dependencies=[Depends(auth)])
def get_log(job_id: str, stage: str, tail: int = 20000):
    _get_job(job_id)
    if stage not in ("reference", "pixal3d", "blender"):
        raise HTTPException(404, "unknown stage")
    p = config.JOBS_DIR / job_id / "stages" / stage / "log.txt"
    if not p.exists():
        return PlainTextResponse("")
    data = p.read_bytes()[-max(0, min(tail, 5_000_000)):]
    return PlainTextResponse(data.decode("utf-8", "replace"))


@app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(auth)])
def cancel_job(job_id: str):
    _get_job(job_id)
    r = store().request_cancel(job_id)
    return {"job_id": job_id, "result": r}


class RetryRequest(BaseModel):
    from_stage: Optional[Literal["reference", "pixal3d", "blender"]] = Field(
        None, description="Re-run from this stage (its outputs and later ones are discarded); required to reprocess a completed job")
    optimize: Optional[dict] = Field(None, description="Override optimisation settings for the re-run, e.g. {target_triangles, texture_size, lod_fractions, generate_lods, generate_collision, collision_triangles}")


@app.post("/v1/jobs/{job_id}/retry", dependencies=[Depends(auth)])
def retry_job(job_id: str, body: RetryRequest | None = None):
    """Requeue a failed or cancelled job (stages that completed earlier are reused). With `from_stage` a completed job is
    reprocessed from that stage, e.g. `{"from_stage": "blender", "optimize": {"target_triangles": 20000}}` re-optimises
    the same master with a new budget without regenerating the image or the 3D model."""
    j = _get_job(job_id)
    body = body or RetryRequest()
    settings = None
    if body.from_stage:
        order = ["reference", "pixal3d", "blender"]
        for st in order[order.index(body.from_stage):]:
            rp = config.JOBS_DIR / job_id / "stages" / st / "result.json"
            if rp.exists():
                rp.unlink()
    if body.optimize:
        allowed = {"target_triangles", "texture_size", "lod_fractions", "generate_lods", "generate_collision", "collision_triangles",
                   "height_m", "width_m", "depth_m", "render_previews", "preview_size"}
        bad = set(body.optimize) - allowed
        if bad:
            raise HTTPException(422, f"unknown optimize keys: {sorted(bad)}")
        settings = dict(j["settings"])
        settings["optimize"] = {**settings["optimize"], **body.optimize}
        if not body.from_stage:
            body.from_stage = "blender"
            rp = config.JOBS_DIR / job_id / "stages" / "blender" / "result.json"
            if rp.exists():
                rp.unlink()
    r = store().retry(job_id, allow_completed=bool(body.from_stage), settings=settings)
    return {"job_id": job_id, "result": r, "from_stage": body.from_stage}


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


def main():
    if config.BIND_HOST not in ("127.0.0.1", "localhost", "::1") and not config.API_TOKEN:
        print("refusing to bind to a non-loopback address without STUDIO_API_TOKEN", file=sys.stderr)
        sys.exit(2)
    # inside compose the container listens on all interfaces; the *published* port is bound to localhost by compose.
    host = "0.0.0.0" if os.environ.get("STUDIO_IN_CONTAINER") else config.BIND_HOST
    uvicorn.run(app, host=host, port=config.PORT, log_level="info")


if __name__ == "__main__":
    main()
