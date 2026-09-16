# Stage workers

A **stage worker** is a small HTTP service that runs one stage of a job as a subprocess, on the machine that owns
that stage's Python environment. It is `services/runner/runner.py`: standard library only, no CUDA code, no torch,
no web framework. The control plane starts one worker process per role, calls it over HTTP, and never shares a
filesystem with it.

The short answer to "why does this exist?" is that it replaces Docker. asset-studio deliberately has no containers
(since 0.2): a stage worker is what lets `reference`, `pixal3d` and `blender` run on machines with different Python
environments and different GPUs, while the control plane keeps the job database and every artifact.

This document is the reference for the concept and the boundary. For *how to deploy* workers across machines, see
[`DISTRIBUTED.md`](DISTRIBUTED.md) — in particular its section 5 (what crosses the network), section 7 (moving
Blender), section 8 (ComfyUI instead of local GPUs) and section 9 (troubleshooting); none of that is repeated here.

## 1. What a stage worker is

`services/runner/runner.py` is a `ThreadingHTTPServer` with one job: for each stage attempt, create a run, accept
the uploaded inputs, spawn `python -m <module> --request <run>/request.json`, mirror the log, and hand back the
stage's output directory as a zip. It runs **one stage subprocess at a time** (`CURRENT["run_id"]`); a second
`start` while one is running is answered with `409 busy`.

Roles are fixed at start-up: `--roles` on the command line, or `STUDIO_WORKER_ROLES` in the environment
(`scripts/serve.py` passes both, from `[worker] roles` and the role being started). A worker started without
`--roles` accepts every role.

```text
python -m runner.runner --name blender-worker --roles blender \
    --host 0.0.0.0 --port 8703 --token <token> --work-dir var/data/worker/blender
```

`scripts/serve.py <role>` is what builds that command line. The interpreter it uses is `[python] <role>` when set,
otherwise the interpreter running `serve.py`; the port is `[worker] <role>_port` (defaults 8701/8702/8703); the work
directory is `[paths] data/worker/<role>` — one per role, because each worker prunes *its own* scratch on start.

### Endpoints

Auth: every endpoint except `/health` requires the bearer token whenever `[worker] token` is configured.

| Endpoint | What it does |
|---|---|
| `POST /runs` | create a run from `{"job_id","stage","module"}`; `403` when `module` is not in the worker's allowlist |
| `PUT /runs/<id>/input/<name>` | upload one input file as a raw body (`Content-Length` or chunked); only while the run is `created` |
| `POST /runs/<id>/start` | start the stage with `{"request": {...}}`; `409` if another run is active |
| `GET /runs/<id>` | `state` (`created`/`running`/`exited`), `pid`, `returncode`, `cancelled`, `peak_rss_mb`, `peak_gpu_used_mib` |
| `GET /runs/<id>/log?offset=N` | the stage's stdout+stderr from byte offset `N`; `X-Log-Size` carries the total, so the caller appends and continues from there (also `?tail=N`) |
| `GET /runs/<id>/bundle` | zip of the run's `out` directory (store-only; the payload is already-compressed GLB/PNG/JPEG) |
| `POST /runs/<id>/cancel` | terminate the process tree: graceful first, then hard after 10 s |
| `DELETE /runs/<id>` | drop the run record and its work directory |
| `POST /cancel_current` | cancel whatever run is active — recovery after a control-plane restart |
| `POST /exec` | short synchronous helper (no GPU work), used for `--list`-style probes |
| `GET /health` | `ok`, `busy`, `run_id`, `name`, `roles`, `version`, `platform`, `python` — the only unauthenticated endpoint |
| `GET /gpu` | `nvidia-smi` summary: GPUs and compute processes |

### Path placeholders

Nothing is shared: every path a stage needs is either uploaded before the start or created inside the run
directory. The control plane rewrites absolute paths on the way out (`runner_client.StagePlan`) and the worker
resolves the placeholders on the way in (`resolve_placeholders`):

```text
on the control plane                          on the worker
<jobs>/<id>/stages/<stage>/...             -> @out/...                          -> <run>/out/...
<jobs>/<id>/artifacts/master.glb           -> @in/artifacts/master.glb  (uploaded) -> <run>/in/artifacts/master.glb

@out            -> <run>/out                    what the stage produces; this is what the bundle returns
@in/<relpath>   -> <run>/in/<relpath>           a file uploaded before start
@work           -> <run>                        the run directory itself
@presets[/<p>]  -> the worker's own checkout     (STUDIO_PRESETS_DIR on that machine)
@root[/<p>]     -> the worker's own repo root    (STUDIO_ROOT on that machine)
```

Both `@in/...` and the placeholder payloads are validated: a path may not be absolute, may not carry a drive
letter, and may not traverse (`safe_relpath`, `safe_name`). A request that references a file *outside* the job
directory cannot be shipped at all — the path is left as-is and the stage reports whatever it reports.

## 2. The five stages of a job

`pipeline.STAGES` is `["reference", "pixal3d", "blender", "validate", "package"]`.

| # | Stage | What it does | Role | Module | Leaves the control plane? |
|---|---|---|---|---|---|
| 1 | `reference` | builds the prompt from the request + style, generates the candidate images, scores them, picks one | `image` | `image_worker.generate`, or `comfy_worker.image_generate` | yes — HTTP to the `image` stage worker |
| 2 | `pixal3d` | one reference image (or a multi-view folder) → `master.glb` | `pixal3d` | `pixal3d_worker.generate`, or `comfy_worker.three_d_generate` | yes — HTTP to the `pixal3d` stage worker |
| 3 | `blender` | reduce / bake / LODs / collision hull / preview renders, from `master.glb` | `blender` | `blender.process_asset` | yes — HTTP to the `blender` stage worker |
| 4 | `validate` | `validate_glb` over the master, the optimized asset, every LOD and the collision hull | — | `studio.validate.validate_glb` | **no** — runs in the control-plane process |
| 5 | `package` | writes `manifest.json`, the artifact list and every `sha256` | — | `studio.pipeline` | **no** — runs in the control-plane process |

**Only `reference`, `pixal3d` and `blender` are shipped to a stage worker.** `validate` and `package` are plain
Python over files that are already in the job directory, so they call into the process directly: no worker, no
upload, no download, no `result.json` to collect. That is why the readiness gate never checks a `validate` or
`package` worker — there is none.

### Which module runs, and why

| The lane points at | `reference` runs | `pixal3d` runs |
|---|---|---|
| the local backend | `image_worker.generate` (diffusers + torch) | `pixal3d_worker.generate` (Pixal3D / TRELLIS.2 + torch) |
| a ComfyUI server (`kind = "comfyui"`) | `comfy_worker.image_generate` | `comfy_worker.three_d_generate` |

`blender` always runs `blender.process_asset`. The choice is made by `pipeline.JobRun.remote(lane)`, which is
simply "is this lane's `kind` `comfyui`?". It matters because the two families are completely different weights:
`comfy_worker.*` imports only the standard library and `worker_paths` (its HTTP client is `urllib`), so a worker
whose lanes both point at ComfyUI needs **no torch and no CUDA at all** — it is an HTTP proxy. The local modules
import torch and load the models inside the worker process.

Two stages are sometimes not run at all:

* `reference` is skipped when the caller supplied a reference image, when the job carries a multi-view folder, or
  when the job is a variation of an image job (`stage_reference_provided`, `stage_reference_from_parent`). The
  `image` worker is then not needed for that job.
* An **image job** (`kind = "image"`) runs `reference` then `package` and stops: it produces variations, and
  nothing is turned into 3D until you pick one.

## 3. Why it exists

Docker was removed from this project on purpose (README: "since 0.2 no Docker either"). A stage worker is what
took its place, and each of its properties answers a problem that containers used to solve:

* **Per-role environment isolation.** `bpy` publishes exactly one interpreter tag per release (4.5/5.0 are cp311,
  5.1+ are cp313) and there is no cp312 build at all, while the control plane runs on any Python 3.11+. torch,
  diffusers and numpy versions conflict between roles too. One virtual environment per role, one worker process per
  role, and never a shared interpreter.
* **Running repo Python code on a different machine without a shared volume.** The worker has its own checkout and
  its own model cache; the only thing that crosses is HTTP. Inputs are uploaded, outputs come back as a bundle, and
  the placeholders above make every path local to the run.
* **Process isolation with no long-lived GPU memory.** The worker deliberately imports no CUDA/torch code, so the
  process that stays resident holds no VRAM; when a stage child exits, the driver releases all of its CUDA
  allocations. Without this, the 2D and 3D stages would have to coexist in one process and one CUDA context.
* **Crash and OOM containment.** A stage that segfaults, exhausts VRAM or hangs kills only its own child. On
  timeout (`[stage] stage_timeout_s`, default 7200 s) or cancel, the control plane cancels the run and the worker
  kills the whole **process tree** — `taskkill /F /T` on Windows, `killpg(SIGKILL)` elsewhere after a 10 s
  `SIGTERM` grace — because a stage shells out to helpers and killing only the python process would leave the GPU
  busy.
* **A module allowlist instead of a remote shell.** A worker accepts only the modules belonging to the roles it was
  started with:

  | Role | Modules it may run |
  |---|---|
  | `image` | `image_worker.generate`, `comfy_worker.image_generate` |
  | `pixal3d` | `pixal3d_worker.generate`, `comfy_worker.three_d_generate` |
  | `blender` | `blender.process_asset` |

  Anything else is refused with `403` (and `/exec` applies the same list). Extra modules are opt-in only, via
  `--allow-module` / `STUDIO_WORKER_EXTRA_MODULES`, so the default stays closed. The 2D box cannot be asked to run
  Blender, and a stolen token cannot turn a worker into a general-purpose remote shell.
* **A bearer token, and a refusal to be exposed accidentally.** The API runs code, so it is not open: every
  endpoint except `/health` needs `[worker] token`, and a worker whose `bind` is not loopback **refuses to start**
  without one. `/health` stays open on purpose so monitoring and `serve.py doctor` work.

The worker is also disposable: it prunes its whole work root on start (a stage's scratch is never authoritative —
the control plane keeps its own copy of every artifact), drops finished runs after 24 h and never-started runs after
1 h, and the run only ever produces files under its own directory.

## 4. The two orthogonal configuration axes

This is the part that gets confused. There are two independent settings, and they answer two different questions.

```text
Axis A - config.toml [servers]  (or Settings -> Stage workers)
  WHICH MACHINE runs the stage process, and where this control plane reaches it.

    [servers]
    image   = "local"                       -> http://127.0.0.1:8701  (this machine, over loopback HTTP)
    pixal3d = "local"                       -> http://127.0.0.1:8702
    blender = "local"                       -> http://127.0.0.1:8703
    # or, a worker on another machine (a bare "host:port" works too):
    image   = "http://192.168.1.21:8701"    -> that machine's runner, plain HTTP
    pixal3d = "http://192.168.1.22:8702"
    blender = "http://192.168.1.22:8703"

Axis B - generation backend, stored in SQLite, edited on Settings -> Generation backends
         (or PUT /v1/settings). WHICH ComfyUI SERVER that stage process talks to.

    image_kind   = "comfyui"   image_url   = "http://127.0.0.1:8188"
                               image_workflow = "KREA-2-TURBO.json"
    three_d_kind = "comfyui"   three_d_url = "http://10.130.136.171:8388"
                               three_d_workflow = "3d_pixal3d_trellis2_image_to_model.json"
```

The two are chained, not alternatives:

```text
control plane  --- Axis A --->  stage worker process  --- Axis B --->  ComfyUI server
(this repo)                     (image_worker.* or                     (the GPU)
                                 comfy_worker.*)
```

`local` is not an in-process call. It means "start that worker process on this machine and still talk to it over
loopback HTTP on port 8701/8702/8703" (`config.normalize_endpoint`). The whole transfer protocol is identical
whether the worker is on this box or another one; the only thing that changes is the address.

### The consequence

Because `comfy_worker.*` are torch-free HTTP proxies, **"2D generation and 3D generation live on different servers"
is satisfied by axis B — the ComfyUI addresses — and not by axis A.** A perfectly working single-machine setup
legitimately has all three `[servers]` set to `local`:

```text
control machine (one checkout of this repo)              GPU boxes (no asset-studio installed)
+------------------------------------------------+
| studio.api        portal + HTTP API            |
| studio.worker     orchestrator                 |
| runner:image      loopback HTTP :8701 ---------+---> http://127.0.0.1:8188       RTX 4060 8 GB
| runner:pixal3d    loopback HTTP :8702 ---------+---> http://10.130.136.171:8388  RTX 4090 24 GB (--novram)
| runner:blender    loopback HTTP :8703 (CPU)    |
| var/jobs  var/data (SQLite)                    |
+------------------------------------------------+

[servers] image = "local"   pixal3d = "local"   blender = "local"
Settings  image -> http://127.0.0.1:8188        three_d -> http://10.130.136.171:8388
```

Three worker processes, all on this machine, all reached over loopback; two ComfyUI servers, one of them remote.
Nothing about axis A needs to change. On the ComfyUI machines there is no asset-studio at all.

One asymmetry is worth knowing: a **stage worker** is called by the control plane, so *its* health is probed from
the control plane; a **ComfyUI server** is called by the *worker*, so that probe is issued through the worker's own
`/exec` and reports what the worker sees. A ComfyUI address can therefore be unreachable from your laptop and still
be perfectly fine for the stage.

## 5. When you actually need to change axis A

Change `[servers]` (or Settings -> Stage workers) when the *stage process* has to live somewhere else:

* **Blender on a machine with a stronger CPU.** Blender is CPU-only; if the 3D box has idle cores and the master
  GLB is already there, moving the stage avoids shipping the master around (see DISTRIBUTED.md section 7).
* **Running the local (non-ComfyUI) Pixal3D models on the 4090 box.** If you want `pixal3d_worker.generate`
  instead of `comfy_worker.three_d_generate`, the torch + TRELLIS.2 environment and the weights must exist on that
  machine, so the `pixal3d` stage process moves there.
* **Isolating the image lane.** Putting `image` on its own box keeps a long 2D generation off the control machine
  and lets you restart that lane on its own.

The recipe, using Blender as the example. On the worker machine:

```toml
# config.toml on the worker machine
[worker]
bind  = "0.0.0.0"          # accept the control machine's connections
token = "<shared secret>"  # a non-loopback bind refuses to start without it
roles = "blender"          # optional: this box only ever runs the blender stage

[python]
blender = ".venv-blender/Scripts/python.exe"   # bpy is a cp311 wheel; see requirements/blender.txt
```

```powershell
scripts\bootstrap.ps1 blender          # or: python scripts\bootstrap.py --role blender
python scripts\serve.py blender        # one worker process, foreground; Ctrl-C to stop
```

`scripts/serve.py` takes the role as a **positional** argument (`control|image|pixal3d|blender|all|doctor`);
`--role` belongs to `scripts/bootstrap.py`, not to `serve.py`.

On the control machine:

```toml
# config.toml on the control machine
[servers]
blender = "http://192.168.1.22:8703"

[worker]
token = "<shared secret>"   # what this control plane sends on every worker call
```

```powershell
python scripts\serve.py doctor     # prints the topology and probes every stage worker
```

Firewall: open the worker's port **inbound from the control machine** — TCP 8701 (`image`), 8702 (`pixal3d`),
8703 (`blender`), or whatever `[worker] <role>_port` says. The control plane only ever calls out; workers never
call back.

What must be true on the worker machine:

| Requirement | Why |
|---|---|
| A checkout of this repository | the worker runs `runner.runner` and the stage module from *that* tree; `@root/...` resolves there |
| That role's virtual environment | `scripts/bootstrap.py --role <role>`; the role's interpreter goes in `[python] <role>` when it is not the one running `serve.py` |
| `presets/` | the presets this role reads. A ComfyUI lane also resolves its **workflow JSON on the worker** — a workflow that exists only on your laptop is "not found" |
| Its own `config.toml` | `[worker] bind`/`token`, `[paths] data`, `[paths] models` (that machine's weights) |
| Its own scratch | `[paths] data/worker/<role>`; the worker creates and prunes it |

## 6. Operational behaviour

### The readiness gate

`[stage] require_ready` defaults to **on** (`config.REQUIRE_READY`). With it on, starting new work — create, release
and retry — first asks `backends.ready_for_job()` whether every server the job will actually use answers:

* the stage workers the job needs (`required_stages`: an image job needs `image`; an asset job needs `pixal3d` and
  `blender`, plus `image` only when a reference image has to be *generated*; a retry from `blender` needs only
  `blender`);
* each lane whose backend is `comfyui`: its address **and** its workflow file, probed through the worker.

If something is missing, the request is refused immediately with HTTP `422` and a structured body:

```json
{
  "error": "not_ready",
  "problems": [{"code": "worker_unreachable", "role": "pixal3d", "url": "http://127.0.0.1:8702",
                "detail": "the pixal3d stage worker at http://127.0.0.1:8702 is unreachable: ..."}],
  "message": "cannot start: ..."
}
```

The codes (`worker_unreachable`, `backend_not_configured`, `backend_unreachable`) are what the portal localises for
its banner. **Only new work is blocked**: the service itself keeps running, so the portal is still reachable to fix
an address and the library stays usable — exactly what you need when a generation server is down. Setting
`[stage] require_ready = false` restores pre-queueing for setups that would rather queue and retry.

`GET /v1/readiness` is the same information for the UI (and drives the disabled buttons). `POST /v1/backends/test`
tests one address using the values in the form rather than what is saved, so a candidate address can be tested
before saving it.

### Restart semantics

* **An address change takes effect on the next job.** A `Runner` reads `config.effective_runners()` when it is
  constructed, and a runner is built per job/request, so a new `[servers]` value or a Settings change is picked up
  without a restart (`PUT /v1/settings` also drops the cached probe results).
* **Starting or stopping a *local* worker process needs a control-plane restart.** `scripts/serve.py` fixes its
  plan at boot: `control` starts `api` + `worker` + one runner for each stage whose `[servers]` value is local.
  Changing an address to or from `local` changes which processes should exist, and that decision is not re-read.

### Probes

Probes are cached briefly and run in parallel. Stage-worker and ComfyUI results are cached for 15 s (`backends.CACHE_S`)
— except when you press **Test**, which is always fresh — and the three stage addresses are probed concurrently
(at most 8 threads), so a readiness check costs the slowest single probe rather than the sum. On some Windows
setups even a refused loopback connection takes ~2 s, which is the difference between a responsive Settings page
and one that hangs for ten seconds with three dead workers.

## 7. Troubleshooting

The symptom/cause/fix table for the distributed deployment lives in DISTRIBUTED.md section 9; this table only covers
what is specific to the stage-worker boundary.

| Symptom | Cause | Check |
|---|---|---|
| Gate reports `worker_unreachable` for a role | that stage process is not running, the port is blocked, or the address is wrong | `python scripts\serve.py doctor` on both machines; `GET /v1/readiness`; `[worker] bind` must be `0.0.0.0` on the worker |
| Stage log: `missing or invalid worker token` (HTTP 401) | the control plane's `[worker] token` differs from the worker's | compare `[worker] token` on both machines — the control plane sends *its* value |
| Worker exits at once: `refusing to listen on 0.0.0.0 without a token` | a non-loopback `bind` with an empty token | set `[worker] token` on that machine |
| `module '...' is not served by role(s) [...]` | the worker was started with `--roles` / `[worker] roles` that exclude this stage | start it with that role, or widen `[worker] roles` |
| `no module named ...` in the stage log | that role's dependencies are missing **on the worker machine** | `python scripts\bootstrap.py --role <role>` there |
| Backend test says `workflow NOT found on the worker` | the workflow JSON exists in your checkout but not on the worker's | copy it into `presets/workflows/` on the worker |
| The Blender stage dies inside `import bpy` | the role is running on an interpreter without a matching bpy wheel (there is no cp312 build) | `[python] blender`; `python scripts\bootstrap.py --role blender`; `serve.py doctor` prints the interpreter per component |
| `busy`, or `worker ... stayed busy with another run for 600s` | a run left over from a previous control plane | `POST /cancel_current`, or `DELETE /runs/<id>`, on that worker |
| Changed an address to or from `local` and nothing happened | `scripts/serve.py` fixes its plan at boot | restart the control plane (`scripts\stop.ps1`, then `scripts\start.ps1`) |
| The bundle arrives but the stage's outputs are missing | the stage itself failed; the bundle only contains what it wrote | read the stage log, which is mirrored onto the control machine while the stage runs |

## See also

* [`DISTRIBUTED.md`](DISTRIBUTED.md) — deploying the workers: topology, tokens, transfers, moving Blender, ComfyUI
  servers, troubleshooting.
* `config.example.toml` — `[servers]`, `[worker]`, `[python]`, `[stage]`.
* `services/runner/runner.py`, `services/studio/studio/runner_client.py` — the two sides of the protocol.
* `services/studio/studio/backends.py` — the probes and the readiness gate.
