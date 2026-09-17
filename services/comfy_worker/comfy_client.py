"""Minimal ComfyUI HTTP client for the proxy workers.

Deliberately stdlib-only (urllib): these containers carry no torch, no requests, and hold no GPU memory,
so the only thing that can go wrong here is the network. Reuses the request/response shapes proven by the
comfyui-run skill's run_comfyui.py, plus three things a long-running orchestrated stage needs and that
script did not:

  * `set_node_input` / `derive_sampler_graph` - patch a workflow by node id, or find the prompt / negative /
    latent / sampler nodes by following the graph instead of hardcoding ids per workflow.
  * node-selective downloads - the TRELLIS.2 workflow has ~18 output nodes and only one of them is the
    deliverable; downloading all of them would move gigabytes per job.
  * SIGTERM handling - the runner cancels a stage with killpg(SIGTERM) and SIGKILLs 10 s later. Without this,
    the local subprocess dies while the ComfyUI server keeps rendering, wedging a small card and leaving the
    runner busy.

Exit codes follow the stage convention used by the other workers: 0 success, 1 anything else (the orchestrator
reads the log, not the code).
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import worker_paths

MODEL_EXTS = (".glb", ".gltf", ".obj", ".stl", ".fbx", ".ply")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")

# Filled in by arm_cancel() once a prompt is queued, so a SIGTERM can retract it.
_active = {"server": None, "prompt_id": None}


class ComfyError(Exception):
    """A failure worth showing verbatim in the stage log: validation, execution error, or unreachable server."""


class Interrupted(Exception):
    """Raised in the main thread when the runner sent SIGTERM (i.e. the job was cancelled or timed out)."""


def log(msg):
    print(msg, flush=True)


def phase(name: str):
    """Same marker the local workers print; pipeline._last_phase() greps it to pick which fallback rungs apply."""
    print(f"[phase] {name}", flush=True)


# ----------------------------------------------------------------------------------------------- http

def _request(url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 120.0):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(server: str, path: str, timeout: float = 120.0):
    return json.loads(_request(server.rstrip("/") + path, timeout=timeout).decode("utf-8"))


def post_json(server: str, path: str, payload, timeout: float = 120.0):
    body = json.dumps(payload).encode("utf-8")
    return json.loads(_request(server.rstrip("/") + path, data=body,
                               headers={"Content-Type": "application/json"}, timeout=timeout).decode("utf-8"))


def get_bytes(server: str, path: str, timeout: float = 600.0) -> bytes:
    return _request(server.rstrip("/") + path, timeout=timeout)


def upload_name(prefix: str, path: str | Path) -> str:
    """A server-side filename that is unique per (prefix, content).

    Uploading replaces by name, so two images put into one graph must never share a name: two references that
    happen to have the same basename would otherwise leave every slot reading whichever was sent last, which is
    a wrong picture with no warning anywhere. `prefix` separates the slots of one run, the digest separates runs
    - so two jobs that run at once cannot overwrite each other's file between upload and execution - and because
    it is keyed on content, re-running the same job replaces its own file instead of accumulating copies.
    """
    p = Path(path)
    return f"{prefix}_{hashlib.sha1(p.read_bytes()).hexdigest()[:12]}{p.suffix.lower() or '.png'}"


def upload_image(server: str, path: str | Path, prefix: str | None = None,
                 timeout: float = 300.0) -> str:
    """POST /upload/image with a hand-rolled multipart body (no `requests` in this container).

    Returns the name the server stored it under. Without a `prefix` the file keeps its own name; with one it is
    stored under `upload_name(prefix, path)`, which is what any caller putting more than one image into a graph
    has to use.
    """
    path = Path(path)
    filename = upload_name(prefix, path) if prefix else path.name
    boundary = uuid.uuid4().hex
    data = path.read_bytes()
    body = b""
    for field, value in (("subfolder", ""), ("type", "input"), ("overwrite", "true")):
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
             f'filename="{filename}"\r\n').encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n" + data + f"\r\n--{boundary}--\r\n".encode()
    resp = json.loads(_request(server.rstrip("/") + "/upload/image", data=body, timeout=timeout,
                               headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}).decode("utf-8"))
    return resp["name"]


def probe(server: str) -> dict:
    """Cheap liveness + identity check. Used by --probe and by the stage preflight."""
    s = get_json(server, "/system_stats", timeout=15.0)
    sysinfo = s.get("system", {})
    devices = s.get("devices", [])
    return {"ok": True, "comfyui_version": sysinfo.get("comfyui_version"),
            "gpu": (devices[0].get("name") if devices else None),
            "vram_total_mib": (devices[0].get("vram_total", 0) // 2**20 if devices else None),
            "argv": " ".join(sysinfo.get("argv", []))[:200]}


# ----------------------------------------------------------------------------------------------- workflow graph

def load_workflow(path: str | Path) -> dict:
    """Load an API-format workflow. Fails loudly on a UI-format export, which is the usual mistake."""
    p = Path(path)
    if not p.is_file():
        raise ComfyError(f"workflow not found: {p}")
    wf = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(wf, dict):
        raise ComfyError(f"{p.name}: expected a JSON object of nodes")
    # API format is {node_id: {class_type, inputs}}; UI format has "nodes"/"links" at the top level.
    if "nodes" in wf and "links" in wf:
        raise ComfyError(f"{p.name} is a UI-format export; re-export with ComfyUI Dev Mode -> Export (API)")
    bad = [k for k, v in wf.items() if not isinstance(v, dict) or "class_type" not in v]
    if bad:
        raise ComfyError(f"{p.name}: nodes without class_type: {bad[:8]} (is this API format?)")
    return wf


def find_nodes(wf: dict, *class_types: str) -> list[str]:
    return [nid for nid, n in wf.items() if n.get("class_type") in class_types]


def set_node_input(wf: dict, node_id: str, key: str, value) -> bool:
    """Patch one input. Returns False when the node or the input does not exist, so callers can warn instead
    of silently queueing a workflow that ignores the setting."""
    node = wf.get(str(node_id))
    if node is None:
        return False
    inputs = node.setdefault("inputs", {})
    if key not in inputs:
        return False
    inputs[key] = value
    return True


def _link_target(wf: dict, node_id: str, key: str) -> str | None:
    """Resolve an input that is a link ([source_node_id, slot]) rather than a literal value."""
    v = (wf.get(node_id) or {}).get("inputs", {}).get(key)
    if isinstance(v, list) and len(v) >= 1 and isinstance(v[0], (str, int)):
        return str(v[0])
    return None


# The input keys an instruction can live on. `text` is CLIPTextEncode; `prompt` is what the instruction-editing
# nodes call it (Krea2EditGroundedEncode), and a writer that assumed `text` would silently do nothing there.
PROMPT_KEYS = ("text", "prompt")


def derive_sampler_graph(wf: dict) -> dict:
    """Find the sampler and its prompt / negative / latent nodes by following the graph.

    Done by graph traversal rather than configuration because it is unambiguous: the KSampler's `positive`
    and `negative` inputs ARE the two text encoders, whatever their node ids are. That is why the image side
    needs no node mapping in Settings while the TRELLIS.2 side does (its knobs are not reachable this way).

    With several samplers (hires fix, refiner) the last one in the chain wins: it is the one whose output
    reaches the decoder, so its latent size is what determines the final image.
    """
    samplers = find_nodes(wf, "KSampler", "KSamplerAdvanced")
    if not samplers:
        raise ComfyError("the workflow has no KSampler/KSamplerAdvanced node; the image backend cannot derive "
                         "where to write the prompt, seed and size")

    def latent_ancestry(nid: str, depth: int = 0) -> set[str]:
        """Samplers this one is downstream of, found by walking `latent_image` back through non-sampler nodes.

        A hires-fix / refiner pass feeds its latent from the base sampler (usually via a LatentUpscale), so the
        sampler with the deepest ancestry is the last one in the chain - and its latent size is the one that
        determines the final image.
        """
        if depth > 24:
            return set()
        seen: set[str] = set()
        prev = _link_target(wf, nid, "latent_image")
        while prev and prev not in seen and depth < 24:
            if prev in samplers:
                seen.add(prev)
                seen |= latent_ancestry(prev, depth + 1)
                break
            seen.add(prev)
            prev = _link_target(wf, prev, "latent_image") or _link_target(wf, prev, "samples")
            depth += 1
        return {s for s in seen if s in samplers}

    ranked = sorted(samplers, key=lambda n: (-len(latent_ancestry(n)), samplers.index(n)))
    chosen = ranked[0]
    if len(samplers) > 1:
        log(f"[comfy] workflow has {len(samplers)} samplers {samplers}; patching {chosen} (last in the chain)")

    def prompt_key(nid: str) -> str | None:
        """The input that carries this node's instruction, if it has one."""
        inputs = (wf.get(nid) or {}).get("inputs") or {}
        for key in PROMPT_KEYS:
            if key in inputs:
                return key
        return None

    def text_node(sampler: str, key: str) -> tuple[str | None, str]:
        """The node feeding one of the sampler's prompt inputs, and the input key to write into it.

        A txt2img graph ends at a CLIPTextEncode whose key is `text`; an instruction-editing graph ends at a
        node whose key is `prompt` (Krea2EditGroundedEncode). The key is therefore part of the answer rather
        than assumed - writing `text` into a node that has none would leave the workflow's own instruction in
        place and silently ignore the caller's.
        """
        nid = _link_target(wf, sampler, key)
        if nid is None:
            return None, "text"
        found = prompt_key(nid)
        if found:
            return nid, found
        # Some graphs put a concatenation or a string primitive between the encoder and the sampler.
        for upstream in ("text", "prompt", "clip", "positive", "negative", "conditioning"):
            deeper = _link_target(wf, nid, upstream)
            if deeper:
                found = prompt_key(deeper)
                if found:
                    return deeper, found
        return nid, "text"

    latent = _link_target(wf, chosen, "latent_image")
    positive, positive_key = text_node(chosen, "positive")
    negative, negative_key = text_node(chosen, "negative")
    return {"sampler": chosen, "n_samplers": len(samplers), "positive": positive, "negative": negative,
            "prompt_key": positive_key, "negative_key": negative_key,
            "latent": latent,
            "latent_kind": (wf.get(latent) or {}).get("class_type") if latent else None}


# ----------------------------------------------------------------------------------------------- queue / wait

def arm_cancel(server: str):
    """Install the SIGTERM handler and remember the server so an in-flight prompt can be retracted."""
    _active["server"] = server.rstrip("/")

    def handler(signum, frame):
        raise Interrupted(f"signal {signum}")

    # The runner starts stages with start_new_session=True and cancels with killpg, so SIGTERM reaches this
    # process directly. SIGINT too, for a manual Ctrl-C during a dry run.
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def retract(server: str | None = None, prompt_id: str | None = None):
    """Best effort: stop the current execution and drop the prompt from the queue.

    Defaults to whatever arm_cancel()/queue() recorded, so a caller unwinding after a signal does not have to
    thread the ids through. Never raises - this runs on the way out after a cancel, and a second failure
    there would mask the first.
    """
    server = server or _active["server"]
    prompt_id = prompt_id or _active["prompt_id"]
    if not server:
        return
    try:
        post_json(server, "/interrupt", {}, timeout=10.0)
        log(f"[cancel] sent /interrupt to {server}")
    except Exception as e:  # noqa: BLE001
        log(f"[cancel] /interrupt failed: {e}")
    if prompt_id:
        try:
            post_json(server, "/queue", {"delete": [prompt_id]}, timeout=10.0)
            log(f"[cancel] deleted prompt {prompt_id} from the queue")
        except Exception as e:  # noqa: BLE001
            log(f"[cancel] queue delete failed: {e}")


def format_validation_error(resp: dict) -> str:
    """ComfyUI reports bad node inputs in `node_errors`; turn that into something readable in the stage log."""
    parts = [f"validation failed: {str(resp.get('error'))[:400]}"]
    for nid, err in (resp.get("node_errors") or {}).items():
        parts.append(f"  node {nid} ({err.get('class_type')}): {str(err.get('errors'))[:600]}")
    extra = resp.get("extra_info") or {}
    if extra:
        parts.append(f"  extra: {json.dumps(extra, ensure_ascii=False)[:600]}")
    return "\n".join(parts)


def queue(server: str, wf: dict, client_id: str) -> str:
    try:
        resp = post_json(server, "/prompt", {"prompt": wf, "client_id": client_id}, timeout=120.0)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            raise ComfyError(format_validation_error(json.loads(raw))) from None
        except json.JSONDecodeError:
            raise ComfyError(f"/prompt returned HTTP {e.code}: {raw[:800]}") from None
    if resp.get("node_errors") or resp.get("error"):
        raise ComfyError(format_validation_error(resp))
    pid = resp.get("prompt_id")
    if not pid:
        raise ComfyError(f"/prompt returned no prompt_id: {json.dumps(resp)[:400]}")
    _active["prompt_id"] = pid
    return pid


def wait(server: str, prompt_id: str, timeout_s: float, poll_s: float = 5.0) -> dict:
    """Poll /history until the prompt finishes. Raises ComfyError on execution failure, Interrupted on SIGTERM."""
    server = server.rstrip("/")
    t0 = time.time()
    last = None
    while True:
        elapsed = time.time() - t0
        if elapsed > timeout_s:
            retract(server, prompt_id)
            raise ComfyError(f"timed out after {timeout_s:.0f}s waiting for prompt {prompt_id}")
        hist = get_json(server, f"/history/{prompt_id}", timeout=60.0)
        if prompt_id in hist:
            entry = hist[prompt_id]
            status = entry.get("status", {})
            state = status.get("status_str")
            log(f"[comfy] finished in {elapsed / 60:.1f} min: {state}")
            if state != "success":
                msgs = [json.dumps(m, ensure_ascii=False)[:1500] for m in status.get("messages", [])]
                detail = "\n  ".join(msgs) if msgs else "(no messages)"
                # Printed verbatim: a CUDA OOM in here is what pipeline._looks_like_oom greps the log for, so
                # translating it would silently disable the OOM retry ladder.
                raise ComfyError(f"ComfyUI execution failed ({state}):\n  {detail}")
            return entry
        q = get_json(server, "/queue", timeout=60.0)
        running = {x[1] for x in q.get("queue_running", [])}
        pending = {x[1] for x in q.get("queue_pending", [])}
        state = "running" if prompt_id in running else ("pending" if prompt_id in pending else "unknown")
        if state != last:
            log(f"[comfy] {state} ({elapsed / 60:.1f} min elapsed, {len(pending)} ahead in queue)")
            last = state
        elif int(elapsed) % 60 < poll_s:
            log(f"[comfy] still {state} ({elapsed / 60:.1f} min)")
        time.sleep(poll_s)


def run_workflow(server: str, wf: dict, timeout_s: float, client_id: str = "asset-studio", poll_s: float = 5.0) -> dict:
    """Queue one workflow and wait for it. The caller patches `wf` beforehand."""
    server = server.rstrip("/")
    arm_cancel(server)
    pid = queue(server, wf, client_id)
    log(f"[comfy] queued prompt {pid} on {server}")
    try:
        entry = wait(server, pid, timeout_s, poll_s)
    except Interrupted:
        retract(server, pid)
        log("[cancel] stage cancelled by the orchestrator; the remote prompt was retracted")
        sys.exit(143)   # 128 + SIGTERM, the conventional "killed by signal" code
    _active["prompt_id"] = None   # finished: a later SIGTERM (during download) must not retract someone else's run
    return entry


# ----------------------------------------------------------------------------------------------- artifacts

def iter_outputs(outputs: dict, only_nodes: set[str] | None = None, exts: tuple = MODEL_EXTS + IMAGE_EXTS):
    """Yield (node_id, filename, subfolder, type) for downloadable artifacts, optionally filtered by node.

    ComfyUI puts images under outputs[nid]["images"] and 3D files under outputs[nid]["result"] (a list of
    "name.ext" strings, sometimes suffixed " [temp]").
    """
    for nid, out in (outputs or {}).items():
        if only_nodes is not None and str(nid) not in only_nodes:
            continue
        for img in out.get("images", []):
            name = img.get("filename")
            if name and name.lower().endswith(exts):
                yield str(nid), name, img.get("subfolder", ""), img.get("type", "output")
        for item in out.get("result", []) or []:
            if not isinstance(item, str):
                continue
            is_temp = item.endswith(" [temp]")
            core = item[: -len(" [temp]")] if is_temp else item
            if not core.lower().endswith(exts):
                continue
            if is_temp:
                yield str(nid), core, "", "temp"
            elif "/" in core or "\\" in core:
                sub, _, name = core.replace("\\", "/").rpartition("/")
                yield str(nid), name, sub, "output"
            else:
                yield str(nid), core, "", "output"


def download(server: str, filename: str, subfolder: str, ftype: str, dest: Path) -> int:
    qs = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": ftype})
    blob = get_bytes(server, f"/view?{qs}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
    log(f"[comfy] saved {dest.name} ({len(blob) / 1e6:.1f} MB)")
    return len(blob)


def find_workflow_file(name: str) -> Path:
    """Resolve a workflow filename against the presets folder of this machine (and a couple of fallbacks)."""
    if os.path.isabs(name) or os.sep in name or "/" in name:
        p = Path(name)
        if p.is_file():
            return p
        raise ComfyError(f"workflow not found: {p}")
    roots = []
    wf = os.environ.get("STUDIO_WORKFLOW_DIR")
    if wf:
        roots.append(Path(wf))
    presets = worker_paths.presets_dir()
    if presets:
        roots += [presets / "workflows", presets]
    # running from a checkout without STUDIO_PRESETS_DIR set (e.g. a manual `python -m comfy_worker...`)
    roots += [worker_paths.repo_root() / "presets" / "workflows", Path.cwd()]
    for root in roots:
        cand = root / name
        if cand.is_file():
            return cand
    tried = ", ".join(str(r / name) for r in roots)
    raise ComfyError(f"workflow {name!r} not found (looked in: {tried})")
