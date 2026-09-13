"""Mocked error-path tests (no GPU, no models): input validation, auth, cancel, artifact path safety,
restart recovery (orphan requeue + stage resume), failure reporting, OOM fallback policy.

Run inside the studio image:  docker compose run --rm --no-deps api python -m pytest -q /app/tests
"""
import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "studio"))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STUDIO_PRESETS_DIR", str(ROOT / "presets"))
    monkeypatch.setenv("STUDIO_MANIFESTS_DIR", str(tmp_path / "manifests"))
    monkeypatch.setenv("STUDIO_IMAGE_RUNNER", "http://127.0.0.1:1")
    monkeypatch.setenv("STUDIO_PIXAL3D_RUNNER", "http://127.0.0.1:1")
    monkeypatch.setenv("STUDIO_BLENDER_RUNNER", "http://127.0.0.1:1")
    for m in list(sys.modules):
        if m.startswith("studio"):
            del sys.modules[m]
    import studio.config as config  # noqa: F401
    import studio.api as api
    from fastapi.testclient import TestClient

    api._store = None
    return TestClient(api.app), api, tmp_path


def test_invalid_inputs(env):
    client, api, _ = env
    r = client.post("/v1/jobs", json={"prompt": "x"})
    assert r.status_code == 422
    r = client.post("/v1/jobs", json={"prompt": "a valid prompt", "quality": "ultra"})
    assert r.status_code == 422
    r = client.post("/v1/jobs", json={"prompt": "a valid prompt", "style": "does_not_exist"})
    assert r.status_code == 422 and "unknown style" in r.text
    r = client.post("/v1/jobs", json={"prompt": "a valid prompt", "texture_size": 3000})
    assert r.status_code == 422
    r = client.post("/v1/jobs", json={"prompt": "a valid prompt", "reference_image_b64": base64.b64encode(b"notanimage").decode()})
    assert r.status_code == 422
    r = client.post("/v1/jobs", json={"prompt": "a valid prompt", "lod_fractions": [1.5]})
    assert r.status_code == 422
    r = client.post("/v1/jobs", json={"prompt": "prompt; rm -rf / && echo $(whoami)", "style": "mobile_factory"})
    assert r.status_code == 200  # prompts are data: never interpolated into a shell, only written to a JSON request file
    job = r.json()["job_id"]
    r = client.get(f"/v1/jobs/{job}")
    assert r.json()["status"] == "queued"


def test_auth_required_when_token_set(env, monkeypatch):
    client, api, _ = env
    monkeypatch.setattr(api.config, "API_TOKEN", "secret-token")
    assert client.get("/v1/jobs").status_code == 401
    assert client.get("/v1/jobs", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/v1/jobs", headers={"Authorization": "Bearer secret-token"}).status_code == 200
    assert client.get("/health").status_code == 200  # health stays open


def test_cancel_queued_and_unknown(env):
    client, api, _ = env
    job = client.post("/v1/jobs", json={"prompt": "a stylized cargo crate"}).json()["job_id"]
    r = client.post(f"/v1/jobs/{job}/cancel")
    assert r.json()["result"] == "cancelled"
    assert client.get(f"/v1/jobs/{job}").json()["status"] == "cancelled"
    assert client.post("/v1/jobs/nope/cancel").status_code == 404
    assert client.post("/v1/jobs/..%2Fetc/cancel").status_code in (400, 404, 405)


def test_retry_requeues_failed_job(env):
    client, api, _ = env
    from studio.db import JobStore

    job = client.post("/v1/jobs", json={"prompt": "a stylized cargo crate"}).json()["job_id"]
    assert client.post(f"/v1/jobs/{job}/retry").json()["result"] == "queued"  # queued jobs are left alone
    store = JobStore()
    store.claim_next("w")
    store.update(job, status="failed", stage="validate", error={"stage": "validate", "message": "x"})
    r = client.post(f"/v1/jobs/{job}/retry").json()
    assert r["result"] == "queued"
    j = client.get(f"/v1/jobs/{job}").json()
    assert j["status"] == "queued" and j["error"] in ({}, None)


def test_artifact_path_confinement(env):
    client, api, tmp = env
    job = client.post("/v1/jobs", json={"prompt": "a stylized cargo crate"}).json()["job_id"]
    art = Path(os.environ["STUDIO_JOBS_DIR"]) / job / "artifacts"
    art.mkdir(parents=True)
    (art / "asset.glb").write_bytes(b"glTF")
    (Path(os.environ["STUDIO_JOBS_DIR"]) / "secret.txt").write_text("nope")
    assert client.get(f"/v1/jobs/{job}/artifacts/asset.glb").status_code == 200
    assert client.get(f"/v1/jobs/{job}/artifacts/../secret.txt").status_code == 404
    assert client.get(f"/v1/jobs/{job}/artifacts/..%2Fsecret.txt").status_code == 404
    assert client.get(f"/v1/jobs/{job}/artifacts/../../secret.txt").status_code in (400, 404)
    names = [a["name"] for a in client.get(f"/v1/jobs/{job}/artifacts").json()["artifacts"]]
    assert names == ["asset.glb"]


def test_restart_recovery_requeues_running_jobs(env):
    client, api, _ = env
    from studio.db import JobStore

    job = client.post("/v1/jobs", json={"prompt": "a stylized cargo crate"}).json()["job_id"]
    store = JobStore()
    claimed = store.claim_next("worker-A")
    assert claimed["id"] == job and claimed["status"] == "running"
    # simulate a worker crash: a new worker starts and requeues orphans
    requeued = JobStore().requeue_orphans("worker-B")
    assert requeued == [job]
    assert store.get(job)["status"] == "queued"
    again = store.claim_next("worker-B")
    assert again["id"] == job and again["attempt"] == 2


def test_stage_resume_and_failure_reporting(env, monkeypatch):
    """A job whose reference stage already completed skips it; a failing Pixal3D stage is reported with stage + log tail."""
    client, api, tmp = env
    from studio.db import JobStore
    from studio.pipeline import JobFailed, JobRun
    from studio.runner_client import StageFailed

    job = client.post("/v1/jobs", json={"prompt": "a stylized cargo crate", "seed": 1}).json()["job_id"]
    store = JobStore()
    j = store.claim_next("w")
    run = JobRun(store, j)
    # pre-completed reference stage
    d = run.stage_dir("reference")
    (d / "cand.png").write_bytes(b"\x89PNG")
    (d / "result.json").write_text(json.dumps({"status": "ok", "selected": "cand.png", "elapsed_s": 1}))
    run.stage_reference()  # must not call the runner (which is unreachable) -> reused
    assert any("reusing" in e["message"] for e in store.events(job))

    class FakeRunner:
        def wait_gpu_free(self, *a, **k):
            return {}

        def gpu(self):  # nothing else on the GPU -> OOM goes straight to the fallback ladder
            return {"available": True, "gpus": [{"used_mib": 1000, "total_mib": 32000}], "processes": []}

        def run(self, cmd, log_path, **k):
            Path(log_path).write_text("[phase] generate\nRuntimeError: CUDA out of memory. Tried to allocate 2 GiB\n")
            raise StageFailed("stage exited with code 1", 1, oom=True)

    run.runners["pixal3d"] = FakeRunner()
    run.settings["allow_quality_fallback"] = False
    with pytest.raises(JobFailed) as ei:
        run.stage_pixal3d()
    assert ei.value.stage == "pixal3d" and "out of memory" in str(ei.value)
    assert any("allow_quality_fallback=false" in w for w in run.warnings)
    # with fallback allowed, the resolution ladder is applied and recorded
    run.settings["allow_quality_fallback"] = True
    run.settings["_fallbacks_applied"] = []
    run.settings["pixal3d"]["resolution"] = 1536
    run.settings["fallback"] = [{"stage": "pixal3d", "set": {"resolution": 1024}, "quality_loss": True}]
    with pytest.raises(JobFailed):
        run.stage_pixal3d()
    assert run.settings["pixal3d"]["resolution"] == 1024
    assert run.settings["pixal3d"]["_requested"] == {"resolution": 1536}
    assert any("retrying with {'resolution': 1024}" in w for w in run.warnings)


def test_prompt_builder_rules():
    from studio.presets import load_presets
    from studio.promptbuilder import build_reference_prompt

    style = load_presets()["styles"]["mobile_factory"]
    out = build_reference_prompt({"prompt": "Stylized water pump station", "height_m": 2.0, "palette": ["teal", "copper"]}, style)
    p = out["prompt"].lower()
    assert "exactly one object" in p and "three-quarter" in p and "no text" in p  # text is forbidden in both prompts
    assert "teal, copper" in p and "2 m" in p
    assert "watermark" in out["negative_prompt"] and "multiple objects" in out["negative_prompt"]


def test_glb_validation_rejects_webp_and_reports_counts(tmp_path):
    import numpy as np
    import trimesh
    from PIL import Image

    from studio.validate import validate_glb

    m = trimesh.creation.box(extents=[1, 2, 1])
    m.apply_translation([0, 1, 0])
    uv = np.random.rand(len(m.vertices), 2)
    tex = Image.fromarray(np.zeros((64, 64, 4), np.uint8))
    m.visual = trimesh.visual.TextureVisuals(uv=uv, material=trimesh.visual.material.PBRMaterial(baseColorTexture=tex))
    p = tmp_path / "a.glb"
    m.export(p)
    rep = validate_glb(p, {"require_uv": True, "require_basecolor": True, "height_m": 2.0, "require_bottom_origin": True, "max_triangles": 12})
    assert rep["ok"], rep["errors"]
    assert rep["triangles"] == 12 and rep["images"][0]["mimeType"] == "image/png"
    rep2 = validate_glb(p, {"max_triangles": 4, "height_m": 3.0})
    assert not rep2["ok"] and len(rep2["errors"]) == 2
    pw = tmp_path / "w.glb"
    m.export(pw, extension_webp=True)
    repw = validate_glb(pw)
    assert not repw["ok"] and any("EXT_texture_webp" in e for e in repw["errors"])
