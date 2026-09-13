"""Dedicated orchestrator worker: one job at a time, one GPU execution slot, durable state, restart recovery."""
from __future__ import annotations

import os
import socket
import time
import traceback

from . import config
from .db import JobStore
from .pipeline import JobCancelled, JobFailed, JobRun


def main():
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    store = JobStore()
    requeued = store.requeue_orphans(worker_id)
    if requeued:
        print(f"[worker] requeued {len(requeued)} interrupted job(s): {requeued}", flush=True)
        # a stage subprocess started by the previous worker may still be running: stop it (it belongs to a requeued job)
        from .runner_client import Runner
        for name in ("image", "pixal3d", "blender"):
            rn = Runner(name)
            r = rn.cancel_current()
            if r.get("cancelled"):
                print(f"[worker] cancelled orphaned {name} run {r['cancelled']}", flush=True)
                for _ in range(40):  # wait for the process to exit (SIGTERM, then SIGKILL after 10 s)
                    if not rn.health().get("busy"):
                        break
                    time.sleep(0.5)
    print(f"[worker] {worker_id} ready; jobs={config.JOBS_DIR} db={config.DB_PATH}", flush=True)
    while True:
        job = store.claim_next(worker_id)
        if not job:
            time.sleep(1.0)
            continue
        if job["cancel_requested"]:
            store.update(job["id"], status="cancelled", stage="cancelled", finished_at=time.time())
            continue
        print(f"[worker] running job {job['id']} (attempt {job['attempt']})", flush=True)
        try:
            run = JobRun(store, job)
            run.run()
        except JobCancelled:
            store.update(job["id"], status="cancelled", stage="cancelled", finished_at=time.time())
            store.event(job["id"], "warn", "job cancelled")
            print(f"[worker] job {job['id']} cancelled", flush=True)
        except JobFailed as e:
            err = {"stage": e.stage, "message": str(e), "details": e.details}
            store.update(job["id"], status="failed", stage=e.stage, finished_at=time.time(), error=err)
            store.event(job["id"], "error", f"stage {e.stage} failed: {e}")
            print(f"[worker] job {job['id']} failed in {e.stage}: {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc()
            err = {"stage": "worker", "message": f"{type(e).__name__}: {e}", "details": {"traceback": tb[-4000:]}}
            store.update(job["id"], status="failed", finished_at=time.time(), error=err)
            store.event(job["id"], "error", f"worker exception: {type(e).__name__}: {e}")
            print(f"[worker] job {job['id']} crashed: {tb}", flush=True)


if __name__ == "__main__":
    main()
