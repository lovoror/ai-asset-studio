# Running asset-studio across machines

asset-studio is a **control plane plus worker services**. The control plane owns the job database and all job
output; each worker owns nothing but a scratch directory and its own model cache. They talk over HTTP, so no shared
drive, mount or container network is involved.

```
                 control machine                       2D server                 3D server
   ┌──────────────────────────────────────┐      ┌──────────────────┐     ┌──────────────────┐
   │  studio.api      (portal + HTTP API) │      │ runner --roles   │     │ runner --roles   │
   │  studio.worker   (orchestrator)      │─────▶│        image     │     │      pixal3d     │
   │  runner --roles  blender  (CPU)      │      │ torch+diffusers  │     │ TRELLIS.2+Pixal3D│
   │  var/jobs   var/data (SQLite)        │─────▶│ HF_HOME          │     │ HF_HOME/TORCH_HOME│
   └──────────────────────────────────────┘      └──────────────────┘     └──────────────────┘
        [servers] image = http://2d:8701            8701/tcp                 8702/tcp
```

Everything below assumes one checkout of this repository per machine (a worker needs `services/`, `presets/` and its
own `config.toml`).

## 1. Decide what runs where

| Role | Runs | Needs | Default on |
|---|---|---|---|
| `control` | the HTTP API + portal, the orchestrator, and every stage configured as `local` | Python only, no GPU | the machine you use |
| `image` | the reference-image stage (Qwen-Image / Z-Image / FLUX) | one NVIDIA GPU, ~60 GB weights | wherever you put it |
| `pixal3d` | the 3D stage (Pixal3D / TRELLIS.2) | one NVIDIA GPU with a built TRELLIS.2 environment | wherever you put it |
| `blender` | reduce / bake / LOD / preview, CPU only | `bpy` — needs its own Python (3.11 for the pinned bpy 4.5; there is no cp312 build) | the control machine |

`[servers]` in `config.toml` is the only place the topology is described. A stage set to `"local"` is started by
`serve.py control` on that machine and reached on `127.0.0.1`; anything else is a URL.

Blender can equally well run on the 3D server (it usually has idle CPU and the master GLB is already there) — point
`[servers] blender` at it and start `serve.py blender` there. See [Moving Blender](#moving-blender) below.

For what a stage worker actually is, why the project has them instead of containers, and how `[servers]` differs
from the generation-backend settings, see [`STAGES.md`](STAGES.md) — this document is the how-to.

## 2. Control machine

```powershell
# Windows
scripts\bootstrap.ps1 control
# macOS / Linux
./scripts/bootstrap.sh control
```

`config.toml`:

```toml
[control]
bind = "0.0.0.0"          # 127.0.0.1 if only this machine should see the portal
port = 8090
api_token = "…"           # required as soon as bind is not loopback
cors_origins = ""         # only needed if the portal is served from another origin

[servers]
image   = "http://192.168.1.21:8701"
pixal3d = "http://192.168.1.22:8702"
blender = "local"

[worker]                  # the local Blender worker
bind = "127.0.0.1"
token = "…"               # same value as on the worker machines

[paths]
jobs = "var/jobs"
data = "var/data"
models = ""               # not needed here: no GPU stage runs locally

[stage]
rembg_model = "ZhengPeng7/BiRefNet"   # or briaai/RMBG-2.0 if you accepted its licence
```

```powershell
python scripts\serve.py doctor      # prints the topology and probes every server
scripts\start.ps1                   # background: var\logs\control.log, scripts\stop.ps1 to stop
# or: python scripts\serve.py control     (foreground, Ctrl-C to stop)
```

The orchestrator needs no GPU and no model weights, so this machine stays light. `var/jobs` grows with the assets
you keep; `var/data` holds `studio.db` and each local worker's scratch.

## 3. A worker machine (2D shown; the 3D one is identical apart from the role)

```powershell
scripts\bootstrap.ps1 image     # installs requirements/image.txt, prefetches the weights
```

`config.toml` on that machine:

```toml
[worker]
bind = "0.0.0.0"        # accept the control machine's connections
token = "…"             # must match the control machine; a 0.0.0.0 bind refuses to start without one
image_port = 8701
roles = "image"         # optional belt-and-braces: this box only ever runs the image stage

[paths]
data = "var/data"       # this worker's scratch (created and cleaned by the worker itself)
models = "D:/models"    # where the weights live; <models>/hf and <models>/torch

[stage]
rembg_model = "ZhengPeng7/BiRefNet"
```

```powershell
python scripts\serve.py image
```

That worker will:
* answer `/health` openly (for monitoring) and require the token for everything else;
* refuse to start if it is bound to a non-loopback address with an empty token;
* only accept the stage modules of the roles it was started with, so a stolen token cannot run arbitrary Python;
* keep its scratch under `[paths] data/worker/<role>` and delete leftovers from its previous run on start.

Prefetch the weights **on the worker machine** — there is no shared model volume any more:

```powershell
python scripts\prefetch.py --role image          # 2D: Qwen-Image, Z-Image/FLUX configs
python scripts\prefetch.py --role pixal3d --mv   # 3D: Pixal3D (+ multi-view), MoGe, DINOv3, BiRefNet, NAF
```

The 3D role also needs its CUDA base environment (torch cu128, flash-attn, nvdiffrast, CuMesh, FlexGEMM, o-voxel,
NATTEN built for the GPU's compute capability). `requirements/pixal3d.txt` documents exactly what and how;
`scripts/bootstrap.py --role pixal3d` checks it and reports what is missing.

## 4. Security: tokens and addresses

Both sides have a token, and both refuse the dangerous configuration:

* `[worker] token` — shared secret. A worker whose `bind` is not loopback will not start without it. The control
  plane sends it on every worker call, including the bundle downloads.
* `[control] api_token` — bearer token for the portal, CLI and MCP. Required as soon as `[control] bind` is not
  loopback. The CLI reads `STUDIO_API_TOKEN` (or `?token=` in a URL) and `STUDIO_URL`.

Worker traffic carries job inputs and outputs (reference images, meshes, textures) unencrypted over plain HTTP. On a
trusted LAN that is usually fine; across an untrusted network put the workers behind WireGuard/SSH, or terminate TLS
in front of them. Do not expose a worker port to the internet.

## 5. What crosses the network, and how much

Per stage the control plane uploads the request plus the inputs that stage reads, and downloads the stage's output
directory:

| Stage | Uploaded to the worker | Downloaded back |
|---|---|---|
| `reference` | the request (prompt, model, size, seeds) | candidate PNGs, `selection.json`, `output.json` |
| `pixal3d` | the chosen reference image, or a multi-view folder | `master.glb` (tens to a few hundred MB), `preprocessed.png`, `output.json` |
| `blender` | `master.glb` + `reference.png` | optimized GLB, LODs, collision hull, textures, previews, `output.json` |

A job therefore moves the master mesh roughly twice if Blender runs on the control machine (down from the 3D server,
up again only if Blender is remote). All transfers are streamed and size-agnostic; `[stage] transfer_timeout_s`
(default 1800 s) is what to raise on a slow link.

## 6. Operating

| Task | Command |
|---|---|
| Report readiness of this machine and probe the servers | `python scripts/serve.py doctor` |
| Check the whole deployment through the API | `python cli\assetctl.py doctor` (`--deep` loads the models on each worker) |
| Very cheap "do the workers answer" probe | `POST /v1/selftest?deep=false` |
| Start / stop the control plane in the background (Windows) | `scripts\start.ps1`, `scripts\stop.ps1` |
| Tail the control-plane log | `scripts\logs.ps1` |
| Run a role in the foreground (any OS) | `python scripts/serve.py control\|image\|pixal3d\|blender\|all` |
| Restart just the worker on one machine | Ctrl-C that `serve.py` and start it again — the control plane waits, then re-attaches to a stage still running, or starts a fresh one |

Ports to open from the control machine to each worker: **8701** (image), **8702** (pixal3d), **8703** (blender), or
whatever `[worker] *_port` says. The control plane only ever calls out; workers never call back.

On Windows, `scripts\stop.ps1` uses `taskkill /T`, so a stage subprocess running on the control machine dies with
the service. To stop a machine that was started in the foreground, Ctrl-C is enough — `serve.py` takes its children
down with it.

## 7. Moving Blender

Blender is CPU-only, so it is a matter of bandwidth versus CPU:

* **on the control machine (default)** — the master GLB is already there; nothing extra crosses the network;
* **on the 3D server** — the master GLB never leaves that machine, and its CPU does the baking; the final optimized
  GLB, textures and previews come back instead (usually much smaller than the master).

```toml
# control machine
[servers]
pixal3d = "http://192.168.1.22:8702"
blender = "http://192.168.1.22:8703"
```

On the 3D server, start two roles (each `serve.py` is a separate process, and each worker keeps its own scratch):

```powershell
python scripts\serve.py pixal3d      # one terminal
python scripts\serve.py blender      # another terminal
```

`python scripts\serve.py all` on a machine that should host every worker also works, and is the single-machine
setup.

## 8. Generating on ComfyUI servers instead of local GPUs

If the GPUs you want to use already serve ComfyUI, you do not need a torch environment at all: the stage workers
become light HTTP proxies and **nothing needs to be installed on the ComfyUI machines**. This is orthogonal to
section 1 — `[servers]` says which machine runs the stage *process*, the generation-backend settings say which
ComfyUI server that process talks to.

```
control machine (this repo)                       ComfyUI boxes (no asset-studio at all)
┌────────────────────────────────────────┐
│ studio.api / studio.worker             │
│ runner:image   ── HTTP ────────────────┼──▶ http://127.0.0.1:8188     images (KREA-2-TURBO.json)
│ runner:pixal3d ── HTTP ────────────────┼──▶ http://10.0.0.9:8388      3D     (3d_pixal3d_trellis2_*.json)
│ runner:blender   CPU only              │
└────────────────────────────────────────┘
```

### Setup

1. One checkout of this repo on the machine that will run the stage processes (the control machine). Then:

   ```powershell
   python scripts\bootstrap.py --role comfy      # httpx, pillow, numpy, scipy, pyyaml - no torch
   python scripts\bootstrap.py --role blender    # CPU post-processing; needs its OWN Python (see section 1)
   python scripts\serve.py doctor                # per-stage interpreter + readiness
   ```

2. Put the **API-format** workflows in `presets/workflows/`. A UI-format export (`nodes`/`links` at the top level)
   is rejected on load — re-save it from ComfyUI with Dev Mode on → **Export (API)**.

3. Point each stage at its server. Two equivalent ways:

   * portal → **Settings → Generation backends**, or
   * `PUT /v1/settings`:

   ```json
   {
     "default_image_model": "krea2-turbo",
     "image_kind": "comfyui", "image_url": "http://127.0.0.1:8188",
     "image_workflow": "KREA-2-TURBO.json",
     "three_d_kind": "comfyui", "three_d_url": "http://10.0.0.9:8388",
     "three_d_workflow": "3d_pixal3d_trellis2_image_to_model.json",
     "trellis2": false,
     "nodes": {"branch": "316", "resolution": "94", "decimate": "186", "texture": "288",
               "image": "122", "output": "322"}
   }
   ```

   These are runtime settings, snapshotted into each job's `manifest.json`, so repointing them never changes what a
   finished job recorded. `presets/image_models.yaml` ships `krea2-turbo` / `krea2-turbo-1536` entries for exactly
   this workflow (8 steps, cfg 1.0, euler/simple, and 1024² because the model does not fit 8 GB at 1328²).

### Node ids: images need none, 3D does

The **image** lane derives everything from the graph — `KSampler.positive`, `.negative`, `.latent_image` — so any
ordinary KSampler txt2img workflow works untouched. The stage log prints what it found:

```
[comfy] image workflow KREA-2-TURBO.json: sampler 8, prompt node 6, negative 7, latent 9 (EmptyLatentImage)
```

The **3D** lane cannot derive anything: nothing marks which `PrimitiveInt` feeds the texture bake. So `nodes` maps
a role to a node id, optionally as `"<id>:<input key>"` when the default key guess is wrong. For the
Pixal3D/TRELLIS.2 workflow these are the ids, with what asset-studio writes into them:

| role | node (class) | input key | written from | workflow's own value |
|---|---|---|---|---|
| `branch` | `316` PrimitiveBoolean | `value` | the `trellis2` setting (true = TRELLIS.2, false = Pixal3D) | `false` (= Pixal3D) |
| `resolution` | `94` Trellis2UpsampleStage | `target_resolution` | the quality preset's `pixal3d.resolution` (1024 balanced / 1536 quality) | `1536` |
| `decimate` | `186` DecimateMesh | `target_face_count` | `master.decimation_target`, i.e. `master_triangles` on the request | `700000` |
| `texture` | `288` PrimitiveInt | `value` | `master.texture_size`, i.e. `master_texture_size` on the request | `4096` |
| `image` | `122` LoadImage | `image` | the chosen reference, uploaded to the server | a sample image |
| `output` | `322` Save3DAdvanced | — | which artifact is the deliverable | the only Save3DAdvanced |

An unset role is left alone, so the workflow's own value stands. `image` and `output` are auto-detected when unset
(`output` by class, most specific first), so the only ones worth configuring deliberately are the four budget /
branch knobs.

### Limits worth knowing

* **`nodes.branch` is what makes the portal's TRELLIS.2 / Pixal3D toggle work.** Leave it unset and the workflow's
  own branch runs, whatever the toggle says.
* **`seed` can only name one node.** A TRELLIS.2 graph has four KSamplers, so wiring one gives partial
  reproducibility; leaving it unset keeps the workflow's fixed seeds (every run of the same workflow is then
  identical, which is deterministic but not seed-controlled).
* **`nodes.normal` is declared but not applied** by the current adapter: the normal-map bake (`224`
  BakeNormalMapFromMesh) keeps the resolution in the workflow file, whatever `texture` says. Edit the workflow if
  you want the normal map at the same size as the colour maps.
* **`resolution` for node `94` is a plain INT** (1024–2048, step 128), not a combo, so the preset values pass
  straight through. `snap_combo` only matters for workflows whose node really is a dropdown.
* **The 3D workflow may not hit the requested master budget.** If the remote decimation node lands more than 25 %
  away from `decimation_target`, the manifest gets a warning; the real count comes from the GLB, not from what was
  asked.
* **`must be .glb`.** The Blender stage and the validator both assume it, and the 3D lane rejects anything else
  before those stages see it.
* The 3D lane needs a **single reference image**; multi-view input requires the local Pixal3D backend.

### Measured on the reference setup

| stage | server | result |
|---|---|---|
| images (KREA-2-TURBO, 1024², 8 steps, Q4 GGUF) | RTX 4060 8 GB, local | 150 s for 1 candidate, candidate score 1.00, no warnings |
| 3D (Pixal3D/TRELLIS.2) | RTX 4090 24 GB, `--novram` | ~8 min, `Save3DAdvanced` → textured GLB with baseColor + metallicRoughness + normal + occlusion |

A ComfyUI server started with `--novram` (or sharing its card with another resident model) is considerably slower
than a VRAM-resident one — that is normal, not a misconfiguration.

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `nothing to start for this role` | `[servers]` marks everything remote; start `serve.py control` on the control machine instead |
| Worker stays `UNREACHABLE` in `doctor` | firewall/port, wrong host in `[servers]`, or the worker is bound to `127.0.0.1` on its own machine (set `[worker] bind = "0.0.0.0"`) |
| Worker exits at once with "refusing to listen … without a token" | set `[worker] token` on that machine and the same value on the control machine |
| `missing or invalid worker token` in a stage log | the two `[worker] token` values differ (the control plane's `[worker] token` is what it sends) |
| Worker answers but every stage fails with `no module named …` | the worker machine's environment is missing that role's dependencies: `python scripts/bootstrap.py --role <role>` |
| `module '…' is not served by role(s) …` | the worker was started with `--roles`/`[worker] roles` that exclude that stage |
| 3D stage fails instantly with a CUDA/torch import error | the TRELLIS.2 base environment is not built on that machine — see `requirements/pixal3d.txt` |
| Stage fails with "reference image not found … cannot be transferred" | a request referenced a path outside the job directory; the control plane cannot ship it to a worker |
| `not healthy within …` from `scripts\start.ps1` | read `var\logs\control.log`: usually a worker is down or the API bound to a port already in use |
| Bundles come back but outputs are missing | check the stage log (mirrored on the control machine) — the stage itself failed; the bundle only contains what it wrote |

## 10. Coming from the container layout

If you ran the earlier Docker version, the changes to be aware of:

* **No volumes.** `studio-models` becomes `[paths] models` per machine, `studio-jobs` becomes `[paths] jobs` on the
  control machine, `studio-data` becomes `[paths] data`. Copy those directories out of the volumes before deleting
  them if you want the old jobs and weights.
* **No shared `/jobs`.** A worker stages its runs under its own `[paths] data/worker/<role>`.
* **Compose service names are gone.** `http://image-worker:8701` becomes a real address such as
  `http://192.168.1.21:8701`; the ports are unchanged (8701/8702/8703).
* **Two new settings are mandatory in practice:** `[worker] token` (the worker API runs code) and, for a reachable
  portal, `[control] api_token`.
* The stage code, presets, job API and portal behaviour are unchanged: existing `var/jobs` directories and
  `studio.db` are still valid, so a migrated job can be re-optimised or downloaded as before.
