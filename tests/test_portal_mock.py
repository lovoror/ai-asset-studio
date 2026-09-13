"""Mocked tests for the portal flow (no GPU): image job -> candidates -> asset job (hold/release), queue, library,
settings, zip download, candidate path confinement, patch/archive/purge, examples, SSE endpoint shape."""
import io
import json
import sys
import zipfile
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
    monkeypatch.setenv("STUDIO_WEB_DIST", str(tmp_path / "nodist"))
    for name in ("STUDIO_IMAGE_RUNNER", "STUDIO_PIXAL3D_RUNNER", "STUDIO_BLENDER_RUNNER"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for m in list(sys.modules):
        if m.startswith("studio"):
            del sys.modules[m]
    import studio.api as api
    from fastapi.testclient import TestClient

    api._store = None
    return TestClient(api.app), api, tmp_path


def _fake_completed_image_job(client, tmp, n=2):
    """Create an image job and fake the worker's outputs (candidates + output.json) as if it completed."""
    from studio.db import JobStore
    from studio.pipeline import JobRun

    r = client.post("/v1/image-jobs", json={"prompt": "a stylized cargo crate", "variations": n, "seed": 5})
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    store = JobStore()
    j = store.claim_next("w")
    run = JobRun(store, j)
    d = run.stage_dir("reference")
    (d / "candidates").mkdir()
    cands = []
    png = b"\x89PNG\r\n\x1a\n" + bytes(64)
    for i in range(n):
        (d / "candidates" / f"cand_{i:02d}.png").write_bytes(png)
        cands.append({"file": f"candidates/cand_{i:02d}.png", "seed": 5 + i, "time_s": 1.0, "metrics": {"score": 0.9 - i * 0.1, "reasons": []}})
    (d / "output.json").write_text(json.dumps({"selected": "candidates/cand_00.png", "candidates": cands,
                                               "selection": {"ranking": []}, "warnings": [], "stats": {}}))
    (d / "selection.json").write_text("{}")
    run.finish_stage("reference", {"selected": "candidates/cand_00.png", "prompt": "p", "negative_prompt": "n", "stats": {}, "run": {}}, 0)
    # artifacts as the pipeline would copy them (thumbs skipped: not real PNGs)
    art = run.art / "reference_candidates"
    art.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (art / f"cand_{i:02d}.png").write_bytes(png)
    run._package_image_job("package", 0)
    return jid


def test_image_job_then_asset_job_hold_release(env):
    client, api, tmp = env
    jid = _fake_completed_image_job(client, tmp)
    j = client.get(f"/v1/jobs/{jid}").json()
    assert j["kind"] == "image" and j["status"] == "completed"
    assert [c["file"] for c in j["candidates"]] == ["cand_00.png", "cand_01.png"]
    assert j["candidates"][0]["score"] == 0.9
    # held asset job from candidate 1
    r = client.post("/v1/asset-jobs", json={"image_job_id": jid, "candidate": "cand_01.png", "hold": True, "target_triangles": 8000})
    assert r.status_code == 200, r.text
    aid = r.json()["job_id"]
    assert r.json()["status"] == "held"
    a = client.get(f"/v1/jobs/{aid}").json()
    assert a["kind"] == "asset" and a["parent_id"] == jid and a["candidate"] == "cand_01.png"
    assert a["settings"]["optimize"]["target_triangles"] == 8000 and a["settings"]["input_mode"] == "image_job"
    q = client.get("/v1/queue").json()
    assert [i["id"] for i in q["items"] if i["status"] == "held"] == [aid]
    # the worker must not claim held jobs
    from studio.db import JobStore
    assert JobStore().claim_next("w") is None
    assert client.post(f"/v1/jobs/{aid}/release").json()["result"] == "queued"
    assert client.post(f"/v1/jobs/{aid}/hold").json()["result"] == "held"
    assert client.post(f"/v1/jobs/{aid}/release").json()["result"] == "queued"
    claimed = JobStore().claim_next("w")
    assert claimed["id"] == aid
    # children are listed on the parent
    assert [c["id"] for c in client.get(f"/v1/jobs/{jid}").json()["children"]] == [aid]


def test_asset_job_rejects_bad_candidates(env):
    client, api, tmp = env
    jid = _fake_completed_image_job(client, tmp)
    assert client.post("/v1/asset-jobs", json={"image_job_id": jid, "candidate": "cand_07.png"}).status_code == 404
    assert client.post("/v1/asset-jobs", json={"image_job_id": jid, "candidate": "../../secret.png"}).status_code == 422
    assert client.post("/v1/asset-jobs", json={"image_job_id": "20260101-000000-deadbeef", "candidate": "cand_00.png"}).status_code == 404
    # an asset job is not a valid parent
    r = client.post("/v1/jobs", json={"prompt": "a stylized cargo crate"}).json()
    assert client.post("/v1/asset-jobs", json={"image_job_id": r["job_id"], "candidate": "cand_00.png"}).status_code == 422


def test_reference_from_parent_is_path_confined(env):
    client, api, tmp = env
    from studio.db import JobStore
    from studio.pipeline import JobFailed, JobRun

    jid = _fake_completed_image_job(client, tmp)
    aid = client.post("/v1/asset-jobs", json={"image_job_id": jid, "candidate": "cand_00.png", "hold": False}).json()["job_id"]
    store = JobStore()
    j = store.claim_next("w")
    assert j["id"] == aid
    run = JobRun(store, j)
    run.stage_reference_from_parent()
    assert (run.art / "reference.png").exists()
    # tampered candidate name in the DB must not escape the parent's candidates directory
    store.update(aid, candidate="../../../../etc/passwd")
    j2 = store.get(aid)
    run2 = JobRun(store, j2)
    (run2.stages_dir / "reference" / "result.json").unlink()
    with pytest.raises(JobFailed):
        run2.stage_reference_from_parent()


def test_library_settings_examples_zip_patch_delete(env):
    client, api, tmp = env
    jid = _fake_completed_image_job(client, tmp)
    lib = client.get("/v1/library").json()["items"]
    assert lib[0]["id"] == jid and lib[0]["kind"] == "image" and lib[0]["candidate_count"] == 2
    assert client.get("/v1/library?kind=asset").json()["items"] == []
    assert client.get("/v1/library?q=crate").json()["items"][0]["id"] == jid
    assert client.get("/v1/library?q=zeppelin").json()["items"] == []
    # settings round trip + validation
    s = client.get("/v1/settings").json()
    assert s["auto_process"] is False and s["default_variations"] == 4
    assert client.put("/v1/settings", json={"auto_process": True, "default_variations": 6}).json()["default_variations"] == 6
    assert client.put("/v1/settings", json={"default_style": "nope"}).status_code == 422
    assert client.put("/v1/settings", json={"default_variations": 99}).status_code == 422
    # examples
    ex = client.get("/v1/examples").json()["items"]
    assert len(ex) >= 10 and all("prompt" in e and "style" in e for e in ex)
    # zip download excludes master.glb by default
    (Path(api.config.JOBS_DIR) / jid / "artifacts" / "master.glb").write_bytes(b"glTFfake")
    r = client.get(f"/v1/jobs/{jid}/download.zip")
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "manifest.json" in names and "master.glb" not in names
    names2 = zipfile.ZipFile(io.BytesIO(client.get(f"/v1/jobs/{jid}/download.zip?include_master=true").content)).namelist()
    assert "master.glb" in names2
    # patch / favorite / archive / purge
    p = client.patch(f"/v1/jobs/{jid}", json={"favorite": True, "title": "My crate"}).json()
    assert p["favorite"] is True and p["title"] == "My crate"
    assert client.get("/v1/library?favorite=true").json()["items"][0]["id"] == jid
    assert client.delete(f"/v1/jobs/{jid}").json()["result"] == "archived"
    assert client.get("/v1/library").json()["items"] == []
    assert client.get("/v1/library?archived=true").json()["items"][0]["id"] == jid
    assert client.delete(f"/v1/jobs/{jid}?purge=true").json()["result"] == "purged"
    assert client.get(f"/v1/jobs/{jid}").status_code == 404
    assert not (Path(api.config.JOBS_DIR) / jid).exists()


def test_stage_files_only_images_and_confined(env):
    client, api, tmp = env
    jid = _fake_completed_image_job(client, tmp)
    assert client.get(f"/v1/jobs/{jid}/stage-files/reference/candidates/cand_00.png").status_code == 200
    assert client.get(f"/v1/jobs/{jid}/stage-files/reference/log.txt").status_code == 404
    assert client.get(f"/v1/jobs/{jid}/stage-files/..%2Fartifacts%2Fmanifest.json").status_code in (400, 404)


def test_spa_not_mounted_without_dist(env):
    client, api, _ = env
    assert client.get("/").status_code == 404
    assert client.get("/health").status_code == 200


def test_spa_serves_asset_routes(tmp_path, monkeypatch):
    """A direct hit on /assets/<id> must serve the SPA (the bundle lives under /static, not /assets)."""
    dist = tmp_path / "dist"
    (dist / "static").mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>")
    (dist / "static" / "app.js").write_text("1")
    monkeypatch.setenv("STUDIO_WEB_DIST", str(dist))
    monkeypatch.setenv("STUDIO_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STUDIO_PRESETS_DIR", str(ROOT / "presets"))
    for m in list(sys.modules):
        if m.startswith("studio"):
            del sys.modules[m]
    import studio.api as api
    from fastapi.testclient import TestClient

    api._store = None
    client = TestClient(api.app)
    assert client.get("/assets/20260101-000000-deadbeef").text == "<html>spa</html>"
    assert client.get("/sessions/x").text == "<html>spa</html>"
    assert client.get("/static/app.js").text == "1"
    assert client.get("/v1/jobs/nope").status_code == 404
