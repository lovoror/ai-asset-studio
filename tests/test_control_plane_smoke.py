"""End-to-end smoke test of the real control plane, without a GPU.

Starts `scripts/serve.py control` (the API + orchestrator + the workers configured as local) with a **separate
worker process standing in for the 2D server**, then drives it over HTTP exactly like the portal does:

  * /health reports every configured worker
  * /capabilities answers
  * an image job submitted to /v1/image-jobs is transferred to that worker, runs there, and its outputs come back
  * the stage log the control plane mirrored from the worker is on disk (OOM classification reads it)

Everything is pointed at a scratch directory through STUDIO_CONFIG, so a developer's own config.toml is never
touched. Needs the control-plane dependencies (fastapi, uvicorn, httpx) but no GPU and no models.

  python -m pytest -q tests/test_control_plane_smoke.py
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
pytest.importorskip("httpx")

ROOT = Path(__file__).resolve().parents[1]

# A drop-in for image_worker.generate: same CLI and the same output.json contract, but no torch.
FAKE_IMAGE_WORKER = '''\
import argparse, json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", required=True)
    ap.add_argument("--list", metavar="YAML")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.list:
        print(json.dumps([{"id": "qwen-image-2512-lightning-8", "available": True, "reason": "fake"}]))
        return
    if a.selftest:
        print("selftest ok")
        return
    req = json.loads(Path(a.request).read_text(encoding="utf-8"))
    out = Path(req["out_dir"])
    cdir = out / "candidates"
    cdir.mkdir(parents=True, exist_ok=True)
    print("[phase] generate %r seeds=%s" % (req["prompt"][:60], req["seeds"]), flush=True)
    cands = []
    for i, seed in enumerate(req["seeds"]):
        name = "cand_%02d.png" % i
        (cdir / name).write_bytes(b"\\x89PNG\\r\\n\\x1a\\n" + bytes(64))
        cands.append({"file": "candidates/" + name, "seed": seed, "time_s": 1.0,
                      "metrics": {"score": 0.9 - i * 0.1, "reasons": ["fake"]}})
    (out / "selection.json").write_text(json.dumps({"selection": cands[0]["file"], "candidates": cands}),
                                        encoding="utf-8")
    (out / "output.json").write_text(json.dumps({
        "candidates": cands, "selected": cands[0]["file"],
        "selection": {"selected": cands[0]["file"]}, "stats": {"fake": True}, "warnings": []}), encoding="utf-8")
    print("[phase] done", flush=True)


if __name__ == "__main__":
    main()
'''


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url, token=None, body=None, method="GET", timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _wait(url, what, seconds=120):
    deadline, last = time.time() + seconds, None
    while time.time() < deadline:
        try:
            return _get(url, timeout=5)
        except (urllib.error.URLError, OSError, ValueError) as e:  # not up yet
            last = e
            time.sleep(0.5)
    raise AssertionError(f"{what} did not come up within {seconds}s: {last}")


def _stop(proc):
    """Stop a process and everything it started. On Windows `terminate()` is a TerminateProcess that serve.py
    cannot intercept, so the tree has to be killed explicitly."""
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_control_plane_runs_an_image_job_through_a_remote_worker(tmp_path):
    mods = tmp_path / "mods" / "image_worker"
    mods.mkdir(parents=True)
    (mods / "__init__.py").write_text("", encoding="utf-8")   # a real package, so it shadows the true one
    (mods / "generate.py").write_text(FAKE_IMAGE_WORKER, encoding="utf-8")

    api_port, worker_port = _free_port(), _free_port()
    token = "smoke-token"
    config = tmp_path / "config.toml"
    config.write_text(f"""
[control]
bind = "127.0.0.1"
port = {api_port}
api_token = "{token}"

[servers]
image = "http://127.0.0.1:{worker_port}"
pixal3d = "local"
blender = "local"

[worker]
bind = "127.0.0.1"
pixal3d_port = {_free_port()}
blender_port = {_free_port()}

[paths]
jobs = {json.dumps(str(tmp_path / "jobs"))}
data = {json.dumps(str(tmp_path / "data"))}
web_dist = {json.dumps(str(tmp_path / "nodist"))}

[stage]
max_oom_retries = 0
""", encoding="utf-8")

    env = dict(os.environ)
    env["STUDIO_CONFIG"] = str(config)
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path / "mods"), str(ROOT / "services"), str(ROOT / "services" / "studio")])
    env["PYTHONUNBUFFERED"] = "1"
    logs = tmp_path / "logs"
    logs.mkdir()
    handles, worker, control = [], None, None
    try:
        fh = open(logs / "worker.log", "wb")
        handles.append(fh)
        worker = subprocess.Popen(
            [sys.executable, "-m", "runner.runner", "--name", "image-worker", "--roles", "image",
             "--host", "127.0.0.1", "--port", str(worker_port), "--work-dir", str(tmp_path / "worker")],
            cwd=str(ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT)
        _wait(f"http://127.0.0.1:{worker_port}/health", "the fake worker", 60)

        fh2 = open(logs / "control.log", "wb")
        handles.append(fh2)
        control = subprocess.Popen([sys.executable, "scripts/serve.py", "control"],
                                   cwd=str(ROOT), env=env, stdout=fh2, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{api_port}"
        try:
            health = _wait(f"{base}/health", "the control plane", 120)
        except AssertionError:
            raise AssertionError("control plane did not start:\n" + (logs / "control.log").read_text(encoding="utf-8", errors="replace")[-3000:])

        assert health["ok"], health
        assert health["runners"]["image"]["ok"] and health["runners"]["image"]["url"].endswith(str(worker_port))
        assert health["runners"]["pixal3d"]["ok"] and health["runners"]["blender"]["ok"]

        caps = _get(f"{base}/capabilities", token=token)
        assert caps["styles"] and caps["image_models"]

        job = _get(f"{base}/v1/image-jobs", token=token, method="POST",
                   body={"prompt": "stylized copper water tank", "style": "mobile_factory", "variations": 2})
        jid = job["job_id"]
        deadline = time.time() + 120
        status = {}
        while time.time() < deadline:
            status = _get(f"{base}/v1/jobs/{jid}", token=token)
            if status["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(1)
        assert status.get("status") == "completed", status

        names = {a["name"] for a in _get(f"{base}/v1/jobs/{jid}/artifacts", token=token)["artifacts"]}
        # An image job keeps every candidate for the picker; artifacts/reference.png is only written for an
        # asset job (stage_reference(variations_only=False)).
        assert {"manifest.json", "reference_candidates/cand_00.png", "reference_candidates/cand_01.png",
                "reference_candidates/selection.json"} <= names, names

        # The worker's stdout was mirrored into the local stage log by the transfer client.
        log = tmp_path / "jobs" / jid / "stages" / "reference" / "log.txt"
        text = log.read_text(encoding="utf-8")
        assert "[phase] generate" in text and "[phase] done" in text, text

        manifest = json.loads((tmp_path / "jobs" / jid / "artifacts" / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["kind"] == "image" and manifest["job_id"] == jid
        assert len(manifest["candidates"]) == 2
        # The remote worker's own report of the run travels back with the stage result.
        assert manifest["resources"]["reference"]["returncode"] == 0
    finally:
        _stop(control)
        _stop(worker)
        for fh in handles:
            fh.close()
