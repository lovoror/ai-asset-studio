"""Stage runner: a tiny stdlib HTTP server that executes one stage subprocess at a time.

It deliberately imports no CUDA/torch code, so the long-lived process holds no GPU memory. Every stage runs as a
child process; when the child exits, the driver releases all of its CUDA allocations.

It is self-contained: the control plane does not share a filesystem with it. For each stage the control plane
creates a run, uploads the input files, starts it, and downloads the result bundle:

  POST   /runs                       {"job_id","stage","module"}      -> {"run_id": ...}
  PUT    /runs/<id>/input/<name>     raw bytes                        -> {"bytes": n}
  POST   /runs/<id>/start            {"request": {...}}               -> {"run_id": ..., "pid": n}
  GET    /runs/<id>                  -> {"state":"created|running|exited", "returncode":.., peaks}
  GET    /runs/<id>/log?offset=N     -> text/plain (from byte offset N)
  GET    /runs/<id>/bundle           -> application/zip of the stage output directory
  POST   /runs/<id>/cancel           -> SIGTERM/SIGKILL the process tree
  DELETE /runs/<id>                  -> drop the run and its work directory
  POST   /cancel_current             -> cancel whatever run is active (control-plane restart recovery)
  POST   /exec                       -> short synchronous helper (no GPU work), for --list style probes

Inside a stage request, path values may use placeholders resolved against the run's own directory:
  @out            -> <run>/out        (what the stage produces; this is what the bundle returns)
  @in/<relpath>   -> <run>/in/<relpath>   (a file uploaded before start)
  @work           -> <run>
This is what makes a worker on another machine work without a shared mount: every path the stage needs is either
uploaded or created here.

Security: the API runs code, so it is not open. All endpoints except /health need the bearer token whenever one
is configured, and binding to a non-loopback address without a token is refused. A worker also only accepts the
modules belonging to the roles it was started with (--roles), so the 2D box cannot be asked to run Blender.
"""
import argparse
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.1.0"

# Which python modules a worker started with a given role is allowed to run. Keeping this closed means a stolen
# token cannot turn a worker into a general-purpose remote shell.
ROLE_MODULES = {
    "image": ("image_worker.generate", "comfy_worker.image_generate"),
    "pixal3d": ("pixal3d_worker.generate", "comfy_worker.three_d_generate"),
    "blender": ("blender.process_asset",),
}
ALL_MODULES = tuple(m for ms in ROLE_MODULES.values() for m in ms)

LOCK = threading.Lock()
RUNS: dict[str, dict] = {}
PROCS: dict[str, subprocess.Popen] = {}
CURRENT = {"run_id": None}

NAME = os.environ.get("RUNNER_NAME", "runner")
TOKEN = (os.environ.get("STUDIO_WORKER_TOKEN") or "").strip()
ROLES = tuple(r.strip() for r in (os.environ.get("STUDIO_WORKER_ROLES") or "").split(",") if r.strip())
# Extra modules this worker may run, on top of its roles. For custom stages and for tests; the operator opts in
# explicitly, so the default allowlist stays closed.
EXTRA_MODULES = tuple(m.strip() for m in (os.environ.get("STUDIO_WORKER_EXTRA_MODULES") or "").split(",") if m.strip())
WORK_ROOT = Path(os.environ.get("STUDIO_WORKER_WORK_DIR") or (Path.cwd() / "var" / "worker")).resolve()
PYTHON = os.environ.get("STUDIO_WORKER_PYTHON") or sys.executable
PRESETS_DIR = os.environ.get("STUDIO_PRESETS_DIR") or ""
REPO_ROOT = os.environ.get("STUDIO_ROOT") or ""

IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------------------------------- helpers

def allowed_modules() -> tuple[str, ...]:
    if not ROLES:
        return ALL_MODULES + EXTRA_MODULES
    return tuple(m for r in ROLES for m in ROLE_MODULES.get(r, ())) + EXTRA_MODULES


def safe_name(value: str, what: str) -> str:
    """A single path component, no separators, no traversal."""
    v = (value or "").strip()
    if not v or v in (".", "..") or any(c in v for c in '\\/:*?"<>|') or ".." in v.split("."):
        raise ValueError(f"invalid {what}: {value!r}")
    if len(v) > 128:
        raise ValueError(f"{what} too long")
    return v


def safe_relpath(value: str) -> str:
    """A relative path below the run directory. Rejects absolute paths, drive letters and traversal."""
    v = (value or "").replace("\\", "/").strip("/")
    if not v:
        raise ValueError("empty path")
    p = Path(v)
    if p.is_absolute() or (len(v) > 1 and v[1] == ":") or any(part in ("", ".", "..") for part in p.parts):
        raise ValueError(f"invalid path: {value!r}")
    return v


def nvidia_smi():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=uuid,name,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return {"available": False, "error": out.stderr.strip()[:200]}
        gpus = []
        for line in out.stdout.strip().splitlines():
            u, n, used, total, util = [x.strip() for x in line.split(",")]
            gpus.append({"uuid": u, "name": n, "used_mib": int(used), "total_mib": int(total), "util_pct": int(util)})
        procs = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        plist = []
        for line in procs.stdout.strip().splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 2:
                plist.append({"pid": parts[0], "name": parts[1], "used_mib": parts[2] if len(parts) > 2 else None})
        return {"available": True, "gpus": gpus, "processes": plist}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": str(e)[:200]}


def _read_rss_mb(pid):
    """Peak resident set size of the stage process. Linux only; other platforms simply report nothing."""
    if IS_WINDOWS:
        return None
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) // 1024
    except Exception:  # noqa: BLE001
        return None
    return None


def _kill_tree(proc: subprocess.Popen, grace: float = 10.0):
    """Terminate a stage and everything it spawned. A stage shells out to ffmpeg/nvcc-style helpers, so killing
    just the python process would leave the GPU busy."""
    def alive():
        return proc.poll() is None

    def hard():
        if not alive():
            return
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    if not alive():
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    t = time.time()
    while alive() and time.time() - t < grace:
        time.sleep(0.2)
    hard()


def _monitor(run_id, proc):
    rec = RUNS[run_id]
    tick = 0
    while proc.poll() is None:
        rss = _read_rss_mb(proc.pid)
        if rss and rss > rec["peak_rss_mb"]:
            rec["peak_rss_mb"] = rss
        if tick % 3 == 0:  # nvidia-smi is a subprocess: sample every 3 s, RSS every second
            g = nvidia_smi()
            if g.get("available") and g.get("gpus"):
                used = g["gpus"][0]["used_mib"]
                rec["peak_gpu_used_mib"] = max(rec["peak_gpu_used_mib"], used)
        tick += 1
        time.sleep(1.0)
    rec["returncode"] = proc.returncode
    rec["ended"] = time.time()
    rec["state"] = "exited"
    with LOCK:
        if CURRENT["run_id"] == run_id:
            CURRENT["run_id"] = None


def resolve_placeholders(obj, out_dir: Path, in_dir: Path, work_dir: Path):
    """Replace @out / @in/... / @work in every string of a stage request with this worker's paths."""
    if isinstance(obj, dict):
        return {k: resolve_placeholders(v, out_dir, in_dir, work_dir) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_placeholders(v, out_dir, in_dir, work_dir) for v in obj]
    if isinstance(obj, str):
        if obj == "@out":
            return str(out_dir)
        if obj == "@work":
            return str(work_dir)
        if obj.startswith("@in/"):
            rel = safe_relpath(obj[4:])
            return str(in_dir / rel)
        if obj == "@presets" and PRESETS_DIR:
            return PRESETS_DIR
        if obj.startswith("@presets/"):
            return str(Path(PRESETS_DIR) / safe_relpath(obj[9:]))
        if obj == "@root" and REPO_ROOT:
            return REPO_ROOT
        if obj.startswith("@root/"):
            return str(Path(REPO_ROOT) / safe_relpath(obj[6:]))
    return obj


def prune_work_root():
    """Drop scratch directories left by a previous process. They are only ever a staging area: the control plane
    keeps its own copy of every artifact, so nothing here is authoritative."""
    if not WORK_ROOT.is_dir():
        return
    for child in WORK_ROOT.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
        except OSError:
            pass


def prune_finished_runs(keep_hours: float = 24.0):
    """Delete runs that finished a while ago, and runs that were created but never started (a control plane that
    died between the two). Their outputs were already collected, and the control plane keeps its own copy."""
    now = time.time()
    for rid, rec in list(RUNS.items()):
        ended = rec.get("ended")
        stale = (rec["state"] == "exited" and ended and now - ended > keep_hours * 3600) or \
                (rec["state"] == "created" and now - rec.get("created", now) > 3600)
        if not stale or CURRENT["run_id"] == rid:
            continue
        RUNS.pop(rid, None)
        PROCS.pop(rid, None)
        shutil.rmtree(rec["dir"], ignore_errors=True)


def bundle_zip(run_dir: Path) -> Path:
    """Zip <run>/out with store-only compression (the payload is already-compressed GLB/PNG/JPEG)."""
    out_dir = run_dir / "out"
    tmp = run_dir / "bundle.zip"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        if out_dir.is_dir():
            for p in sorted(out_dir.rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(out_dir).as_posix())
    return tmp


# ---------------------------------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"asset-studio-runner/{VERSION}"

    def log_message(self, fmt, *args):  # quiet
        pass

    # -- plumbing
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code, text: str):
        body = text.encode("utf-8", "replace")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _authorised(self) -> bool:
        if not TOKEN:
            return True
        hdr = self.headers.get("Authorization") or ""
        tok = hdr[7:] if hdr.lower().startswith("bearer ") else (self.headers.get("X-Studio-Token") or "")
        import hmac
        return hmac.compare_digest(tok, TOKEN)

    def _run(self, rid: str):
        return RUNS.get(rid)

    # -- routing
    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = {}
        for kv in query.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                params[k] = v
        if path == "/health":
            return self._json(200, {"ok": True, "busy": CURRENT["run_id"] is not None, "run_id": CURRENT["run_id"],
                                    "name": NAME, "roles": list(ROLES) or sorted(ROLE_MODULES), "version": VERSION,
                                    "platform": sys.platform, "python": PYTHON})
        if not self._authorised():
            return self._json(401, {"error": "missing or invalid worker token"})
        if path == "/gpu":
            return self._json(200, nvidia_smi())
        if path.startswith("/runs/"):
            parts = path.split("/")
            rid = parts[2]
            rec = self._run(rid)
            if not rec:
                return self._json(404, {"error": "unknown run"})
            if len(parts) == 3:
                return self._json(200, {k: v for k, v in rec.items() if k != "dir"})
            if len(parts) == 4 and parts[3] == "log":
                return self._log(rec, params)
            if len(parts) == 4 and parts[3] == "bundle":
                return self._bundle(rec)
        return self._json(404, {"error": "not found"})

    def _log(self, rec, params):
        p = Path(rec["dir"]) / "log.txt"
        try:
            full = p.read_bytes() if p.is_file() else b""
        except OSError:
            full = b""
        data = full
        if "tail" in params:
            try:
                data = full[-max(0, int(params["tail"])):]
            except ValueError:
                data = full
        elif "offset" in params:
            try:
                data = full[max(0, int(params["offset"])):]
            except ValueError:
                data = full
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        # The total size, not the size of this reply: the caller appends `data` and continues from here.
        self.send_header("X-Log-Size", str(len(full)))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _bundle(self, rec):
        run_dir = Path(rec["dir"])
        try:
            tmp = bundle_zip(run_dir)
        except OSError as e:
            return self._json(500, {"error": f"cannot build bundle: {e}"})
        size = tmp.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{rec["run_id"]}.zip"')
        self.end_headers()
        try:
            with open(tmp, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_PUT(self):
        path, _, _q = self.path.partition("?")
        if not self._authorised():
            return self._json(401, {"error": "missing or invalid worker token"})
        if path.startswith("/runs/") and "/input/" in path:
            head, _, raw_name = path.partition("/input/")
            rid = head.split("/")[2]
            rec = self._run(rid)
            if not rec:
                return self._json(404, {"error": "unknown run"})
            if rec["state"] != "created":
                return self._json(409, {"error": f"run already {rec['state']}"})
            try:
                from urllib.parse import unquote
                rel = safe_relpath(unquote(raw_name))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            dest = Path(rec["dir"]) / "in" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                got = self._receive(dest)
            except OSError as e:
                return self._json(500, {"error": f"cannot store upload: {e}"})
            rec["inputs"].append(rel)
            return self._json(200, {"path": rel, "bytes": got})
        return self._json(404, {"error": "not found"})

    def _receive(self, dest: Path) -> int:
        """Stream a request body to disk. Accepts both a Content-Length body and chunked transfer encoding, so
        a client that cannot size its upload (a pipe, a generator) still works."""
        got = 0
        chunked = "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()
        with open(dest, "wb") as f:
            if not chunked:
                remaining = int(self.headers.get("Content-Length") or 0)
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    remaining -= len(chunk)
                return got
            while True:
                line = self.rfile.readline(1024).strip()
                if not line:
                    break
                size = int(line.split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline(1024)  # trailing CRLF after the last chunk
                    break
                left = size
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        return got
                    f.write(chunk)
                    got += len(chunk)
                    left -= len(chunk)
                self.rfile.readline(1024)  # CRLF after each chunk
        return got

    def do_DELETE(self):
        path, _, _q = self.path.partition("?")
        if not self._authorised():
            return self._json(401, {"error": "missing or invalid worker token"})
        if path.startswith("/runs/"):
            rid = path.split("/")[2]
            rec = RUNS.pop(rid, None)
            PROCS.pop(rid, None)
            if not rec:
                return self._json(404, {"error": "unknown run"})
            with LOCK:
                if CURRENT["run_id"] == rid:
                    CURRENT["run_id"] = None
            shutil.rmtree(rec["dir"], ignore_errors=True)
            return self._json(200, {"deleted": rid})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path, _, _q = self.path.partition("?")
        if not self._authorised():
            return self._json(401, {"error": "missing or invalid worker token"})

        if path == "/exec":  # short synchronous helper (no GPU work); never blocks a stage run
            body = self._body()
            cmd = body.get("cmd")
            if not isinstance(cmd, list) or not all(isinstance(c, str) for c in cmd):
                return self._json(400, {"error": "cmd must be a list of strings"})
            cmd = self._rewrite_cmd(cmd)
            if isinstance(cmd, str):  # error message
                return self._json(400, {"error": cmd})
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=float(body.get("timeout", 60)),
                                   cwd=body.get("cwd") or None)
                return self._json(200, {"returncode": r.returncode, "stdout": r.stdout[-20000:], "stderr": r.stderr[-4000:]})
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)[:300]})

        if path == "/runs":
            body = self._body()
            try:
                job_id = safe_name(body.get("job_id"), "job_id")
                stage = safe_name(body.get("stage"), "stage")
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            module = (body.get("module") or "").strip()
            if module not in allowed_modules():
                return self._json(403, {"error": f"module {module!r} is not served by role(s) "
                                                 f"{list(ROLES) or sorted(ROLE_MODULES)}"})
            with LOCK:
                prune_finished_runs()
                rid = uuid.uuid4().hex[:12]
                run_dir = WORK_ROOT / job_id / stage
                # A retry of the same stage reuses the directory: clear it so no output from the previous
                # attempt can be mistaken for this one's.
                shutil.rmtree(run_dir, ignore_errors=True)
                (run_dir / "in").mkdir(parents=True, exist_ok=True)
                (run_dir / "out").mkdir(parents=True, exist_ok=True)
                RUNS[rid] = {"run_id": rid, "job_id": job_id, "stage": stage, "module": module,
                             "state": "created", "pid": None, "returncode": None, "started": None, "ended": None,
                             "peak_rss_mb": 0, "peak_gpu_used_mib": 0, "cancelled": False, "inputs": [],
                             "dir": str(run_dir), "created": time.time()}
            return self._json(200, {"run_id": rid, "stage": stage, "job_id": job_id})

        if path.startswith("/runs/") and path.endswith("/start"):
            rid = path.split("/")[2]
            rec = self._run(rid)
            if not rec:
                return self._json(404, {"error": "unknown run"})
            if rec["state"] != "created":
                return self._json(409, {"error": f"run already {rec['state']}"})
            with LOCK:
                if CURRENT["run_id"] is not None:
                    return self._json(409, {"error": "busy", "run_id": CURRENT["run_id"]})
                CURRENT["run_id"] = rid
            body = self._body()
            run_dir = Path(rec["dir"])
            try:
                request = resolve_placeholders(body.get("request") or {}, run_dir / "out", run_dir / "in", run_dir)
                (run_dir / "request.json").write_text(json.dumps(request, indent=2, default=str), encoding="utf-8")
            except (ValueError, OSError, TypeError) as e:
                with LOCK:
                    CURRENT["run_id"] = None
                return self._json(400, {"error": f"bad request: {e}"})
            cmd = [PYTHON, "-m", rec["module"], "--request", str(run_dir / "request.json")]
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            env["STUDIO_RUNNER_NAME"] = NAME
            env["STUDIO_JOB_ID"] = rec["job_id"]
            logf = open(run_dir / "log.txt", "wb")
            try:
                kwargs = {"cwd": str(run_dir), "env": env, "stdout": logf, "stderr": subprocess.STDOUT}
                if IS_WINDOWS:
                    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                else:
                    kwargs["start_new_session"] = True
                proc = subprocess.Popen(cmd, **kwargs)
            except Exception as e:  # noqa: BLE001
                logf.close()
                with LOCK:
                    CURRENT["run_id"] = None
                return self._json(500, {"error": f"spawn failed: {e}"})
            rec.update({"state": "running", "pid": proc.pid, "started": time.time(), "cmd": cmd})
            PROCS[rid] = proc
            threading.Thread(target=_monitor, args=(rid, proc), daemon=True).start()
            return self._json(200, {"run_id": rid, "pid": proc.pid, "state": "running"})

        if path == "/cancel_current":
            rid = CURRENT["run_id"]
            proc = PROCS.get(rid) if rid else None
            if rid and proc and proc.poll() is None:
                RUNS[rid]["cancelled"] = True
                threading.Thread(target=_kill_tree, args=(proc,), daemon=True).start()
                return self._json(200, {"cancelled": rid})
            return self._json(200, {"cancelled": None})

        if path.startswith("/runs/") and path.endswith("/cancel"):
            rid = path.split("/")[2]
            proc = PROCS.get(rid)
            if not proc:
                return self._json(404, {"error": "unknown run"})
            RUNS[rid]["cancelled"] = True
            if proc.poll() is None:
                threading.Thread(target=_kill_tree, args=(proc,), daemon=True).start()
            return self._json(200, {"ok": True})

        return self._json(404, {"error": "not found"})

    def _rewrite_cmd(self, cmd: list[str]):
        """Map the python interpreter and the @presets/@root placeholders of an /exec command. Returns a string
        (the error) instead of a list when the command may not run here."""
        out = list(cmd)
        if out and out[0] in ("python", "python3", "py"):
            out[0] = PYTHON
        module = next((out[i + 1] for i in range(len(out) - 1) if out[i] == "-m"), None)
        if module and module not in allowed_modules():
            return f"module {module!r} is not served by this worker"
        return [resolve_placeholders(a, Path("."), Path("."), Path(".")) for a in out]


def main():
    global WORK_ROOT, TOKEN, ROLES, NAME, EXTRA_MODULES
    ap = argparse.ArgumentParser(description="asset-studio stage runner")
    ap.add_argument("--host", default=os.environ.get("STUDIO_WORKER_BIND", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("STUDIO_WORKER_PORT", "8701")))
    ap.add_argument("--name", default=NAME)
    ap.add_argument("--roles", default=",".join(ROLES),
                    help="comma-separated subset of image,pixal3d,blender this worker may run (default: all)")
    ap.add_argument("--allow-module", default=",".join(EXTRA_MODULES), dest="extra_modules",
                    help="comma-separated extra python modules this worker may run (custom stages, tests)")
    ap.add_argument("--token", default=TOKEN, help="bearer token required on every endpoint except /health")
    ap.add_argument("--work-dir", default=str(WORK_ROOT), help="scratch directory for stage runs")
    ap.add_argument("--no-prune", action="store_true", help="keep scratch directories from previous runs")
    a = ap.parse_args()

    NAME = a.name
    ROLES = tuple(r.strip() for r in (a.roles or "").split(",") if r.strip())
    EXTRA_MODULES = tuple(m.strip() for m in (a.extra_modules or "").split(",") if m.strip())
    unknown = [r for r in ROLES if r not in ROLE_MODULES]
    if unknown:
        raise SystemExit(f"unknown role(s) {unknown}; known: {', '.join(ROLE_MODULES)}")
    TOKEN = (a.token or "").strip()
    WORK_ROOT = Path(a.work_dir).expanduser().resolve()

    loopback = a.host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not TOKEN:
        raise SystemExit(
            f"refusing to listen on {a.host} without a token: this API runs stage processes.\n"
            "Set [worker] token in config.toml (same value on every machine), or bind to 127.0.0.1.")
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    if not a.no_prune:
        prune_work_root()
        WORK_ROOT.mkdir(parents=True, exist_ok=True)

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    roles = ",".join(ROLES) or "all"
    print(f"[runner:{NAME}] roles={roles} listening on {a.host}:{a.port} work={WORK_ROOT} "
          f"python={PYTHON} auth={'on' if TOKEN else 'off'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print(f"[runner:{NAME}] stopping", flush=True)


if __name__ == "__main__":
    main()
