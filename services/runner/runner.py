"""Stage runner: a tiny stdlib HTTP server that executes one stage subprocess at a time.

It deliberately imports no CUDA/torch code, so the long-lived container process holds no
GPU memory. Every stage runs as a child process; when the child exits, the driver releases
all of its CUDA allocations. The orchestrator (studio.worker) talks to this over the compose
network. Endpoints:

  GET  /health                -> {"ok": true, "busy": bool, "name": str}
  GET  /gpu                   -> nvidia-smi summary (used/total MiB, process list) or {"available": false}
  POST /run                   -> {"cmd": [...], "cwd": str?, "log_path": str, "env": {..}?} => {"run_id": str}
  GET  /runs/<id>             -> {"state": "running"|"exited", "returncode": int|None, ... resource stats}
  POST /runs/<id>/cancel      -> SIGTERM then SIGKILL
  POST /cancel_current        -> cancel whatever run is active (worker restart recovery)
"""
import argparse
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
RUNS = {}
CURRENT = {"run_id": None}
NAME = os.environ.get("RUNNER_NAME", "runner")


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
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) // 1024
    except Exception:  # noqa: BLE001
        return None
    return None


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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True, "busy": CURRENT["run_id"] is not None, "run_id": CURRENT["run_id"], "name": NAME})
        if self.path == "/gpu":
            return self._json(200, nvidia_smi())
        if self.path.startswith("/runs/"):
            rid = self.path.split("/")[2]
            rec = RUNS.get(rid)
            if not rec:
                return self._json(404, {"error": "unknown run"})
            return self._json(200, rec)
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/exec":  # short synchronous helper (no GPU work): returns stdout/stderr, never blocks a stage run
            cmd = body.get("cmd")
            if not isinstance(cmd, list) or not all(isinstance(c, str) for c in cmd):
                return self._json(400, {"error": "cmd must be a list of strings"})
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=float(body.get("timeout", 60)))
                return self._json(200, {"returncode": r.returncode, "stdout": r.stdout[-20000:], "stderr": r.stderr[-4000:]})
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)[:300]})
        if self.path == "/run":
            cmd = body.get("cmd")
            if not isinstance(cmd, list) or not all(isinstance(c, str) for c in cmd):
                return self._json(400, {"error": "cmd must be a list of strings"})
            log_path = body.get("log_path")
            with LOCK:
                if CURRENT["run_id"] is not None:
                    return self._json(409, {"error": "busy", "run_id": CURRENT["run_id"]})
                rid = uuid.uuid4().hex[:12]
                CURRENT["run_id"] = rid
            env = dict(os.environ)
            env.update({k: str(v) for k, v in (body.get("env") or {}).items()})
            logf = open(log_path, "ab") if log_path else subprocess.DEVNULL
            try:
                proc = subprocess.Popen(cmd, cwd=body.get("cwd") or None, env=env,
                                        stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
            except Exception as e:  # noqa: BLE001
                with LOCK:
                    CURRENT["run_id"] = None
                return self._json(500, {"error": f"spawn failed: {e}"})
            RUNS[rid] = {"run_id": rid, "state": "running", "pid": proc.pid, "returncode": None,
                         "started": time.time(), "ended": None, "peak_rss_mb": 0, "peak_gpu_used_mib": 0,
                         "cancelled": False}
            PROCS[rid] = proc
            threading.Thread(target=_monitor, args=(rid, proc), daemon=True).start()
            return self._json(200, {"run_id": rid})
        if self.path == "/cancel_current":
            rid = CURRENT["run_id"]
            proc = PROCS.get(rid)
            if rid and proc and proc.poll() is None:
                RUNS[rid]["cancelled"] = True
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                threading.Thread(target=_kill_later, args=(proc,), daemon=True).start()
                return self._json(200, {"cancelled": rid})
            return self._json(200, {"cancelled": None})
        if self.path.startswith("/runs/") and self.path.endswith("/cancel"):
            rid = self.path.split("/")[2]
            proc = PROCS.get(rid)
            if not proc:
                return self._json(404, {"error": "unknown run"})
            RUNS[rid]["cancelled"] = True
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                threading.Thread(target=_kill_later, args=(proc,), daemon=True).start()
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})


PROCS = {}


def _kill_later(proc, grace=10.0):
    t = time.time()
    while proc.poll() is None and time.time() - t < grace:
        time.sleep(0.2)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"[runner:{NAME}] listening on {a.host}:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
