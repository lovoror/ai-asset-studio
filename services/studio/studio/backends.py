"""Connectivity of the addresses the Settings page configures, and the gate that refuses impossible jobs.

Two different things are being reached, and they must be tested from different places:

* a **stage worker** (`config.effective_runners()`) is an HTTP service this control plane calls directly, so its
  health is probed from here;
* a **ComfyUI server** is reached by the *worker*, not by the control plane. A probe from here would answer the
  wrong question (the control plane is usually on a different machine than the 2D/3D worker), so the check is
  issued through the worker's own `/exec` and reports what that worker sees.

`check_ready()` uses the same probes to decide whether a job may be submitted at all: a stage whose server is
down is refused up front with a readable reason, instead of being queued to fail minutes later. Results are cached
briefly because a submission must not pay for a probe that a /health poll just did.
"""
from __future__ import annotations

import json
import re
import time

from collections.abc import Callable
from concurrent import futures

from . import config
from .runner_client import Runner

# A host that does not answer costs the whole connect timeout, and on some Windows setups even a *refused*
# loopback connection takes ~2s (a filtering driver). Probing the addresses in parallel makes a readiness check
# cost the slowest single probe instead of the sum: with three dead workers that is the difference between a
# responsive Settings page and one that hangs for ten seconds.
MAX_PROBE_THREADS = 8


def parallel_probes(calls: dict[object, Callable[[], dict]]) -> dict[object, dict]:
    """Run `{key: callable}` concurrently, preserving the keys. Probes are non-raising by contract."""
    if len(calls) <= 1:
        return {k: f() for k, f in calls.items()}
    with futures.ThreadPoolExecutor(max_workers=min(len(calls), MAX_PROBE_THREADS)) as pool:
        pending = {k: pool.submit(f) for k, f in calls.items()}
        return {k: p.result() for k, p in pending.items()}

# role -> the comfy_worker module that runs that stage (and therefore owns the ComfyUI probe)
COMFY_MODULE = {"image": "comfy_worker.image_generate", "pixal3d": "comfy_worker.three_d_generate"}

# One round trip that both reaches the server and resolves the workflow file *on the worker*, so a missing
# workflow is reported by the machine that would fail to find it. The marker makes the JSON easy to extract from
# whatever else the worker prints.
_PROBE = (
    "import json,sys\n"
    "out={'ok':False,'url':sys.argv[1] if len(sys.argv)>1 else None}\n"
    "try:\n"
    "    from comfy_worker.comfy_client import probe, find_workflow_file\n"
    "    out['server']=probe(sys.argv[1])\n"
    "    out['ok']=True\n"
    "except Exception as e:\n"
    "    out['error']=type(e).__name__+': '+str(e)[:300]\n"
    "if len(sys.argv)>2 and sys.argv[2]:\n"
    "    try:\n"
    "        out['workflow_path']=str(find_workflow_file(sys.argv[2]))\n"
    "    except Exception as e:\n"
    "        out['workflow_error']=type(e).__name__+': '+str(e)[:300]\n"
    "print('AS_PROBE '+json.dumps(out))\n"
)

_CACHE: dict[tuple, tuple[float, dict]] = {}
# Long enough that a portal poll or a burst of submissions is free, short enough that a server which just died
# stops new work quickly. The explicit Test button ignores it (`fresh=True`): pressing Test must really test.
CACHE_S = 15.0


def _cached(key: tuple, produce, fresh: bool = False) -> dict:
    now = time.time()
    hit = _CACHE.get(key)
    if hit and not fresh and now - hit[0] < CACHE_S:
        return {**hit[1], "cached": True}
    out = produce()
    _CACHE[key] = (now, out)
    return out


def test_worker(role: str, timeout: float = 3.0, fresh: bool = False, url: str | None = None) -> dict:
    """Is the stage worker for `role` answering? Probed from here, because this is what calls it.

    `url` probes an address instead of the configured one, so the Settings page can test what is typed in the box
    before it is saved.
    """
    raw = (url or "").strip()
    target = config.normalize_endpoint(role, raw) if raw else None

    def probe() -> dict:
        r = Runner(role, base=target)
        t0 = time.time()
        h = r.health(timeout=timeout)
        ok = bool(h.get("ok"))
        return {"ok": ok, "target": "worker", "role": role, "url": r.base,
                "elapsed_ms": round((time.time() - t0) * 1000),
                "detail": (f"{h.get('name', 'worker')} roles={h.get('roles')} python={h.get('python')}"
                           + ("  BUSY" if h.get("busy") else "")) if ok else f"no answer: {h.get('error', '')}",
                "info": h}
    return _cached(("worker", role, target or ""), probe, fresh)


def test_comfyui(role: str, url: str, workflow: str | None = None, fresh: bool = False) -> dict:
    """Can the worker for `role` reach this ComfyUI server? Run through that worker's /exec."""
    url = (url or "").strip()
    if not url:
        return {"ok": False, "target": "comfyui", "role": role, "detail": "no address configured"}
    if "://" not in url:
        url = "http://" + url
    url = url.rstrip("/")
    return _cached(("comfyui", role, url, workflow or ""),
                   lambda: _probe_comfyui(role, url, workflow), fresh)


def _probe_comfyui(role: str, url: str, workflow: str | None) -> dict:
    module = COMFY_MODULE[role]
    r = Runner(role)
    # "python" is swapped for the worker's own interpreter by the runner (/exec), so the right environment runs it.
    res = r.exec(["python", "-c", _PROBE, url, workflow or ""], timeout=45)
    tail = (res.get("stdout") or "") + (res.get("stderr") or "")
    m = re.search(r"^AS_PROBE (.*)$", tail, re.MULTILINE)
    if not m:
        return {"ok": False, "target": "comfyui", "role": role, "url": url,
                "detail": f"the {role} worker could not run the probe: {tail.strip()[-300:] or 'no output'}"}
    try:
        info = json.loads(m.group(1))
    except ValueError:
        return {"ok": False, "target": "comfyui", "role": role, "url": url, "detail": "unreadable probe output"}
    out = {"ok": bool(info.get("ok")), "target": "comfyui", "role": role, "url": url, "module": module, "info": info}
    if not info.get("ok"):
        out["detail"] = f"the {role} worker cannot reach {url}: {info.get('error', 'no answer')}"
        return out
    s = info.get("server") or {}
    bits = [f"ComfyUI {s.get('comfyui_version', '?')}", str(s.get("gpu") or "")]
    if s.get("vram_total_mib"):
        free = s.get("vram_free_mib")
        bits.append(f"{s['vram_total_mib']} MiB" + (f" ({s['vram_total_mib'] - free} used)" if free else ""))
    detail = " · ".join(b for b in bits if b)
    if workflow:
        if info.get("workflow_error"):
            out["ok"] = False
            detail += f" · workflow NOT found on the worker: {info['workflow_error']}"
        else:
            detail += f" · workflow ok ({info.get('workflow_path')})"
    out["detail"] = detail
    return out


def test(body) -> dict:
    """Dispatch a BackendTest request (api.py) to the right probe. Always fresh: pressing Test must really test."""
    if body.target == "worker":
        return test_worker(body.role, fresh=True, url=body.url)
    return test_comfyui(body.role, body.url or "", body.workflow, fresh=True)


# --------------------------------------------------------------------------------------- submission gate

def required_stages(*, kind: str, generates_reference: bool) -> list[str]:
    """The stage workers a job will actually use.

    An image job (variations only) runs `reference` then `package` and never touches Blender. An asset job always
    runs the 3D stage and Blender, and needs the image worker only when it has to *generate* a reference - a job
    built from a picked candidate or a supplied image copies that file locally instead.
    """
    if kind == "image":
        return ["image"]
    stages = ["pixal3d", "blender"]
    if generates_reference:
        stages.insert(0, "image")
    return stages


def stages_from(from_stage: str | None, *, kind: str, generates_reference: bool) -> list[str]:
    """The stages a *retry* will run. Retrying from `blender` is a local re-optimise: it must not be blocked
    because the 2D/3D generation servers happen to be down."""
    if from_stage == "blender":
        return ["blender"]
    if from_stage == "pixal3d":
        return ["pixal3d", "blender"]
    return required_stages(kind=kind, generates_reference=generates_reference)


def check_ready(backend: dict, *, kind: str, generates_reference: bool,
                stages: list[str] | None = None) -> list[dict]:
    """Problems that make this job impossible right now (empty list = go ahead). Never raises.

    Each problem is structured - `{"code", ...}` plus a diagnostic `detail` - so the portal can show a localised
    sentence and the HTTP error can carry the detail for CLI/script callers.
    """
    problems: list[dict] = []
    stages = stages if stages is not None else required_stages(kind=kind, generates_reference=generates_reference)
    backend = backend or {}

    # Probe every address at once (see parallel_probes): a submission should not wait for the sum of the
    # unreachable hosts.
    calls: dict[object, Callable[[], dict]] = {("worker", role): (lambda r=role: test_worker(r)) for role in stages}
    lane_cfg: list[tuple[str, str, dict]] = []
    for lane, role, needed in (("image", "image", generates_reference), ("three_d", "pixal3d", kind != "image")):
        if not needed or role not in stages:
            continue
        cfg = backend.get(lane) or {}
        if cfg.get("kind") != "comfyui":
            continue  # the local backend loads inside the worker process: nothing external to reach
        lane_cfg.append((lane, role, cfg))
        if cfg.get("url") and cfg.get("workflow"):
            calls[("lane", lane)] = (lambda r=role, c=cfg: test_comfyui(r, c["url"], c["workflow"]))
    got = parallel_probes(calls)

    for role in stages:
        w = got[("worker", role)]
        if not w.get("ok"):
            problems.append({"code": "worker_unreachable", "role": role, "url": w.get("url"),
                             "detail": f"the {role} stage worker at {w.get('url')} is unreachable: {w.get('detail')}"})

    for lane, role, cfg in lane_cfg:
        for key in ("url", "workflow"):
            if not cfg.get(key):
                problems.append({"code": "backend_not_configured", "lane": lane, "missing": key,
                                 "detail": f"the {lane} backend is set to ComfyUI but no {key} is configured"})
        probe = got.get(("lane", lane))
        if probe is not None and not probe.get("ok"):
            problems.append({"code": "backend_unreachable", "lane": lane, "role": role, "url": cfg["url"],
                             "detail": f"the {lane} generation server {cfg['url']} is unreachable from the "
                                       f"{role} worker: {probe.get('detail')}"})
    return problems


def problems_text(problems: list[dict]) -> str:
    """One line per problem, for the HTTP error body (the portal renders the codes instead)."""
    return "; ".join(p.get("detail", p.get("code", "?")) for p in problems)


def readiness(backend: dict, *, kind: str, generates_reference: bool, stages: list[str] | None = None) -> dict:
    problems = check_ready(backend, kind=kind, generates_reference=generates_reference, stages=stages)
    return {"ready": not problems, "problems": problems}


def ready_for_job(settings: dict, *, kind: str, from_stage: str | None = None) -> list[dict]:
    """check_ready() for a job described by its stored/resolved settings (submission, release, retry)."""
    s = settings or {}
    generates_reference = kind == "image" or s.get("input_mode", "text") == "text"
    stages = stages_from(from_stage, kind=kind, generates_reference=generates_reference)
    return check_ready(s.get("backend") or {}, kind=kind, generates_reference=generates_reference, stages=stages)


def invalidate():
    """Drop cached probe results (after the addresses change, so the next check re-probes)."""
    _CACHE.clear()
    config.effective_runners(force=True)
