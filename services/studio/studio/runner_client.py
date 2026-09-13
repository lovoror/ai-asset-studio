"""HTTP client for the stage runners (see services/runner/runner.py)."""
from __future__ import annotations

import json
import time

import httpx

from . import config


class RunnerError(RuntimeError):
    pass


class StageCancelled(RuntimeError):
    pass


class StageFailed(RuntimeError):
    def __init__(self, message, returncode=None, oom=False, run=None):
        super().__init__(message)
        self.returncode = returncode
        self.oom = oom
        self.run = run or {}


class Runner:
    def __init__(self, name: str):
        self.name = name
        self.base = config.RUNNERS[name]
        self.http = httpx.Client(base_url=self.base, timeout=30)

    def health(self) -> dict:
        try:
            r = self.http.get("/health")
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:200]}

    def gpu(self) -> dict:
        try:
            r = self.http.get("/gpu")
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            return {"available": False, "error": str(e)[:200]}

    def cancel_current(self) -> dict:
        try:
            r = self.http.post("/cancel_current")
            return r.json()
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:200]}

    def attached_run(self, run_file: str | None) -> str | None:
        """If a previous worker started this stage and the runner still has that run (running, or exited cleanly),
        return its id so the caller can wait for it instead of starting the stage again."""
        if not run_file:
            return None
        try:
            rid = json.loads(open(run_file, encoding="utf-8").read())["run_id"]
            s = self.http.get(f"/runs/{rid}").json()
        except Exception:  # noqa: BLE001
            return None
        if s.get("state") == "running" or (s.get("state") == "exited" and s.get("returncode") == 0 and not s.get("cancelled")):
            return rid
        return None

    def run(self, cmd: list[str], log_path: str, env: dict | None = None, cwd: str | None = None,
            should_cancel=None, timeout_s: int | None = None, poll_s: float = 2.0, run_file: str | None = None,
            log=None) -> dict:
        """Start a stage subprocess and block until it exits. Raises StageCancelled / StageFailed.
        `run_file` records the run id so a restarted worker re-attaches to a stage that is still running."""
        timeout_s = timeout_s or config.STAGE_TIMEOUT_S
        rid = self.attached_run(run_file)
        if rid:
            if log:
                log(f"re-attached to the {self.name} stage still running from before the worker restart (run {rid})")
        else:
            t_busy = time.time()
            while True:  # a run left over from a previous worker may still be winding down: wait instead of failing
                r = self.http.post("/run", json={"cmd": cmd, "log_path": log_path, "env": env or {}, "cwd": cwd})
                if r.status_code != 409:
                    break
                if should_cancel and should_cancel():
                    raise StageCancelled("cancelled while waiting for the runner")
                if time.time() - t_busy > 600:
                    raise StageFailed(f"runner {self.name} stayed busy with another run for 600s")
                time.sleep(3)
            r.raise_for_status()
            rid = r.json()["run_id"]
            if run_file:
                try:
                    with open(run_file, "w", encoding="utf-8") as f:
                        json.dump({"run_id": rid, "cmd": cmd, "started": time.time()}, f)
                except OSError:
                    pass
        t0 = time.time()
        while True:
            s = self.http.get(f"/runs/{rid}").json()
            if s["state"] == "exited":
                if s.get("cancelled"):
                    raise StageCancelled("stage cancelled")
                if s["returncode"] != 0:
                    oom = _looks_like_oom(log_path)
                    raise StageFailed(f"stage exited with code {s['returncode']}", s["returncode"], oom=oom, run=s)
                return s
            if should_cancel and should_cancel():
                self.http.post(f"/runs/{rid}/cancel")
                for _ in range(60):
                    time.sleep(0.5)
                    if self.http.get(f"/runs/{rid}").json()["state"] == "exited":
                        break
                raise StageCancelled("stage cancelled")
            if time.time() - t0 > timeout_s:
                self.http.post(f"/runs/{rid}/cancel")
                raise StageFailed(f"stage timed out after {timeout_s}s")
            time.sleep(poll_s)


def _looks_like_oom(log_path: str) -> bool:
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    keys = ("CUDA out of memory", "OutOfMemoryError", "CUBLAS_STATUS_ALLOC_FAILED", "cudaErrorMemoryAllocation",
            "out of memory", "Cannot allocate memory", "std::bad_alloc")
    return any(k in tail for k in keys)
