"""End-to-end tests of the worker transfer protocol: no GPU, no models, but a *real* runner process.

A fake stage module stands in for image_worker/pixal3d_worker/blender. The point is everything the old shared
/jobs volume used to hide: the control plane must upload the inputs the stage needs to a worker that shares no
filesystem with it, mirror the worker's log back, collect the outputs, and classify failures from that log.

  python -m pytest -q tests/test_runner_transfer.py
"""
import importlib
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

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "services"
sys.path.insert(0, str(SERVICES / "studio"))

# Stands in for a real stage: reads its request, proves it received the uploaded inputs, writes outputs, and can
# simulate an out-of-memory crash.
FAKE_STAGE = '''\
import argparse, json, sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--request", required=True)
a = ap.parse_args()
req = json.loads(Path(a.request).read_text(encoding="utf-8"))
out = Path(req["out_dir"]); out.mkdir(parents=True, exist_ok=True)
print("[phase] start", flush=True)
if req.get("fail"):
    print("torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB", flush=True)
    sys.exit(3)
src = Path(req["image_path"])
views = sorted(p.name for p in Path(req["views_dir"]).rglob("*") if p.is_file()) if req.get("views_dir") else []
(out / "out.txt").write_text("got %s %d bytes" % (src.name, src.stat().st_size), encoding="utf-8")
(out / "output.json").write_text(json.dumps({"tag": req["tag"], "input_name": src.name, "views": views}))
print("[phase] done", flush=True)
'''


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Worker:
    def __init__(self, base, proc):
        self.base = base
        self.proc = proc

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _start_worker(tmp_path, *extra, token: str = "") -> Worker:
    pkg = tmp_path / "mods" / "fakestage"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "run.py").write_text(FAKE_STAGE, encoding="utf-8")
    port = _free_port()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path / "mods"), str(SERVICES)])
    cmd = [sys.executable, "-m", "runner.runner", "--host", "127.0.0.1", "--port", str(port),
           "--name", "fake-image", "--roles", "image", "--allow-module", "fakestage.run",
           "--work-dir", str(tmp_path / "worker")]
    if token:
        cmd += ["--token", token]
    cmd += list(extra)
    proc = subprocess.Popen(cmd, cwd=str(SERVICES), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"runner exited early with code {proc.returncode}")
        try:
            with urllib.request.urlopen(base + "/health", timeout=1) as r:
                if json.loads(r.read()).get("ok"):
                    return Worker(base, proc)
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("runner did not become healthy")


def _fresh_studio(tmp_path, base: str, monkeypatch, token: str = ""):
    """Import studio.config/runner_client against a throwaway config pointing at this worker."""
    monkeypatch.setenv("STUDIO_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("STUDIO_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STUDIO_IMAGE_RUNNER", base)
    monkeypatch.setenv("STUDIO_PIXAL3D_RUNNER", base)
    monkeypatch.setenv("STUDIO_BLENDER_RUNNER", base)
    monkeypatch.setenv("STUDIO_WORKER_TOKEN", token)
    for m in list(sys.modules):
        if m.startswith("studio"):
            del sys.modules[m]
    config = importlib.import_module("studio.config")
    client = importlib.import_module("studio.runner_client")
    return config, client


def _make_stage(tmp_path, config, *, fail=False):
    """A stage directory plus the inputs a real stage reads: an artifact and a directory produced by an earlier
    stage. Those inputs sit under the job directory but *outside* the running stage's own directory, which is
    exactly the layout the pipeline uses (stage X reads artifacts/ and stages/<other>/)."""
    job = Path(config.JOBS_DIR) / "job1"
    stage = job / "stages" / "pixal3d"
    art = job / "artifacts"
    views = job / "stages" / "reference" / "views"
    stage.mkdir(parents=True, exist_ok=True)
    art.mkdir(parents=True, exist_ok=True)
    views.mkdir(parents=True, exist_ok=True)
    (art / "reference.png").write_bytes(b"PNGDATA")
    (views / "transforms.json").write_text("{}", encoding="utf-8")
    (views / "000.png").write_bytes(b"x")
    req = {"image_path": str(art / "reference.png"), "views_dir": str(views), "out_dir": str(stage),
           "tag": "hello", "fail": fail}
    (stage / "request.json").write_text(json.dumps(req), encoding="utf-8")
    return stage, stage / "log.txt"


def test_an_input_inside_the_stage_dir_is_not_uploaded_and_that_is_a_trap(tmp_path):
    """Where a stage's *input* is allowed to live, pinned because getting it wrong fails three minutes later.

    The stage directory is what a stage produces, so the worker's own run directory starts empty: the runner
    rewrites a path inside it to `@out/...` and ships nothing, while a path inside the job directory but outside
    the stage being run becomes `@in/...` and is uploaded first. Anything a stage needs as input therefore has to
    be the second kind. Measured on a real job: reference images copied into the stage directory reached the
    worker as the literal `@out/references/ref0.png` and the stage died on
    `FileNotFoundError: '@out\\references\\ref0.png'` - a placeholder the worker's empty run directory cannot
    resolve. `pipeline._stage_references` copies them to `<job>/references/` for exactly this reason.
    """
    from studio.runner_client import StagePlan

    jobs = tmp_path / "jobs"
    job = jobs / "20260101-000000-aaaaaaaa"
    stage = job / "stages" / "reference"
    stage.mkdir(parents=True)
    (stage / "already_here.png").write_bytes(b"x")            # inside the stage dir: an output, not shipped
    elsewhere = job / "references"
    elsewhere.mkdir()
    (elsewhere / "ref0.png").write_bytes(b"y")                # inside the job dir: an input, shipped

    plan = StagePlan({"in_stage": str(stage / "already_here.png"),
                      "in_job": str(elsewhere / "ref0.png"),
                      "out_dir": str(stage)}, stage, jobs)

    assert plan.request["in_stage"] == "@out/already_here.png"
    assert plan.request["out_dir"] == "@out"
    rel = (elsewhere / "ref0.png").relative_to(jobs).as_posix()
    assert plan.request["in_job"] == f"@in/{rel}"
    assert list(plan.uploads) == [rel], "only the job-directory input is uploaded"
    assert plan.total_bytes == 1


def test_transfer_roundtrip(tmp_path, monkeypatch):
    """Inputs (a file and a directory) reach the worker; outputs and the log come back."""
    w = _start_worker(tmp_path)
    try:
        config, client = _fresh_studio(tmp_path, w.base, monkeypatch)
        stage, log_path = _make_stage(tmp_path, config)
        rec = client.Runner("image").run_stage(
            job_id="job1", stage="pixal3d", module="fakestage.run",
            request_path=stage / "request.json", stage_dir=stage, log_path=log_path)

        assert rec["returncode"] == 0
        out = json.loads((stage / "output.json").read_text(encoding="utf-8"))
        assert out["tag"] == "hello"
        # the uploaded input kept its name and arrived intact
        assert out["input_name"] == "reference.png"
        assert (stage / "out.txt").read_text(encoding="utf-8") == "got reference.png 7 bytes"
        # a directory input was uploaded recursively
        assert out["views"] == ["000.png", "transforms.json"]
        # the worker's stdout was mirrored into the local stage log (this is what OOM classification reads)
        text = log_path.read_text(encoding="utf-8")
        assert "[phase] start" in text and "[phase] done" in text
    finally:
        w.stop()


def test_oom_is_classified_from_the_remote_log(tmp_path, monkeypatch):
    w = _start_worker(tmp_path)
    try:
        config, client = _fresh_studio(tmp_path, w.base, monkeypatch)
        stage, log_path = _make_stage(tmp_path, config, fail=True)
        with pytest.raises(client.StageFailed) as e:
            client.Runner("image").run_stage(
                job_id="job1", stage="pixal3d", module="fakestage.run",
                request_path=stage / "request.json", stage_dir=stage, log_path=log_path)
        assert e.value.returncode == 3
        assert e.value.oom is True, "OOM must be detected from the mirrored log, not the local one"
    finally:
        w.stop()


def test_role_gating_refuses_other_stages(tmp_path, monkeypatch):
    """An image-only worker must not accept the Blender stage."""
    w = _start_worker(tmp_path)
    try:
        config, client = _fresh_studio(tmp_path, w.base, monkeypatch)
        stage, log_path = _make_stage(tmp_path, config)
        with pytest.raises(client.StageFailed) as e:
            client.Runner("image").run_stage(
                job_id="job1", stage="blender", module="blender.process_asset",
                request_path=stage / "request.json", stage_dir=stage, log_path=log_path)
        assert "refused" in str(e.value) or "not served" in str(e.value)
    finally:
        w.stop()


def test_token_is_required_when_configured(tmp_path, monkeypatch):
    w = _start_worker(tmp_path, token="s3cret")
    try:
        def get(path, headers=None):
            req = urllib.request.Request(w.base + path, headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code

        assert get("/health") == 200                       # liveness stays open
        assert get("/gpu") == 401                          # everything else is closed
        assert get("/gpu", {"Authorization": "Bearer wrong"}) == 401
        assert get("/gpu", {"Authorization": "Bearer s3cret"}) == 200

        # and the control-plane client works with the shared secret
        config, client = _fresh_studio(tmp_path, w.base, monkeypatch, token="s3cret")
        stage, log_path = _make_stage(tmp_path, config)
        client.Runner("image").run_stage(
            job_id="job1", stage="pixal3d", module="fakestage.run",
            request_path=stage / "request.json", stage_dir=stage, log_path=log_path)
        assert (stage / "output.json").is_file()
    finally:
        w.stop()


def test_worker_refuses_public_bind_without_token(tmp_path):
    """Binding a worker to a non-loopback address without a token must fail loudly, since the API runs code."""
    port = _free_port()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SERVICES)
    p = subprocess.run([sys.executable, "-m", "runner.runner", "--host", "0.0.0.0", "--port", str(port),
                        "--work-dir", str(tmp_path / "worker")],
                       cwd=str(SERVICES), env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode != 0
    assert "without a token" in (p.stderr + p.stdout)

