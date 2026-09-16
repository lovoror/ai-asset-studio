"""HTTP client for the stage runners (see services/runner/runner.py).

A worker may be on another machine, so nothing is shared with it except HTTP. For every stage attempt this
client: creates a run on the worker, uploads every file the stage needs, starts it, mirrors the log back into the
local log file (the control plane reads that log to classify out-of-memory failures), and finally downloads the
stage's output bundle into the local stage directory.

Paths inside a stage request are rewritten on the way out. Anything the stage produces goes into the stage
directory; anything it reads lives under the job directory. Local absolute paths are therefore replaced by
placeholders that the worker resolves against its own run directory:

    <jobs>/<id>/stages/<stage>/...        -> @out/...
    <jobs>/<id>/artifacts/master.glb      -> @in/artifacts/master.glb   (uploaded first)

That is the whole trick behind running the 2D and 3D stages on separate machines without a shared mount.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

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


def _under(path: Path, root: Path) -> str | None:
    """The relative path of `path` inside `root`, or None when it is outside. Both are resolved first so a
    symlinked or non-normalised job directory still matches."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return None


class StagePlan:
    """A stage request with its paths translated to worker-side placeholders, plus the files to upload."""

    def __init__(self, request: dict, stage_dir: Path, jobs_dir: Path):
        self.stage_dir = stage_dir.resolve()
        self.jobs_dir = jobs_dir.resolve()
        self.uploads: dict[str, Path] = {}   # worker-relative name -> local file
        self.request = self._rewrite(request)
        self.request["out_dir"] = "@out"

    def _rewrite(self, obj):
        if isinstance(obj, dict):
            return {k: self._rewrite(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._rewrite(v) for v in obj]
        if not isinstance(obj, str) or not obj:
            return obj
        p = Path(obj)
        if not p.is_absolute():
            return obj
        inside_stage = _under(p, self.stage_dir)
        if inside_stage is not None:
            return "@out" if inside_stage in ("", ".") else f"@out/{inside_stage}"
        inside_job = _under(p, self.jobs_dir)
        if inside_job is None:
            # Outside the job directory: there is no way to ship it, so leave it alone and let the stage report
            # whatever it reports. (Everything the pipeline generates lives under the job directory.)
            return obj
        self._add(inside_job, p)
        return f"@in/{inside_job}"

    def _add(self, rel: str, local: Path):
        if local.is_dir():
            for f in sorted(local.rglob("*")):
                if f.is_file():
                    self.uploads[f"{rel}/{f.relative_to(local).as_posix()}"] = f
        elif local.is_file():
            self.uploads[rel] = local

    @property
    def total_bytes(self) -> int:
        try:
            return sum(p.stat().st_size for p in self.uploads.values())
        except OSError:
            return 0


class Runner:
    def __init__(self, name: str, base: str | None = None):
        self.name = name
        # Read per construction (not at import) so an address changed in the portal's Settings page is used by the
        # next job without restarting the control plane. `base` overrides it, for testing an address that has not
        # been saved yet.
        self.base = base or config.effective_runners()[name]
        headers = {}
        if config.WORKER_TOKEN:
            headers["Authorization"] = f"Bearer {config.WORKER_TOKEN}"
        self.http = httpx.Client(
            base_url=self.base,
            headers=headers,
            timeout=httpx.Timeout(config.TRANSFER_TIMEOUT_S, connect=config.CONNECT_TIMEOUT_S),
            follow_redirects=False,
        )
        self._log_offsets: dict[str, int] = {}

    # ---- probes ------------------------------------------------------------------
    def health(self, timeout: float = 5.0) -> dict:
        try:
            r = self.http.get("/health", timeout=timeout)
            r.raise_for_status()
            return {**r.json(), "url": self.base}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:200], "url": self.base}

    def gpu(self) -> dict:
        try:
            r = self.http.get("/gpu")
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            return {"available": False, "error": str(e)[:200]}

    def exec(self, cmd: list[str], timeout: float = 60.0) -> dict:
        """Short synchronous helper on the worker (no GPU work), e.g. `image_worker.generate --list`."""
        try:
            r = self.http.post("/exec", json={"cmd": cmd}, timeout=timeout)
            return r.json()
        except Exception as e:  # noqa: BLE001
            return {"returncode": -1, "stdout": "", "stderr": str(e)[:300]}

    def cancel_current(self) -> dict:
        try:
            r = self.http.post("/cancel_current")
            return r.json()
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:200]}

    def attached_run(self, run_file: str | None) -> str | None:
        """If a previous worker started this stage and the worker still has that run (running, or exited
        cleanly), return its id so the caller waits for it instead of starting the stage again."""
        if not run_file:
            return None
        try:
            rid = json.loads(Path(run_file).read_text(encoding="utf-8"))["run_id"]
            r = self.http.get(f"/runs/{rid}")
            if r.status_code == 404:
                return None
            s = r.json()
        except Exception:  # noqa: BLE001
            return None
        if s.get("state") == "running" or (s.get("state") == "exited" and s.get("returncode") == 0
                                           and not s.get("cancelled")):
            return rid
        return None

    # ---- the stage ------------------------------------------------------------------
    def run_stage(self, *, job_id: str, stage: str, module: str, request_path: Path, stage_dir: Path,
                  log_path: Path, should_cancel=None, timeout_s: int | None = None, poll_s: float = 2.0,
                  run_file: str | None = None, log=None) -> dict:
        """Run one stage on this worker and block until it exits, transferring inputs in and outputs out.

        Raises StageCancelled / StageFailed. `run_file` records the worker-side run id so a restarted control
        plane re-attaches to a stage that is still running instead of starting it again.
        """
        timeout_s = timeout_s or config.STAGE_TIMEOUT_S
        rid = self.attached_run(run_file)
        if rid:
            if log:
                log(f"re-attached to the {self.name} stage still running from before the restart (run {rid})")
        else:
            rid = self._submit(job_id, stage, module, request_path, stage_dir, log_path, should_cancel, log)
            if run_file:
                try:
                    Path(run_file).write_text(json.dumps({"run_id": rid, "module": module, "stage": stage,
                                                          "started": time.time()}), encoding="utf-8")
                except OSError:
                    pass
        t0 = time.time()
        try:
            while True:
                state = self._state(rid)
                self._mirror_log(rid, log_path)
                if state["state"] == "exited":
                    self._mirror_log(rid, log_path, final=True)
                    if state.get("cancelled"):
                        raise StageCancelled("stage cancelled")
                    if state["returncode"] != 0:
                        raise StageFailed(f"stage exited with code {state['returncode']}", state["returncode"],
                                          oom=_looks_like_oom(log_path), run=state)
                    self._fetch_bundle(rid, stage_dir)
                    # The bundle is the authority on the stage's outputs; the log already arrived via _mirror_log.
                    return state
                if should_cancel and should_cancel():
                    self._cancel(rid)
                    raise StageCancelled("stage cancelled")
                if time.time() - t0 > timeout_s:
                    self._cancel(rid)
                    raise StageFailed(f"stage timed out after {timeout_s}s")
                time.sleep(poll_s)
        except StageCancelled:
            self._cancel(rid)
            raise

    # ---- internals ------------------------------------------------------------------
    def _submit(self, job_id, stage, module, request_path, stage_dir, log_path, should_cancel, log) -> str:
        wait_from = time.time()
        while True:
            r = self.http.post("/runs", json={"job_id": job_id, "stage": stage, "module": module})
            if r.status_code == 403:
                raise StageFailed(f"worker {self.name} ({self.base}) refused module {module!r}: {r.text[:200]}")
            r.raise_for_status()
            rid = r.json()["run_id"]
            try:
                plan = StagePlan(json.loads(Path(request_path).read_text(encoding="utf-8")), stage_dir,
                                 config.JOBS_DIR)
                if log and plan.uploads:
                    log(f"{self.name}: sending {len(plan.uploads)} input file(s) "
                        f"({plan.total_bytes / 1e6:.1f} MB) to {self.base}")
                for name, local in plan.uploads.items():
                    self._upload(rid, name, local)
                r = self.http.post(f"/runs/{rid}/start", json={"request": plan.request})
            except Exception:
                self._delete(rid)
                raise
            if r.status_code == 409:
                # The worker is finishing someone else's run (a leftover from a previous control plane): wait.
                self._delete(rid)
                if should_cancel and should_cancel():
                    raise StageCancelled("cancelled while waiting for the worker")
                if time.time() - wait_from > 600:
                    raise StageFailed(f"worker {self.name} stayed busy with another run for 600s")
                time.sleep(3)
                continue
            r.raise_for_status()
            return rid

    def _upload(self, rid: str, name: str, local: Path):
        # hmac/urllib-style quoting: keep the path readable but escape anything that is not a path character.
        from urllib.parse import quote
        size = local.stat().st_size
        with open(local, "rb") as fh:
            r = self.http.put(f"/runs/{rid}/input/{quote(name)}", content=fh,
                              headers={"Content-Length": str(size)})
        if r.status_code >= 400:
            raise StageFailed(f"upload of {name} failed ({r.status_code}): {r.text[:200]}")

    def _state(self, rid: str) -> dict:
        try:
            r = self.http.get(f"/runs/{rid}")
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise StageFailed(f"lost contact with worker {self.name} ({self.base}): {e}") from e

    def _mirror_log(self, rid: str, log_path: Path, final: bool = False):
        """Append whatever the worker has written since the last call to the local stage log. The control plane
        classifies OOM failures by reading that file, and the portal tails it while the stage runs."""
        off = self._log_offsets.get(rid, 0)
        try:
            r = self.http.get(f"/runs/{rid}/log", params={"offset": off},
                              timeout=30 if not final else config.TRANSFER_TIMEOUT_S)
            if r.status_code != 200:
                return
        except httpx.HTTPError:
            return
        data = r.content
        total = int(r.headers.get("X-Log-Size") or (off + len(data)))
        if data:
            try:
                with open(log_path, "ab") as f:
                    f.write(data)
            except OSError:
                pass
        self._log_offsets[rid] = total

    def _fetch_bundle(self, rid: str, stage_dir: Path):
        tmp = Path(tempfile.mkstemp(suffix=".zip", prefix="studio-stage-")[1])
        try:
            with self.http.stream("GET", f"/runs/{rid}/bundle") as r:
                if r.status_code == 404:  # worker restarted and lost the run: nothing to collect
                    raise StageFailed(f"worker {self.name} no longer has run {rid}; its outputs are unavailable")
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
            stage_dir.mkdir(parents=True, exist_ok=True)
            root = stage_dir.resolve()
            with zipfile.ZipFile(tmp) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    target = (root / info.filename).resolve()
                    if target != root and root not in target.parents:
                        raise StageFailed(f"worker {self.name} returned an unsafe path: {info.filename!r}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _cancel(self, rid: str):
        for _ in range(60):
            try:
                self.http.post(f"/runs/{rid}/cancel", timeout=15)
                if self.http.get(f"/runs/{rid}", timeout=15).json().get("state") == "exited":
                    return
            except Exception:  # noqa: BLE001
                return
            time.sleep(0.5)

    def _delete(self, rid: str):
        try:
            self.http.delete(f"/runs/{rid}", timeout=30)
        except Exception:  # noqa: BLE001
            pass


def _looks_like_oom(log_path) -> bool:
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
