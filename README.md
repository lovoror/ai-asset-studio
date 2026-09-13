# asset-studio

Fully local, agent-callable **text → 3D game asset** service for one RTX 5090, built on Docker Desktop.

```
prompt ─► structured asset template ─► Qwen-Image-2512 reference image ─► Pixal3D (TRELLIS.2 backbone)
       ─► master GLB (PBR, PNG textures) ─► headless Blender (scale/origin/cleanup/decimate/bake/LODs/collision/previews)
       ─► validation ─► manifest.json + artifacts
```

An agent POSTs a description and gets back: an untouched high-quality master GLB, an optimized game GLB at a
triangle/texture budget, optional LODs and a convex collision mesh, neutral-lit preview renders, and a
machine-readable manifest (settings requested vs. effective, statistics, warnings, timings, VRAM/RAM, artifact list).

Nothing leaves the machine: every model (≈110 GB) is prefetched once into a Docker volume and the workers run with
`HF_HUB_OFFLINE=1`.

## Layout

| Path | What |
|---|---|
| `compose.yaml` | 5 services: `api`, `worker` (orchestrator, one GPU slot), `image-worker` (Qwen), `pixal3d-worker`, `blender` |
| `docker/*.Dockerfile` | isolated environments (see *Environments*) |
| `services/studio/studio/` | FastAPI app, SQLite job store, pipeline, prompt builder, presets, GLB validator, worker |
| `services/runner/runner.py` | tiny stdlib stage runner: spawns one subprocess per stage so CUDA memory is freed on exit |
| `services/image_worker/generate.py` | Qwen-Image-2512 reference candidates + technical selection |
| `services/pixal3d_worker/` | Pixal3D adapter (single-image + multi-view), prefetch/verify |
| `services/blender/process_asset.py` | bpy 4.5 LTS post-processing |
| `presets/` | `styles.yaml` (incl. `mobile_factory`), `quality.yaml` (balanced/quality + OOM fallback ladders) |
| `cli/assetctl.py` | dependency-free CLI: generate / status / wait / download / cancel / retry / logs / doctor |
| `mcp/server.py` | thin MCP wrapper with the same actions |
| `scripts/` | `bootstrap.ps1`, `start.ps1`, `stop.ps1`, `prefetch.ps1`, `write_manifest.py`, `prefetch_hf.py` |
| `manifests/dependency-manifest.json` | pinned commits, model revisions, image ids, frozen packages |
| `tests/` | `test_api_mock.py` (mocked error paths, no GPU) and `acceptance_real.py` (real GPU runs) |
| `examples/` | request JSONs + curl example |
| `samples/` | outputs of real completed jobs (`crate/`, `pump/`, `mug/`) + `acceptance_report.json` |
| `docs/` | notes, measurements, limitations |

## Quick start

Prerequisites (this machine already has them): Docker Desktop with the NVIDIA runtime, the `trellis2:rtx5090`
base image (built from `C:\Users\Zorro\TRELLIS.2\Dockerfile`), `.env` with the GPU UUID (`.env.example`).

```powershell
scripts\bootstrap.ps1          # first time: build images, prefetch ~110 GB of weights, verify offline loading
scripts\start.ps1              # docker compose up -d, waits for /health   (scripts\stop.ps1 to stop)
python cli\assetctl.py doctor  # environment / GPU / runner checks   (--deep loads the models too)
```

CLI:

```powershell
python cli\assetctl.py generate "Stylized industrial water pump station with copper pipes" `
   --style mobile_factory --quality quality --seed 12345 --height-m 2.0 --triangles 12000 --texture 2048 `
   --wait --download out\pump
```

Open any job's results in your local Blender as a labelled comparison scene (front row textured: master, optimized,
LODs, collision; back row the same meshes untextured):

```powershell
python clissetctl.py view <job_id>            # downloads to out\<job_id> if needed, then launches Blender
python clissetctl.py generate "..." --open    # generate, wait, download and open in one go
```
Blender is found via `STUDIO_BLENDER`, `PATH`, or the newest install under `Program Files\Blender Foundation`.
The MCP server exposes the same action as `open_in_blender`.

HTTP (`examples/curl_example.sh`):

```bash
curl -X POST http://127.0.0.1:8090/v1/jobs -H 'Content-Type: application/json' -d @examples/pump_station.json
# -> {"job_id": "...", "status": "queued", "status_url": "/v1/jobs/<id>", "artifacts_url": "/v1/jobs/<id>/artifacts"}
curl http://127.0.0.1:8090/v1/jobs/<id>                      # status, stage, progress, warnings, settings, timings, events
curl http://127.0.0.1:8090/v1/jobs/<id>/artifacts            # list
curl -o asset.glb http://127.0.0.1:8090/v1/jobs/<id>/artifacts/<name>.glb
curl -X POST http://127.0.0.1:8090/v1/jobs/<id>/cancel   # or /retry to resume a failed/cancelled job from its completed stages
curl http://127.0.0.1:8090/health ; curl http://127.0.0.1:8090/capabilities
```

Request fields (all application-level; mapped to real backend options in `presets/quality.yaml`):
`prompt, style, quality (balanced|quality), seed, height_m/width_m/depth_m, target_triangles, texture_size,
generate_lods, lod_fractions, generate_collision, collision_triangles, allow_quality_fallback, materials, palette,
negative_extra, reference_candidates, reference_image_b64, multiview, render_previews, preview_size,
master_texture_size, master_triangles`. `GET /capabilities` returns the JSON schema.

The API binds to `127.0.0.1` only. To expose it on the network set `STUDIO_BIND` and a `STUDIO_API_TOKEN`
(bearer auth becomes mandatory; the server refuses to start non-loopback without a token).

## Environments (pinned in `manifests/dependency-manifest.json`)

| Image | Base | Key pins |
|---|---|---|
| `asset-studio/pixal3d-worker` | `trellis2:rtx5090` (nvidia/cuda 12.8.1, conda py3.11, nvcc 12.9, **torch 2.11.0+cu128**, flash-attn 2.8.3, nvdiffrast 0.4.0, CuMesh, FlexGEMM, o-voxel all built for sm_120) | Pixal3D `master` @ `f7cf384` (not the `paper` branch), **NATTEN 0.21.0** compiled with `NATTEN_CUDA_ARCH=12.0` (libnatten cutlass-fna verified on the 5090), utils3d 0.0.2 wheel specified by Pixal3D, MoGe @ `74fbce0`, transformers 4.57.3, diffusers 0.37.1 |
| `asset-studio/image-worker` | python 3.11 slim | torch 2.11.0+cu128, diffusers 0.40.0, transformers 5.17.0, accelerate 1.15.0 |
| `asset-studio/blender` | python 3.11 slim | bpy 4.5.13 (Blender 4.5 LTS), meshoptimizer 0.2.30a0 (bound with explicit argtypes), fast-simplification (fallback), trimesh, pygltflib |
| `asset-studio/studio` | python 3.12 slim | fastapi, uvicorn, pydantic, httpx, pygltflib (no CUDA) |

Models (Hugging Face, pinned revisions, cached in volume `studio-models`): `Qwen/Qwen-Image-2512`,
`TencentARC/Pixal3D` (single-view ckpts; `*_mv` optional), `Ruicheng/moge-2-vitl`,
`camenduru/dinov3-vitl16-pretrain-lvd1689m`, `ZhengPeng7/BiRefNet`, plus `valeoai/NAF` (torch.hub, pinned commit).

## How the stages work

* **Reference image** – `promptbuilder.py` turns the request into a single-object, plain-background, three-quarter,
  soft-lit, no-text prompt plus a strong negative prompt; the `mobile_factory` style adds chunky-silhouette rules.
  Qwen-Image-2512 runs at the model card's full-quality settings (50 steps, `true_cfg_scale` 4.0, 1328×1328) in BF16:
  text encoder on GPU → encode → freed; the 41 GB transformer is loaded with an explicit device map (30/60 blocks
  GPU-resident, the rest CPU-offloaded and streamed by accelerate hooks) because the Docker VM has 46 GB RAM.
  `quality` mode makes 2 candidates; candidates are scored by transparent technical checks (background plainness,
  framing margins, clipping, centring, foreground fraction) recorded in `reference_candidates/selection.json`.
  An external local vision evaluator can be plugged in with `STUDIO_VISION_EVALUATOR_URL` (optional, off by default).
* **Pixal3D** – upstream `inference.py` path unchanged: BiRefNet matting + crop, MoGe-2 camera (FOV/distance),
  `Pixal3DImageTo3DPipeline.run(pipeline_type="1024_cascade"|"1536_cascade")` with upstream sampler defaults and
  low-VRAM on-demand loading. Only the export is replaced by an adapter around `o_voxel.postprocess.to_glb`
  (configurable decimation target/texture size instead of the hardcoded 1M/4096, **PNG instead of WebP**).
  The gated `briaai/RMBG-2.0` matting model is swapped for the open `ZhengPeng7/BiRefNet` via a local
  `pipeline.json` override (`STUDIO_REMBG_MODEL` switches back if you have access).
* **Blender** – imports the master (never modified), applies `height_m` (uniform scale), origin at bottom-centre,
  removes loose/degenerate geometry and only tiny debris (components < 0.5 % of the diagonal, position-based
  connectivity so UV-seam splits do not count as parts), then reduces to `target_triangles` the way engine tooling does:
  **meshoptimizer** (`meshopt_simplifyWithAttributes`, the simplifier behind gltfpack and the Unity/Unreal plug-ins) on the
  welded master with vertex normals as attributes (weight 0.7), `SimplifyPrune` for isolated shells and a relative
  `target_error` that is relaxed step by step (1 % → 3 % → 5 % → 20 % → unbounded) only if the budget cannot be met;
  the achieved error is recorded in the manifest. Light reductions (≥ 35 %) keep the master UVs/textures via Blender's
  seam-delimited collapse. If quadric simplification stalls above the budget (many thin shells), a single-shell voxel
  proxy of the *copy* is reduced instead (recorded with the voxel size). Reduced meshes get Smart-UV + packed islands and
  base colour / metallic / roughness / alpha / tangent-space normal baked from the untouched master with Cycles; a
  hit-mask bake fills ray misses. LODs are simplified from LOD0 with their own UVs and (smaller) bakes; the collision
  mesh is a decimated convex hull. Previews are Cycles CPU renders (4 asset views + master + contact sheet).
  `POST /v1/jobs/{id}/retry {"from_stage":"blender","optimize":{"target_triangles":20000}}` re-optimises a finished job
  without regenerating the image or the 3D model.
* **Validate** – pure-python GLB inspection: primitives, triangle/vertex counts, UVs, normals, material texture
  slots, image mime types and sizes, `extensionsRequired` (WebP rejected), bounds, height, origin, budgets.
* **Package** – `manifest.json` with everything above and sha256 of each artifact.

Failure handling: CUDA OOM in a GPU stage releases the subprocess and retries with the preset's fallback ladder;
entries marked `quality_loss` are only applied when `allow_quality_fallback` is true, and every fallback is
recorded as a warning with requested vs. effective settings. Jobs are durable in SQLite; a worker restart requeues
running jobs and completed stages (`stages/<name>/result.json`) are reused. Before a GPU stage starts the worker
checks `nvidia-smi` and waits (never kills) if another workload holds more than `STUDIO_GPU_BUSY_THRESHOLD_MIB`.

## What the output is (and is not)

* `master.glb` – Pixal3D export, up to 1 M triangles, 4096² PBR textures (base colour + alpha, metallic-roughness).
* `<name>.glb` – **optimized static asset**: automatic decimation, not animation-ready topology, no rig.
* `<name>_LOD1.glb`, `<name>_LOD2.glb`, `<name>_collision.glb`, `previews/*.png`, `reference.png`, `manifest.json`.
* Units: metres, glTF Y-up, origin at bottom centre. A normal map exists only when the manifest says it was baked.

## Multi-view (optional)

`multiview` accepts coherent posed views + `transforms.json` semantics (4×4 camera-to-world, Z-up, `camera_angle_x`),
validated (file refs, rotation orthonormality, look-at, front view canonical) and fed to Pixal3D's `inference_mv`
path with the `*_mv` checkpoints (`scripts\prefetch.ps1 -MultiView`). There is **no** automatic novel-view
generation: four independently prompted images with made-up poses are not a calibrated multi-view input.

## Tests

```powershell
docker compose run --rm --no-deps -v ${PWD}:/src api python -m pytest -q /src/tests/test_api_mock.py   # mocked error paths
python tests\acceptance_real.py --cancel-test --restart-test                                          # real GPU runs
```

See `docs/RESULTS.md` for measured timings, VRAM/RAM and known limitations.
