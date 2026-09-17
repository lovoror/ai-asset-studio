"""Tests for the remote-ComfyUI path: the proxy worker's workflow graph handling, and the orchestrator
settings that decide when it is used.

No GPU, no network, no ComfyUI server: the graph logic is pure dict manipulation and the HTTP calls are
monkeypatched. Run anywhere with the control-plane dependencies installed:

    python scripts/bootstrap.py --role control
    python -m pytest -q tests
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in ("services/studio", "services"):
    sys.path.insert(0, str(ROOT / p))

from comfy_worker import comfy_client as cc                      # noqa: E402
from comfy_worker import three_d_generate as td                  # noqa: E402


# ----------------------------------------------------------------------------------------------- fixtures

def txt2img() -> dict:
    """A minimal but structurally real API-format txt2img workflow."""
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "krea2_turbo.safetensors"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "OLD POSITIVE", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "OLD NEGATIVE", "clip": ["4", 1]}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 4}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": 0, "steps": 20, "cfg": 8.0, "sampler_name": "ddim", "scheduler": "normal",
                         "denoise": 1.0, "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
                         "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "cand"}},
    }


def hires_fix() -> dict:
    """txt2img plus a chained second sampler, whose latent size is the one that matters."""
    wf = txt2img()
    wf.update({
        "10": {"class_type": "LatentUpscale",
               "inputs": {"upscale_method": "bilinear", "width": 1536, "height": 1536, "crop": "center",
                          "samples": ["3", 0]}},
        "11": {"class_type": "KSampler",
               "inputs": {"seed": 0, "steps": 10, "cfg": 4.0, "sampler_name": "euler", "scheduler": "normal",
                          "denoise": 0.5, "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
                          "latent_image": ["10", 0]}},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["4", 2]}},
        "13": {"class_type": "SaveImage", "inputs": {"images": ["12", 0]}},
    })
    return wf


def trellis2() -> dict:
    """The shape of the real TRELLIS.2 image-to-model workflow: one LoadImage, a branch switch, several
    tuning primitives, an intermediate Save3D and the real Save3DAdvanced deliverable."""
    return {
        "122": {"class_type": "LoadImage", "inputs": {"image": "example.png", "upload": "image"}},
        "316": {"class_type": "PrimitiveBoolean", "inputs": {"value": True}},
        "94": {"class_type": "Trellis2UpsampleStage", "inputs": {"target_resolution": "512", "mesh": ["200", 0]}},
        "186": {"class_type": "DecimateMesh", "inputs": {"target_face_count": 100000, "mesh": ["94", 0]}},
        "288": {"class_type": "PrimitiveInt", "inputs": {"value": 4096}},
        "224": {"class_type": "BakeNormalMapFromMesh", "inputs": {"resolution": 2048, "mesh": ["186", 0]}},
        "247": {"class_type": "Save3D", "inputs": {"mesh": ["186", 0], "filename": "intermediate"}},
        "322": {"class_type": "Save3DAdvanced", "inputs": {"mesh": ["224", 0], "filename": "master", "format": "glb"}},
    }


@pytest.fixture()
def no_combo(monkeypatch):
    """Keep snap_combo off the network: report that the server could not be asked."""
    monkeypatch.setattr(td, "snap_combo", lambda *a, **k: None)


# ----------------------------------------------------------------------------------------------- graph derivation

def test_derive_sampler_graph_simple():
    g = cc.derive_sampler_graph(txt2img())
    assert g["sampler"] == "3" and g["n_samplers"] == 1
    assert g["positive"] == "6" and g["negative"] == "7"
    assert g["latent"] == "5" and g["latent_kind"] == "EmptyLatentImage"


def test_derive_sampler_graph_picks_the_last_sampler_in_a_chain():
    """A hires fix has two samplers; the second one's latent decides the output size, so it must win."""
    g = cc.derive_sampler_graph(hires_fix())
    assert g["sampler"] == "11" and g["n_samplers"] == 2
    assert g["latent"] == "10" and g["latent_kind"] == "LatentUpscale"
    assert g["positive"] == "6" and g["negative"] == "7"


def test_derive_sampler_graph_stops_at_a_node_that_takes_text():
    """A StringConcat between the encoder and the sampler is itself the right patch target: overwriting its
    `text` input replaces the link with the prompt and keeps the suffix it appends."""
    wf = txt2img()
    wf["20"] = {"class_type": "StringConcat", "inputs": {"text": ["6", 0], "suffix": ", studio light"}}
    wf["3"]["inputs"]["positive"] = ["20", 0]
    g = cc.derive_sampler_graph(wf)
    assert g["positive"] == "20"
    from comfy_worker import image_generate as ig
    ig._patch(wf, g, {"prompt": "a mug", "width": 512, "height": 512, "steps": 8}, 1, [])
    assert wf["20"]["inputs"] == {"text": "a mug", "suffix": ", studio light"}


def test_derive_sampler_graph_walks_through_a_conditioning_node():
    """A ControlNetApply has no `text` input, so the walk has to continue upstream to the real encoder."""
    wf = txt2img()
    wf["30"] = {"class_type": "ControlNetApplyAdvanced",
                "inputs": {"positive": ["6", 0], "negative": ["7", 0], "control_net": ["31", 0], "image": ["32", 0]}}
    wf["3"]["inputs"]["positive"] = ["30", 0]
    assert cc.derive_sampler_graph(wf)["positive"] == "6"


def test_derive_sampler_graph_requires_a_sampler():
    with pytest.raises(cc.ComfyError, match="no KSampler"):
        cc.derive_sampler_graph(trellis2())


def test_patch_writes_prompt_seed_and_size():
    wf = txt2img()
    g = cc.derive_sampler_graph(wf)
    warnings: list = []
    from comfy_worker import image_generate as ig
    req = {"prompt": "a ceramic mug", "negative_prompt": "text, watermark", "width": 1024, "height": 1024,
           "steps": 8, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple"}
    ig._patch(wf, g, req, 12345, warnings)
    assert wf["6"]["inputs"]["text"] == "a ceramic mug"
    assert wf["7"]["inputs"]["text"] == "text, watermark"
    assert wf["5"]["inputs"] == {"width": 1024, "height": 1024, "batch_size": 1}
    s = wf["3"]["inputs"]
    assert (s["seed"], s["steps"], s["cfg"], s["sampler_name"], s["scheduler"]) == (12345, 8, 1.0, "euler", "simple")
    assert s["denoise"] == 1.0 and s["model"] == ["4", 0]     # untouched
    assert warnings == []


def test_patch_uses_noise_seed_for_k_sampler_advanced():
    wf = txt2img()
    wf["3"]["class_type"] = "KSamplerAdvanced"
    del wf["3"]["inputs"]["seed"]
    wf["3"]["inputs"]["noise_seed"] = 0
    from comfy_worker import image_generate as ig
    ig._patch(wf, cc.derive_sampler_graph(wf), {"prompt": "p", "width": 512, "height": 512, "steps": 8}, 777, [])
    assert wf["3"]["inputs"]["noise_seed"] == 777


def test_patch_warns_instead_of_silently_ignoring():
    wf = txt2img()
    del wf["5"]                                  # the latent node disappears
    wf["3"]["inputs"]["latent_image"] = None     # and is no longer a link
    g = cc.derive_sampler_graph(wf)
    warnings: list = []
    from comfy_worker import image_generate as ig
    ig._patch(wf, g, {"prompt": "p", "width": 1024, "height": 1024, "steps": 8}, 1, warnings)
    assert any("latent_image" in w for w in warnings)


# ----------------------------------------------------------------------------------------------- node roles

def test_parse_node_ref():
    assert td.parse_node_ref("94") == ("94", None)
    assert td.parse_node_ref(94) == ("94", None)
    assert td.parse_node_ref(" 288:value ") == ("288", "value")
    assert td.parse_node_ref("288:") == ("288", None)


def test_patch_role_default_keys(no_combo):
    wf, warnings = trellis2(), []
    assert td.patch_role(wf, "http://x", "branch", "316", False, warnings)
    assert td.patch_role(wf, "http://x", "decimate", "186", 20000, warnings)
    assert td.patch_role(wf, "http://x", "texture", "288", 2048, warnings)
    assert td.patch_role(wf, "http://x", "normal", "224", 1024, warnings)
    assert wf["316"]["inputs"]["value"] is False
    assert wf["186"]["inputs"]["target_face_count"] == 20000
    assert wf["288"]["inputs"]["value"] == 2048
    assert wf["224"]["inputs"]["resolution"] == 1024
    assert warnings == []


def test_patch_role_explicit_key_wins(no_combo):
    wf, warnings = trellis2(), []
    assert td.patch_role(wf, "http://x", "texture", "288:value", 512, warnings)
    assert wf["288"]["inputs"]["value"] == 512


def test_patch_role_falls_back_to_the_only_scalar_input(no_combo):
    wf = {"77": {"class_type": "SomeCustomSizeNode", "inputs": {"pixels": 4096, "mesh": ["1", 0]}}}
    warnings: list = []
    assert td.patch_role(wf, "http://x", "texture", "77", 1024, warnings)
    assert wf["77"]["inputs"]["pixels"] == 1024 and warnings == []


def test_patch_role_reports_what_it_could_not_do(no_combo):
    warnings: list = []
    assert td.patch_role(trellis2(), "http://x", "texture", "999", 1024, warnings) is False
    assert any("not in the workflow" in w for w in warnings)
    warnings = []
    # two scalar inputs and no key that matches the role: ambiguous, so refuse rather than guess
    wf = {"77": {"class_type": "Ambiguous", "inputs": {"a": 1, "b": 2}}}
    assert td.patch_role(wf, "http://x", "texture", "77", 1024, warnings) is False
    assert any("'<node id>:<input key>'" in w for w in warnings)
    # an unconfigured role is not a failure - the workflow's own value stands
    assert td.patch_role(trellis2(), "http://x", "seed", None, 5, []) is False


def test_patch_role_snaps_a_combo_resolution(monkeypatch):
    monkeypatch.setattr(td, "snap_combo", lambda server, cls, key, want: ("1024" if want > 900 else "512"))
    wf, warnings = trellis2(), []
    assert td.patch_role(wf, "http://x", "resolution", "94", 1536, warnings)
    assert wf["94"]["inputs"]["target_resolution"] == "1024"


def test_snap_combo_picks_the_nearest_allowed_option(monkeypatch):
    seen = {}

    def fake_get_json(server, path, timeout=0):
        seen["path"] = path
        return {"Trellis2UpsampleStage": {"input": {"required": {"target_resolution": [["256", "512", "1024", "1536"]],
                                                                                          "mesh": ["MESH"]}}}}

    monkeypatch.setattr(cc, "get_json", fake_get_json)
    monkeypatch.setattr(td, "get_json", fake_get_json)
    assert td.snap_combo("http://x", "Trellis2UpsampleStage", "target_resolution", 1400) == "1536"
    assert td.snap_combo("http://x", "Trellis2UpsampleStage", "target_resolution", 1024) == "1024"
    assert seen["path"].startswith("/object_info/")
    # an unreachable server degrades to passing the value through, it does not fail the stage
    monkeypatch.setattr(td, "get_json", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert td.snap_combo("http://x", "Trellis2UpsampleStage", "target_resolution", 1024) is None


def test_pick_output_node_prefers_the_most_specific_class():
    """The real TRELLIS.2 workflow has an intermediate Save3D next to the deliverable Save3DAdvanced."""
    warnings: list = []
    assert td.pick_output_node(trellis2(), {}, warnings) == "322"
    assert warnings == []


def test_pick_output_node_explicit_override_and_ambiguity():
    warnings: list = []
    assert td.pick_output_node(trellis2(), {"output": "247"}, warnings) == "247"
    assert warnings == []
    # an override naming a node that is not there is reported, then class_type takes over
    warnings = []
    assert td.pick_output_node(trellis2(), {"output": "999"}, warnings) == "322"
    assert any("not in the workflow" in w for w in warnings)
    # two of the most specific class: refuse to guess, download everything and keep the largest
    warnings = []
    wf = trellis2()
    wf["400"] = {"class_type": "Save3DAdvanced", "inputs": {"mesh": ["224", 0]}}
    assert td.pick_output_node(wf, {}, warnings) is None
    assert any("Set the 'output' node id" in w for w in warnings)
    # no save node at all
    warnings = []
    assert td.pick_output_node({"1": {"class_type": "LoadImage", "inputs": {}}}, {}, warnings) is None
    assert any("no save node found" in w for w in warnings)


# ----------------------------------------------------------------------------------------------- http plumbing

def test_load_workflow_rejects_a_ui_export(tmp_path):
    ui = tmp_path / "ui.json"
    ui.write_text(json.dumps({"nodes": [], "links": [], "groups": []}), encoding="utf-8")
    with pytest.raises(cc.ComfyError, match="UI-format"):
        cc.load_workflow(ui)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"1": {"inputs": {}}}), encoding="utf-8")
    with pytest.raises(cc.ComfyError, match="class_type"):
        cc.load_workflow(bad)
    with pytest.raises(cc.ComfyError, match="not found"):
        cc.load_workflow(tmp_path / "nope.json")
    api = tmp_path / "api.json"
    api.write_text(json.dumps(txt2img()), encoding="utf-8")
    assert len(cc.load_workflow(api)) == len(txt2img()) == 7


def test_iter_outputs_filters_by_node_and_handles_temp_files():
    outputs = {
        "322": {"result": ["master.glb"]},
        "247": {"result": ["sub/intermediate.glb", "scratch.obj [temp]"]},
        "9": {"images": [{"filename": "cand.png", "subfolder": "", "type": "output"},
                         {"filename": "notes.txt", "subfolder": "", "type": "output"}]},
        "10": {"images": [{"filename": "preview.png", "type": "temp"}]},
    }
    got = [(n, f, s, t) for n, f, s, t in cc.iter_outputs(outputs, only_nodes={"322"})]
    assert got == [("322", "master.glb", "", "output")]
    everything = sorted((n, f) for n, f, _, _ in cc.iter_outputs(outputs))
    assert everything == [("10", "preview.png"), ("247", "intermediate.glb"), ("247", "scratch.obj"),
                          ("322", "master.glb"), ("9", "cand.png")]     # notes.txt is not a known extension
    temp = [(f, s, t) for _, f, s, t in cc.iter_outputs(outputs, only_nodes={"247"})]
    assert ("scratch.obj", "", "temp") in temp and ("intermediate.glb", "sub", "output") in temp


def test_set_node_input_reports_misses():
    wf = txt2img()
    assert cc.set_node_input(wf, "3", "steps", 8) is True
    assert cc.set_node_input(wf, "3", "nonexistent", 8) is False
    assert cc.set_node_input(wf, "999", "steps", 8) is False
    assert cc.set_node_input(wf, "5", "batch_size", 1) is True          # int coerced through as given


def test_format_validation_error_names_the_node():
    resp = {
        "error": {"type": "prompt_outputs_failed_validation"},
        "node_errors": {"94": {"class_type": "Trellis2UpsampleStage",
                               "errors": [{"type": "value_not_in_list",
                                           "details": "target_resolution: '4096' not in ['256','512','1024']"}]}},
    }
    msg = cc.format_validation_error(resp)
    assert "node 94" in msg and "Trellis2UpsampleStage" in msg and "value_not_in_list" in msg


def test_retract_survives_a_dead_server():
    """A cancel must not mask the original error with a second one from an unreachable server."""
    cc.arm_cancel("http://127.0.0.1:1")
    cc._active["prompt_id"] = "abc123"
    cc.retract()                       # no raise
    cc.retract("http://127.0.0.1:1", "abc123")
    cc._active.clear()
    cc._active.update({"server": None, "prompt_id": None})


def test_run_workflow_retracts_and_exits_143_on_sigterm(monkeypatch):
    monkeypatch.setattr(cc, "queue", lambda server, wf, client_id: "pid-1")
    monkeypatch.setattr(cc, "arm_cancel", lambda server: None)
    calls = []
    monkeypatch.setattr(cc, "retract", lambda server=None, pid=None: calls.append((server, pid)))

    def boom(*a, **k):
        raise cc.Interrupted("signal 15")

    monkeypatch.setattr(cc, "wait", boom)
    with pytest.raises(SystemExit) as ei:
        cc.run_workflow("http://x", {}, 60.0)
    assert ei.value.code == 143
    assert calls == [("http://x", "pid-1")]


# ----------------------------------------------------------------------------------------------- glb summary

def test_glb_summary_counts_a_real_asset():
    samples = sorted(ROOT.glob("samples/*/*.glb"))
    if not samples:
        pytest.skip("no sample GLBs checked out")
    s = td.glb_summary(samples[0])
    assert s["master_faces"] > 0 and s["master_vertices"] > 0 and s["glb_version"] == 2
    assert s["byte_length"] == samples[0].stat().st_size


def test_glb_summary_rejects_a_non_glb(tmp_path):
    for name, blob in [("empty", b""), ("html", b"<html>500 Internal Server Error</html>"),
                       ("obj", b"# wavefront\nv 0 0 0\n"), ("trunc", b"glTF\x02\x00\x00\x00")]:
        p = tmp_path / f"{name}.glb"
        p.write_bytes(blob)
        with pytest.raises(cc.ComfyError):
            td.glb_summary(p)


@pytest.mark.parametrize("pad", [b" ", b"\x00"], ids=["space-padded (spec)", "nul-padded (some exporters)"])
def test_glb_summary_reads_the_json_chunk_without_the_binary_one(tmp_path, pad):
    """A TRELLIS.2 master is tens of MB; only the header and JSON chunk should be touched. Both legal chunk
    paddings must parse - json.loads tolerates trailing whitespace but not a NUL."""
    doc = {"asset": {"version": "2.0", "generator": "trellis2"},
           "accessors": [{"count": 600}, {"count": 200}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 1, "TEXCOORD_0": 1}, "indices": 0}]}],
           "images": [{"mimeType": "image/png"}], "materials": [{}]}
    body = json.dumps(doc).encode()
    body += pad * ((4 - len(body) % 4) % 4)
    bin_chunk = len(body).to_bytes(4, "little") + b"BIN\x00" + b"\x00" * len(body)
    blob = b"glTF" + (2).to_bytes(4, "little") + (12 + 8 + len(body) + len(bin_chunk)).to_bytes(4, "little")
    blob += len(body).to_bytes(4, "little") + b"JSON" + body + bin_chunk
    p = tmp_path / "m.glb"
    p.write_bytes(blob)
    s = td.glb_summary(p)
    assert s["master_faces"] == 200 and s["master_vertices"] == 200 and s["has_uv"] is True
    assert s["generator"] == "trellis2" and s["images"] == 1
    assert s["byte_length"] == len(blob)


def test_glb_summary_flags_a_mesh_without_uvs(tmp_path):
    doc = {"asset": {"version": "2.0"}, "accessors": [{"count": 30}, {"count": 10}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 1}, "indices": 0}]}]}
    body = json.dumps(doc).encode()
    body += b"\x00" * ((4 - len(body) % 4) % 4)
    blob = b"glTF" + (2).to_bytes(4, "little") + (12 + 8 + len(body)).to_bytes(4, "little")
    blob += len(body).to_bytes(4, "little") + b"JSON" + body
    p = tmp_path / "nouvs.glb"
    p.write_bytes(blob)
    s = td.glb_summary(p)
    assert s["has_uv"] is False and s["master_faces"] == 10


# ----------------------------------------------------------------------------------------------- orchestrator side

@pytest.fixture()
def studio(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STUDIO_PRESETS_DIR", str(ROOT / "presets"))
    monkeypatch.setenv("STUDIO_MANIFESTS_DIR", str(tmp_path / "manifests"))
    monkeypatch.setenv("STUDIO_WEB_DIST", str(tmp_path / "nodist"))
    for lane in ("IMAGE", "PIXAL3D", "BLENDER"):
        monkeypatch.setenv(f"STUDIO_{lane}_RUNNER", "http://127.0.0.1:1")
    for m in list(sys.modules):
        if m.startswith("studio"):
            del sys.modules[m]
    import studio.api as api
    from fastapi.testclient import TestClient

    api._store = None
    return TestClient(api.app), api, tmp_path


COMFY_SETTINGS = {"image_kind": "local", "image_url": "http://192.168.1.20:8188", "image_workflow": "krea2_turbo.json",
                  "three_d_kind": "comfyui", "three_d_url": "http://192.168.1.21:8188/",
                  "three_d_workflow": "trellis2_i2m.json", "trellis2": True,
                  "nodes": {"branch": "316", "resolution": 94, "decimate": "186", "texture": "288", "normal": "224"}}


def test_resolve_settings_snapshots_the_backend(studio):
    from studio.presets import resolve_settings

    s = resolve_settings({"prompt": "a mug", "style": "mobile_factory", "quality": "balanced",
                          "model": "krea2-turbo"}, COMFY_SETTINGS)
    assert s["backend"]["three_d"]["kind"] == "comfyui"
    assert s["backend"]["three_d"]["url"] == "http://192.168.1.21:8188"      # trailing slash stripped
    # a registry family of comfyui outranks the image_kind switch: the model has no local implementation
    assert s["backend"]["image"]["kind"] == "comfyui"
    assert s["reference"]["family"] == "comfyui"
    # the registry's params override the quality preset's 1328x1328, which does not fit an 8 GB card
    assert (s["reference"]["width"], s["reference"]["height"]) == (1024, 1024)
    assert s["reference"]["steps"] == 8 and s["reference"]["cfg"] == 1.0
    # node ids are stored as strings whatever the settings table held
    assert s["backend"]["three_d"]["nodes"]["resolution"] == "94"


def test_resolve_settings_leaves_local_jobs_alone(studio):
    from studio.presets import resolve_settings

    s = resolve_settings({"prompt": "a mug", "style": "mobile_factory", "quality": "balanced",
                          "model": "qwen-image-2512-lightning-8"}, COMFY_SETTINGS)
    assert s["backend"]["image"]["kind"] == "local"
    assert (s["reference"]["width"], s["reference"]["height"]) == (1328, 1328)
    legacy = resolve_settings({"prompt": "a mug", "style": "mobile_factory", "quality": "balanced"}, None)
    assert legacy["backend"] == {"image": {"kind": "local", "url": "", "workflow": ""},
                                 "three_d": {"kind": "local", "url": "", "workflow": "",
                                             "multiview_workflow": "3d_pixal3d_multi_views.json",
                                             "trellis2": True, "nodes": {}}}


def test_resolve_settings_rejects_an_unconfigured_remote_image_lane(studio):
    from studio.presets import resolve_settings

    req = {"prompt": "a mug", "style": "mobile_factory", "quality": "balanced", "model": "krea2-turbo"}
    for missing, needle in (({"image_url": ""}, "server address"), ({"image_workflow": ""}, "workflow file")):
        with pytest.raises(ValueError, match=needle):
            resolve_settings(req, {**COMFY_SETTINGS, **missing})
    # ... but a job that copies an existing candidate never runs the image lane, so it must not be blocked
    ok = resolve_settings({**req, "image_job_id": "abc", "candidate": "cand_00.png"},
                          {**COMFY_SETTINGS, "image_url": "", "image_workflow": ""})
    assert ok["input_mode"] == "image_job"


def test_settings_patch_validates_backend_urls(studio):
    from studio.models import SettingsPatch

    assert SettingsPatch(image_url="http://gpu-box.lan:8188/").image_url == "http://gpu-box.lan:8188"
    assert SettingsPatch(image_url="  http://10.0.0.5:8188/sub/  ").image_url == "http://10.0.0.5:8188/sub"
    assert SettingsPatch(image_url="").image_url == ""
    assert SettingsPatch(three_d_url=None).three_d_url is None
    for bad in ("ftp://x/y", "http://", "not a url", "http://169.254.169.254/latest/meta-data",
                "http://metadata.google.internal/", "http://host:99999"):
        with pytest.raises(Exception):
            SettingsPatch(image_url=bad)
    with pytest.raises(Exception):
        SettingsPatch(image_workflow="../escape.json")
    with pytest.raises(Exception):
        SettingsPatch(nodes={"branch": "not-a-number"})
    assert SettingsPatch(nodes={"branch": "316"}).nodes.branch == "316"
    # an empty workflow string means "clear it"; None means "leave it alone"
    assert SettingsPatch(image_workflow="").image_workflow == ""
    assert SettingsPatch(image_workflow=None).image_workflow is None


def test_settings_patch_accepts_every_node_role():
    """The adapter reads eight roles; a role missing from BackendNodes would be dropped silently by pydantic."""
    from studio.models import BackendNodes, SettingsPatch

    roles = ("branch", "seed", "resolution", "decimate", "texture", "normal", "image", "output")
    ids = {"branch": "316", "seed": "55", "resolution": "94", "decimate": "186", "texture": "288",
           "normal": "224", "image": "122", "output": "322"}
    n = SettingsPatch(nodes=ids).nodes
    assert all(getattr(n, r) == ids[r] for r in roles)
    # the "<node id>:<input key>" override form
    n2 = SettingsPatch(nodes={"texture": "288:value", "resolution": "94:target_resolution"}).nodes
    assert n2.texture == "288:value" and n2.resolution == "94:target_resolution"
    for bad in ("316:", "abc", "316:a b", "123456789", "316:toolongkeyname" + "x" * 30):
        with pytest.raises(Exception):
            SettingsPatch(nodes={"branch": bad})
    assert set(BackendNodes.model_fields) == set(roles)


def test_patch_role_covers_every_configurable_role(no_combo):
    """Guard against the adapter and BackendNodes drifting apart."""
    wf, warnings = trellis2(), []
    wf["55"] = {"class_type": "PrimitiveSeed", "inputs": {"seed": 0}}
    roles = {"branch": ("316", False), "seed": ("55", 1234), "resolution": ("94", 1024),
             "decimate": ("186", 20000), "texture": ("288", 2048), "normal": ("224", 1024)}
    for role, (nid, value) in roles.items():
        assert td.patch_role(wf, "http://x", role, nid, value, warnings), role
        assert role in td.ROLE_KEYS, f"role {role} has no default input keys"
    assert wf["55"]["inputs"]["seed"] == 1234
    assert warnings == []


def test_put_settings_persists_the_new_keys(studio):
    """put_settings silently drops any key missing from DEFAULT_SETTINGS - this is the trap."""
    _, api, _ = studio
    from studio.db import JobStore

    store = JobStore()
    out = store.put_settings(COMFY_SETTINGS)
    assert out["image_url"] == "http://192.168.1.20:8188"
    assert out["three_d_kind"] == "comfyui" and out["trellis2"] is True
    assert out["nodes"] == {"branch": "316", "resolution": 94, "decimate": "186", "texture": "288", "normal": "224"}
    # a fresh connection sees the same thing, i.e. it really reached the table
    assert JobStore().get_settings()["three_d_workflow"] == "trellis2_i2m.json"


def test_capabilities_advertises_backend_config(studio):
    client, _, _ = studio
    f = client.get("/capabilities").json()["features"]
    assert f["backend_config"] is True and f["backend_kinds"] == ["local", "comfyui"]


def test_comfyui_models_are_listed_and_reported_available(studio):
    client, _, _ = studio
    models = {m["id"]: m for m in client.get("/capabilities").json()["image_models"]}
    assert "krea2-turbo" in models and models["krea2-turbo"]["family"] == "comfyui"
    assert models["krea2-turbo"]["params"] == {"steps": 8, "width": 1024, "height": 1024}
    from image_worker.backends import available
    import yaml
    entries = {e["id"]: e for e in yaml.safe_load((ROOT / "presets/image_models.yaml").read_text(encoding="utf-8"))}
    # a remote model cannot be checked on disk; reporting False would grey it out in the portal
    assert available(entries["krea2-turbo"]) == (True, "")
    assert available(entries["qwen-image-2512-lightning-8"])[0] in (True, False)


def test_fallback_skips_local_only_rungs_on_a_remote_lane(studio):
    _, api, _ = studio
    from studio.db import JobStore
    from studio.pipeline import JobRun

    store = JobStore()
    a = store.create({"prompt": "a mug", "style": "mobile_factory", "quality": "balanced"},
                     {"seed": 1, "style": "mobile_factory", "quality": "balanced",
                      "reference": {"width": 1024, "height": 1024, "gpu_resident_blocks": 30},
                      "pixal3d": {"resolution": 1536, "max_num_tokens": 49152},
                      "master": {"texture_size": 4096},
                      "fallback": [{"stage": "reference", "set": {"gpu_resident_blocks": 16}, "local_only": True},
                                   {"stage": "reference", "set": {"width": 768, "height": 768}, "quality_loss": True},
                                   {"stage": "pixal3d", "set": {"max_num_tokens": 32768}, "local_only": True},
                                   {"stage": "pixal3d", "set": {"resolution": 1024}, "quality_loss": True},
                                   {"stage": "master", "set": {"texture_size": 2048}, "quality_loss": True}],
                      "allow_quality_fallback": True,
                      "backend": {"image": {"kind": "comfyui", "url": "http://x", "workflow": "w.json"},
                                  "three_d": {"kind": "local"}}},
                     kind="asset")
    run = JobRun(store, store.get(a))
    log = run.stage_dir("reference") / "log.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("[phase] generate\nCUDA out of memory\n", encoding="utf-8")

    # image lane is remote: the gpu_resident_blocks rung is dropped, the size rung still applies
    assert run.remote("image") and not run.remote("three_d")
    assert run._fallback("reference", log) is True
    assert run.settings["reference"]["width"] == 768
    assert run.settings["reference"].get("gpu_resident_blocks") == 30       # never touched
    assert any("local backend" in w for w in run.warnings)
    assert run.settings["_fallbacks_applied"] == [0, 1]                   # rung 0 marked used, warned once

    # 3D lane is local: its local_only rung still applies
    assert run._fallback("pixal3d", log) is True
    assert run.settings["pixal3d"]["max_num_tokens"] == 32768
    # the master rung is not local_only, so a remote 3D lane would honour it too
    assert run._fallback("master", log) is True and run.settings["master"]["texture_size"] == 2048
    assert run._fallback("master", log) is False                          # ladder exhausted


def test_both_lanes_remote_exhausts_the_ladder_without_a_rerun(studio):
    from studio.db import JobStore
    from studio.pipeline import JobRun

    store = JobStore()
    a = store.create({"prompt": "a mug", "style": "mobile_factory", "quality": "balanced"},
                     {"seed": 1, "style": "mobile_factory", "quality": "balanced",
                      "reference": {"width": 1024, "height": 1024}, "pixal3d": {"resolution": 1536},
                      "master": {}, "fallback": [{"stage": "pixal3d", "set": {"max_num_tokens": 32768},
                                                  "local_only": True}],
                      "allow_quality_fallback": True,
                      "backend": {"image": {"kind": "comfyui", "url": "http://x", "workflow": "w.json"},
                                  "three_d": {"kind": "comfyui", "url": "http://y", "workflow": "w.json"}}},
                     kind="asset")
    run = JobRun(store, store.get(a))
    log = run.stage_dir("pixal3d") / "log.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    assert run._fallback("pixal3d", log) is False
    assert run.settings["pixal3d"] == {"resolution": 1536}


# ------------------------------------------------------------------- the turnaround (2D multi-angle) path

def edit_workflow() -> dict:
    """A minimal instruction-editing graph: the instruction lives on `prompt`, and it is grounded on the same
    LoadImage three times over (both encoders and the model patch) - the shape the shipped turnaround has."""
    return {
        "3": {"class_type": "UNETLoader", "inputs": {"unet_name": "krea2_turbo_nvfp4.safetensors",
                                                     "weight_dtype": "default"}},
        "4": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_4b_fp8_scaled.safetensors",
                                                     "type": "krea2", "device": "cpu"}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
        "10": {"class_type": "LoadImage", "inputs": {"image": "reference.png"}},
        "12": {"class_type": "Krea2EditGroundedEncode",
               "inputs": {"clip": ["4", 0], "prompt": "OLD INSTRUCTION", "image": ["10", 0], "grounding_px": 0}},
        "13": {"class_type": "Krea2EditGroundedEncode",
               "inputs": {"clip": ["4", 0], "prompt": "", "image": ["10", 0], "grounding_px": 768}},
        "14": {"class_type": "VAEEncode", "inputs": {"pixels": ["10", 0], "vae": ["5", 0]}},
        "15": {"class_type": "Krea2EditModelPatch",
               "inputs": {"model": ["3", 0], "source_latent": ["14", 0], "source_image": ["10", 0],
                          "vae": ["5", 0]}},
        "16": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 2048, "height": 512, "batch_size": 1}},
        "17": {"class_type": "KSampler",
               "inputs": {"seed": 0, "steps": 10, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
                          "denoise": 1.0, "model": ["15", 0], "positive": ["12", 0], "negative": ["13", 0],
                          "latent_image": ["16", 0]}},
        "18": {"class_type": "VAEDecode", "inputs": {"samples": ["17", 0], "vae": ["5", 0]}},
        "19": {"class_type": "SaveImage", "inputs": {"images": ["18", 0], "filename_prefix": "studio_turnaround"}},
    }


def test_derive_sampler_graph_reports_the_key_an_instruction_lives_on():
    """Assuming `text` on an editing graph would leave the workflow's own instruction in place and silently
    ignore the caller's, so the key is part of the answer."""
    g = cc.derive_sampler_graph(edit_workflow())
    assert g["positive"] == "12" and g["prompt_key"] == "prompt"
    assert g["negative"] == "13" and g["negative_key"] == "prompt"


def test_a_plain_txt2img_workflow_still_reports_text():
    g = cc.derive_sampler_graph(txt2img())
    assert g["prompt_key"] == "text" and g["negative_key"] == "text"


def test_patch_writes_the_instruction_where_the_workflow_keeps_it():
    wf = edit_workflow()
    from comfy_worker import image_generate as ig
    ig._patch(wf, cc.derive_sampler_graph(wf),
              {"prompt": "draw a turnaround", "width": 2048, "height": 512, "steps": 8}, 5, [])
    assert wf["12"]["inputs"]["prompt"] == "draw a turnaround"
    assert wf["13"]["inputs"]["prompt"] == ""          # the negative stays empty, as the node pack trains it
    assert wf["17"]["inputs"]["seed"] == 5 and wf["17"]["inputs"]["steps"] == 8
    assert (wf["16"]["inputs"]["width"], wf["16"]["inputs"]["height"]) == (2048, 512)


def test_patch_everywhere_reaches_both_encoders_and_warns_when_there_is_nothing_to_patch():
    from comfy_worker import image_generate as ig
    wf = edit_workflow()
    assert ig._patch_everywhere(wf, "grounding_px", 512, [], "the grounding resolution") == 2
    assert wf["12"]["inputs"]["grounding_px"] == 512 and wf["13"]["inputs"]["grounding_px"] == 512

    warnings: list = []
    assert ig._patch_everywhere(wf, "no_such_input", 1, warnings, "the thing") == 0
    assert any("no_such_input" in w for w in warnings)


# ---------------------------------------------------------------------- several references into one graph

def multi_ref_workflow() -> dict:
    """The shape of the shipped `krea2_edit_refs.json`: two reference slots, each resized, both feeding the
    named `image`/`image_b` (or `source_image`/`source_image_b`) inputs of the three nodes that need them."""
    return {
        "3": {"class_type": "UNETLoader", "inputs": {"unet_name": "krea2_turbo_nvfp4.safetensors"}},
        "4": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_4b.safetensors", "type": "krea2"}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
        "10": {"class_type": "LoadImage", "_meta": {"title": "REFERENCE1"}, "inputs": {"image": "reference1.png"}},
        "11": {"class_type": "ImageResizeKJv2", "inputs": {"image": ["10", 0], "width": 1024, "height": 1024}},
        "30": {"class_type": "LoadImage", "_meta": {"title": "REFERENCE2"}, "inputs": {"image": "reference2.png"}},
        "31": {"class_type": "ImageResizeKJv2", "inputs": {"image": ["30", 0], "width": 1024, "height": 1024}},
        "12": {"class_type": "Krea2EditGroundedEncode",
               "inputs": {"clip": ["4", 0], "prompt": "OLD", "image": ["11", 0], "image_b": ["31", 0],
                          "grounding_px": 768}},
        "13": {"class_type": "Krea2EditGroundedEncode",
               "inputs": {"clip": ["4", 0], "prompt": "", "image": ["11", 0], "image_b": ["31", 0],
                          "grounding_px": 768}},
        "14": {"class_type": "VAEEncode", "inputs": {"pixels": ["11", 0], "vae": ["5", 0]}},
        "15": {"class_type": "Krea2EditModelPatch",
               "inputs": {"model": ["3", 0], "source_image": ["11", 0], "source_image_b": ["31", 0],
                          "source_latent": ["14", 0], "vae": ["5", 0], "fit_mode": "fit"}},
        "16": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "17": {"class_type": "KSampler",
               "inputs": {"seed": 0, "steps": 8, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
                          "denoise": 1.0, "model": ["15", 0], "positive": ["12", 0], "negative": ["13", 0],
                          "latent_image": ["16", 0]}},
        "18": {"class_type": "VAEDecode", "inputs": {"samples": ["17", 0], "vae": ["5", 0]}},
        "19": {"class_type": "SaveImage", "inputs": {"images": ["18", 0], "filename_prefix": "cand"}},
    }


def _reachable_from_output(wf: dict, out: str = "19") -> set:
    """Every node ComfyUI can reach walking back from an output node.

    This is the set validation and execution are limited to (execution.py:1128 collects the OUTPUT_NODEs and
    validates what it can reach from them), so "unreachable" is the same statement as "never looked at".
    """
    seen: set = set()
    stack = [out]
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        for val in (wf.get(nid, {}).get("inputs") or {}).values():
            if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str) and val[0] in wf:
                stack.append(val[0])
    return seen


def test_reference_slots_prefer_titles_and_fall_back_to_node_order():
    from comfy_worker import image_generate as ig
    wf = multi_ref_workflow()
    assert ig.reference_slots(wf) == ["10", "30"]

    for nid, node in wf.items():                      # untitled: numerically, so 10 comes before 30
        node.pop("_meta", None)
    assert ig.reference_slots(wf) == ["10", "30"]


def test_reference_slots_are_ordered_by_title_not_by_node_id():
    """A slot's number is its title, so re-adding a node with a lower id cannot silently swap the references."""
    from comfy_worker import image_generate as ig
    wf = multi_ref_workflow()
    wf["30"]["_meta"]["title"] = "REFERENCE1"
    wf["10"]["_meta"]["title"] = "REFERENCE2"
    assert ig.reference_slots(wf) == ["30", "10"]


def test_two_references_are_uploaded_in_order_into_the_two_slots(monkeypatch, tmp_path):
    from comfy_worker import image_generate as ig
    wf = multi_ref_workflow()
    uploaded: list = []

    def fake_upload(server, path, prefix=None):
        uploaded.append((Path(path).name, prefix))
        return f"{prefix}_{Path(path).name}"

    monkeypatch.setattr(ig, "upload_image", fake_upload)

    warnings: list = []
    used = ig.patch_reference_images(wf, "http://s", [tmp_path / "subject.png", tmp_path / "style.png"],
                                    warnings)

    # the prefix is what keeps the two uploads from replacing each other: both files could share a basename
    assert uploaded == [("subject.png", "reference1"), ("style.png", "reference2")]
    assert used == ["10", "30"]
    assert wf["10"]["inputs"]["image"] == "reference1_subject.png"
    assert wf["30"]["inputs"]["image"] == "reference2_style.png"
    assert wf["12"]["inputs"]["image_b"] == ["31", 0]          # both inputs stay wired
    assert wf["15"]["inputs"]["source_image_b"] == ["31", 0]
    assert warnings == []


def test_two_references_with_the_same_basename_do_not_reach_the_same_file(tmp_path):
    """Measured: two references called `reference.png` were uploaded and the second replaced the first, so both
    slots read the same picture and the result was an edit of one input with no warning anywhere.

    The upload replaces by name (`overwrite=true`, which is what keeps a re-run from piling up copies), so the
    name has to carry something about the content as well as the slot.
    """
    a = tmp_path / "reference.png"
    b = tmp_path / "other" / "reference.png"
    b.parent.mkdir()
    a.write_bytes(b"first picture")
    b.write_bytes(b"second picture")

    assert cc.upload_name("reference1", a) != cc.upload_name("reference1", b)
    assert cc.upload_name("reference1", a) != cc.upload_name("reference2", a)
    assert cc.upload_name("reference1", a) == cc.upload_name("reference1", a)     # a re-run replaces itself
    assert cc.upload_name("reference1", a).startswith("reference1_")
    assert cc.upload_name("reference1", a).endswith(".png")


def test_the_spare_slot_is_disconnected_when_only_one_reference_is_given(monkeypatch, tmp_path):
    """The node pack's README: "leave the b-inputs unconnected for single-image use".

    So the spare slot's inputs are removed rather than pointed at the workflow's own placeholder filename -
    which would otherwise be an upload that never happened.
    """
    from comfy_worker import image_generate as ig
    wf = multi_ref_workflow()
    monkeypatch.setattr(ig, "upload_image", lambda server, path, prefix=None: "remote_subject.png")

    warnings: list = []
    cut = ig.disconnect_slots(wf, ["30"], warnings)

    # the slot's own resize goes too, so nothing hangs off the loader any more
    assert cut == ["31.image", "12.image_b", "13.image_b", "15.source_image_b"]
    assert "image_b" not in wf["12"]["inputs"] and "image_b" not in wf["13"]["inputs"]
    assert "source_image_b" not in wf["15"]["inputs"]
    # the walk stops at the real graph: the first-render wiring is untouched
    assert wf["12"]["inputs"]["image"] == ["11", 0] and wf["12"]["inputs"]["clip"] == ["4", 0]
    assert wf["15"]["inputs"]["source_image"] == ["11", 0]
    assert wf["15"]["inputs"]["source_latent"] == ["14", 0]
    assert wf["11"]["inputs"]["image"] == ["10", 0]
    assert any(w.startswith("1 reference slot(s) had no reference") for w in warnings)

    # what makes removing the inputs enough: the branch is no longer reachable, so ComfyUI never validates
    # the LoadImage filename that was never uploaded
    reach = _reachable_from_output(wf)
    assert "30" not in reach and "31" not in reach
    assert {"10", "11", "12", "13", "14", "15", "17", "19"} <= reach


def test_a_used_slot_is_never_disconnected(monkeypatch, tmp_path):
    from comfy_worker import image_generate as ig
    wf = multi_ref_workflow()
    warnings: list = []
    assert ig.disconnect_slots(wf, [], warnings) == []
    assert warnings == []
    assert wf["12"]["inputs"]["image_b"] == ["31", 0]


def test_more_references_than_slots_warns_and_keeps_the_first(monkeypatch, tmp_path):
    from comfy_worker import image_generate as ig
    wf = multi_ref_workflow()
    monkeypatch.setattr(ig, "upload_image", lambda server, path, prefix=None: f"{prefix}.png")
    warnings: list = []
    used = ig.patch_reference_images(wf, "http://s",
                                     [tmp_path / f"r{i}.png" for i in range(3)], warnings)
    assert used == ["10", "30"] and wf["30"]["inputs"]["image"] == "reference2.png"
    assert any("the extra reference(s) were ignored" in w for w in warnings)


def test_a_workflow_with_no_loadimage_cannot_take_a_reference(monkeypatch, tmp_path):
    from comfy_worker import image_generate as ig
    wf = multi_ref_workflow()
    del wf["10"], wf["30"]
    monkeypatch.setattr(ig, "upload_image", lambda server, path, prefix=None: "x.png")
    with pytest.raises(cc.ComfyError, match="no LoadImage"):
        ig.patch_reference_images(wf, "http://s", [tmp_path / "r.png"], [])


def test_candidate_generation_uploads_the_references_once_for_every_seed(monkeypatch, tmp_path):
    """One upload per reference for the whole job, not one per seed: every candidate is the same request with a
    different seed, so they all read the same references."""
    import json as _json

    from comfy_worker import image_generate as ig
    wf = multi_ref_workflow()
    monkeypatch.setattr(ig, "load_workflow", lambda p: wf)
    monkeypatch.setattr(ig, "find_workflow_file", lambda name: Path(name))
    monkeypatch.setattr(ig, "probe", lambda server: {"comfyui_version": "test"})
    uploaded: list = []

    def fake_upload(server, path, prefix=None):
        uploaded.append(prefix)
        return f"{prefix}_{Path(path).name}"

    monkeypatch.setattr(ig, "upload_image", fake_upload)
    seen: list = []

    def fake_run(server, workflow, timeout, client_id=None):
        seen.append(_json.loads(_json.dumps(workflow)))
        return {"outputs": {"19": {"images": [{"filename": "c.png", "subfolder": "", "type": "output"}]}},
                "prompt": [1, "client", workflow]}

    monkeypatch.setattr(ig, "run_workflow", fake_run)
    monkeypatch.setattr(ig, "download", lambda server, name, sub, t, dest: dest.write_bytes(b"png") or 3)
    monkeypatch.setattr(ig, "analyze_candidate",
                        lambda f: {"score": 1.0, "bg_color": [0.87, 0.87, 0.89], "fg_components": 1})
    monkeypatch.setattr(ig, "external_eval", lambda url, f: None)
    refs = [tmp_path / "subject.png", tmp_path / "style.png"]
    for r in refs:
        r.write_bytes(b"png")

    warnings: list = []
    cands, _, _ = ig.generate_candidates(
        {"prompt": "P", "seeds": [1, 2], "width": 1024, "height": 1024, "steps": 8,
         "reference_images": [str(r) for r in refs]},
        tmp_path, {"url": "http://server", "workflow": "krea2_edit_refs.json"}, warnings)

    assert uploaded == ["reference1", "reference2"]
    assert len(seen) == 2 and [w["17"]["inputs"]["seed"] for w in seen] == [1, 2]
    assert all(w["12"]["inputs"]["image_b"] == ["31", 0] for w in seen), "each candidate gets both references"
    assert all(w["10"]["inputs"]["image"] == "reference1_subject.png" for w in seen)
    assert all(w["30"]["inputs"]["image"] == "reference2_style.png" for w in seen)
    assert [c["seed"] for c in cands] == [1, 2]
    assert warnings == []


def test_candidate_generation_with_one_reference_cuts_the_spare_slot_for_every_seed(monkeypatch, tmp_path):
    import json as _json

    from comfy_worker import image_generate as ig
    wf = multi_ref_workflow()
    monkeypatch.setattr(ig, "load_workflow", lambda p: wf)
    monkeypatch.setattr(ig, "find_workflow_file", lambda name: Path(name))
    monkeypatch.setattr(ig, "probe", lambda server: {})
    monkeypatch.setattr(ig, "upload_image", lambda server, path, prefix=None: "subject.png")
    seen: list = []

    def fake_run(server, workflow, timeout, client_id=None):
        seen.append(_json.loads(_json.dumps(workflow)))
        return {"outputs": {"19": {"images": [{"filename": "c.png", "subfolder": "", "type": "output"}]}},
                "prompt": [1, "client", workflow]}

    monkeypatch.setattr(ig, "run_workflow", fake_run)
    monkeypatch.setattr(ig, "download", lambda server, name, sub, t, dest: dest.write_bytes(b"png") or 3)
    monkeypatch.setattr(ig, "analyze_candidate", lambda f: {"score": 1.0})
    monkeypatch.setattr(ig, "external_eval", lambda url, f: None)
    ref = tmp_path / "subject.png"
    ref.write_bytes(b"png")

    warnings: list = []
    ig.generate_candidates({"prompt": "P", "seeds": [4], "width": 1024, "height": 1024,
                            "reference_images": [str(ref)]},
                           tmp_path, {"url": "http://server", "workflow": "krea2_edit_refs.json"}, warnings)

    assert len(seen) == 1
    assert "image_b" not in seen[0]["12"]["inputs"] and "source_image_b" not in seen[0]["15"]["inputs"]
    assert "30" not in _reachable_from_output(seen[0])
    assert any("had no reference" in w for w in warnings)


def test_no_references_leaves_a_reference_graph_exactly_as_shipped(monkeypatch, tmp_path):
    """A text-to-image job against an editing workflow is a mistake the caller makes, not one to guess around:
    with no references nothing is uploaded and nothing is cut."""
    from comfy_worker import image_generate as ig
    wf = multi_ref_workflow()
    monkeypatch.setattr(ig, "load_workflow", lambda p: wf)
    monkeypatch.setattr(ig, "find_workflow_file", lambda name: Path(name))
    monkeypatch.setattr(ig, "probe", lambda server: {})
    monkeypatch.setattr(ig, "upload_image",
                        lambda server, path, prefix=None: pytest.fail("nothing should be uploaded"))
    seen: list = []

    def fake_run(server, workflow, timeout, client_id=None):
        seen.append(workflow)
        return {"outputs": {"19": {"images": [{"filename": "c.png", "subfolder": "", "type": "output"}]}},
                "prompt": [1, "client", workflow]}

    monkeypatch.setattr(ig, "run_workflow", fake_run)
    monkeypatch.setattr(ig, "download", lambda server, name, sub, t, dest: dest.write_bytes(b"png") or 3)
    monkeypatch.setattr(ig, "analyze_candidate", lambda f: {"score": 1.0})
    monkeypatch.setattr(ig, "external_eval", lambda url, f: None)

    warnings: list = []
    ig.generate_candidates({"prompt": "P", "seeds": [4], "width": 1024, "height": 1024},
                           tmp_path, {"url": "http://server", "workflow": "krea2_edit_refs.json"}, warnings)

    assert len(seen) == 1
    assert seen[0]["10"]["inputs"]["image"] == "reference1.png"      # the workflow's own placeholders
    assert seen[0]["12"]["inputs"]["image_b"] == ["31", 0]
    assert warnings == []


def test_turnaround_uploads_the_reference_and_points_every_loadimage_at_it(monkeypatch, tmp_path):
    """The reference reaches three different nodes, but all three read the LoadImage, so one patch covers the
    whole edit wiring."""
    import json as _json

    from comfy_worker import image_generate as ig
    wf = edit_workflow()
    monkeypatch.setattr(ig, "load_workflow", lambda p: wf)
    monkeypatch.setattr(ig, "find_workflow_file", lambda name: Path(name))
    monkeypatch.setattr(ig, "probe", lambda server: {"comfyui_version": "test"})
    uploaded: list = []
    monkeypatch.setattr(ig, "upload_image",
                        lambda server, path: uploaded.append(Path(path).name) or "remote_ref.png")
    seen: dict = {}

    def fake_run(server, workflow, timeout, client_id=None):
        seen["wf"] = _json.loads(_json.dumps(workflow))
        seen["server"] = server
        return {"outputs": {"19": {"images": [{"filename": "sheet.png", "subfolder": "", "type": "output"}]}},
                "prompt": [1, "client", workflow]}

    monkeypatch.setattr(ig, "run_workflow", fake_run)
    monkeypatch.setattr(ig, "download", lambda server, name, sub, t, dest: dest.write_bytes(b"png") or 3)
    ref = tmp_path / "reference.png"
    ref.write_bytes(b"png")

    warnings: list = []
    out = ig.generate_turnaround({"workflow": "krea2_turnaround.json", "reference_image": str(ref),
                                  "prompt": "make a turnaround", "width": 2048, "height": 512, "steps": 8,
                                  "grounding_px": 512, "seed": 3},
                                 tmp_path, {"url": "http://server", "workflow": "unused.json"}, warnings)

    assert uploaded == ["reference.png"]
    assert seen["server"] == "http://server"
    assert seen["wf"]["10"]["inputs"]["image"] == "remote_ref.png"
    assert seen["wf"]["12"]["inputs"]["prompt"] == "make a turnaround"
    assert seen["wf"]["12"]["inputs"]["grounding_px"] == 512
    assert seen["wf"]["13"]["inputs"]["grounding_px"] == 512
    assert (tmp_path / "turnaround.png").is_file()
    assert out["sheet"] == "turnaround.png"
    assert warnings == []


def test_turnaround_refuses_a_workflow_with_no_loadimage(monkeypatch, tmp_path):
    from comfy_worker import image_generate as ig
    wf = edit_workflow()
    del wf["10"]
    monkeypatch.setattr(ig, "load_workflow", lambda p: wf)
    monkeypatch.setattr(ig, "find_workflow_file", lambda name: Path(name))
    monkeypatch.setattr(ig, "probe", lambda server: {})
    monkeypatch.setattr(ig, "upload_image", lambda server, path: "remote.png")
    ref = tmp_path / "reference.png"
    ref.write_bytes(b"png")
    with pytest.raises(cc.ComfyError, match="no LoadImage"):
        ig.generate_turnaround({"workflow": "w.json", "reference_image": str(ref), "prompt": "p",
                                "width": 64, "height": 64, "steps": 1},
                               tmp_path, {"url": "http://server"}, [])


def test_run_dispatches_the_turnaround_mode(monkeypatch, tmp_path):
    from comfy_worker import image_generate as ig
    called: dict = {}

    def fake_run_turnaround(req):
        called["mode"] = req.get("mode")
        (Path(req["out_dir"]) / "output.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(ig, "run_turnaround", fake_run_turnaround)
    ig.run({"mode": "turnaround", "out_dir": str(tmp_path)})
    assert called["mode"] == "turnaround"


# -------------------------------------------------------------------------------------------- drawing it again

def _turnaround_sheet(path, side_pair="duplicate"):
    """A four-cell sheet - front, left, back, right - with the side pair however the test wants it.

    The silhouettes are distinct on purpose: the splitter drops a panel that is another panel's picture, and a
    signature is taken from the object's own box, where two solid blobs of the same box-filling kind are
    indistinguishable (see the fixtures in test_sheet.py).
    """
    from PIL import Image, ImageDraw

    front = [(-22, -32), (22, -32), (22, 0), (2, 0), (2, 32), (-22, 32)]
    wedge = [(-34, 30), (-34, -30), (34, 30)]
    cross = [(-10, -32), (10, -32), (10, -10), (32, -10), (32, 10), (10, 10), (10, 32),
             (-10, 32), (-10, 10), (-32, 10), (-32, -10), (-10, -10)]
    right = wedge if side_pair == "duplicate" else [(-x, y) for x, y in wedge]
    im = Image.new("RGB", (720, 340), (200, 200, 198))
    draw = ImageDraw.Draw(im)
    for cx, points in zip((80, 260, 440, 620), (front, wedge, cross, right)):
        draw.polygon([(cx + x, 170 + y) for x, y in points], fill=(60, 120, 90))
    im.save(path)
    return path


def test_a_sheet_the_model_drew_the_same_side_twice_is_drawn_again(studio, tmp_path, monkeypatch):
    """Measured: the model gives both side panels the same side in 3 sheets out of 5.

    Mirroring one of them is exact only for a left-right symmetric object, so a sheet that comes back that way is
    worth another draw - and the re-draw has to change the seed, or it reproduces the same mistake.
    """
    import json
    import shutil

    from studio.db import JobStore
    from studio.pipeline import JobRun

    store = JobStore()
    job = store.create(
        {"prompt": "a car", "style": "mobile_factory", "quality": "balanced"},
        {"seed": 7, "backend": {"image": {"kind": "comfyui", "url": "http://x", "workflow": "w.json"}},
         "views": {"enabled": True, "workflow": "krea2_turnaround.json", "prompt": "four views",
                   "width": 2048, "height": 512, "steps": 8, "grounding_px": 768, "count": 4,
                   "fov_degrees": 20.0}},
        kind="asset")
    run = JobRun(store, store.get(job))
    monkeypatch.setattr(run, "remote", lambda role: True)

    sheets = [_turnaround_sheet(tmp_path / "first.png", "duplicate"),
              _turnaround_sheet(tmp_path / "second.png", "pair")]
    seeds: list[int] = []

    def fake_stage(role, stage, module, stage_dir, log_path, extra):
        seeds.append(json.loads((stage_dir / "request.json").read_text(encoding="utf-8"))["seed"])
        shutil.copy(sheets[min(len(seeds) - 1, len(sheets) - 1)], stage_dir / "turnaround.png")
        (stage_dir / "output.json").write_text(
            json.dumps({"sheet": "turnaround.png", "time_s": 1.0, "warnings": [], "effective": {}}),
            encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr(run, "_run_gpu_stage", fake_stage)
    d = run.stage_dir("reference")
    d.mkdir(parents=True, exist_ok=True)
    out = run._generate_views(tmp_path / "reference.png", d)

    assert len(seeds) == 2 and seeds[1] != seeds[0], "the second draw has to use a different seed"
    assert out["num_views"] == 4 and out["turnaround"]["attempts"] == 2
    # the rejected sheet's mirror warning is not reported: the accepted sheet never needed one
    assert not any("same side twice" in w for w in run.warnings)


# ------------------------------------------- references and verbatim prompts on the way into the reference stage

def _tiny_png(colour=(90, 120, 100)) -> bytes:
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (24, 24), colour).save(buf, format="PNG")
    return buf.getvalue()


def _reference_settings() -> dict:
    return {"seed": 11, "style": "mobile_factory",
            "reference": {"model": "krea2-turbo", "family": "comfyui", "width": 1024, "height": 1024,
                          "steps": 8, "cfg": 1.0, "candidates": 1},
            "backend": {"image": {"kind": "comfyui", "url": "http://x", "workflow": "krea2_edit_refs.json"}}}


def test_references_are_copied_into_the_job_before_the_stage_runs(studio):
    """Only files under the job directory are shipped to a stage worker, so a reference that names another job's
    file has to be copied in first, and an inline picture has to be written out. Either way it is renamed to
    `ref<i>`, which is also what keeps two references from one job from colliding on the ComfyUI server."""
    import base64

    from studio import config
    from studio.db import JobStore
    from studio.pipeline import JobRun

    other = config.JOBS_DIR / "20260101-000000-aaaaaaaa" / "artifacts"
    other.mkdir(parents=True)
    (other / "reference.png").write_bytes(_tiny_png((10, 20, 30)))

    store = JobStore()
    job = store.create(
        {"prompt": "an edited crate", "style": "mobile_factory",
         "references": [{"image_b64": base64.b64encode(_tiny_png((90, 120, 100))).decode(), "label": "front"},
                        {"job_id": "20260101-000000-aaaaaaaa", "file": "artifacts/reference.png",
                         "label": "style"}]},
        _reference_settings(), kind="image")
    run = JobRun(store, store.get(job))
    d = run.stage_dir("reference")
    d.mkdir(parents=True, exist_ok=True)

    paths = run._stage_references(d)

    assert [Path(p).name for p in paths] == ["ref0.png", "ref1.png"]
    assert all(Path(p).parent == d / "references" for p in paths)
    assert Path(paths[0]).read_bytes() == _tiny_png((90, 120, 100)), "the inline bytes survive the round trip"
    assert Path(paths[1]).read_bytes() == (other / "reference.png").read_bytes()
    assert not run.request.get("workflow")      # the workflow comes from settings, not from the request here


def test_no_references_means_no_reference_directory(studio):
    from studio.db import JobStore
    from studio.pipeline import JobRun

    store = JobStore()
    job = store.create({"prompt": "a crate", "style": "mobile_factory"}, _reference_settings(), kind="image")
    run = JobRun(store, store.get(job))
    d = run.stage_dir("reference")
    d.mkdir(parents=True, exist_ok=True)
    assert run._stage_references(d) == []
    assert not (d / "references").exists(), "a text-to-image job should not grow an empty references/ directory"


def test_the_stage_request_carries_the_references_and_a_verbatim_prompt(studio, monkeypatch):
    """The canvas shows the composed prompt and sends it back edited. `final_prompt` is that edit, and it has to
    win over the composed template - otherwise "just tweak the prompt" would still be a guess."""
    import base64
    import json

    from studio.db import JobStore
    from studio.pipeline import JobRun

    store = JobStore()
    job = store.create(
        {"prompt": "an edited crate", "style": "mobile_factory",
         "final_prompt": "A single painted crate, verbatim.",
         "references": [{"image_b64": base64.b64encode(_tiny_png()).decode(), "label": "front"}]},
        _reference_settings(), kind="image")
    run = JobRun(store, store.get(job))
    seen: dict = {}

    def fake_stage(role, stage, module, stage_dir, log_path, extra):
        seen.update(json.loads((stage_dir / "request.json").read_text(encoding="utf-8")))
        (stage_dir / "candidates").mkdir(exist_ok=True)
        (stage_dir / "candidates" / "cand_00.png").write_bytes(_tiny_png())
        (stage_dir / "output.json").write_text(json.dumps(
            {"candidates": [{"file": "candidates/cand_00.png", "seed": 11, "metrics": {}}],
             "selected": "candidates/cand_00.png", "selection": {"chosen": "candidates/cand_00.png"},
             "stats": {}, "warnings": []}), encoding="utf-8")
        (stage_dir / "selection.json").write_text("{}", encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr(run, "_run_gpu_stage", fake_stage)
    run.stage_reference(variations_only=True)

    assert seen["prompt"] == "A single painted crate, verbatim."
    assert seen["template"]["subject"] == "an edited crate", "what the composition would have been is still recorded"
    assert [Path(p).name for p in seen["reference_images"]] == ["ref0.png"]
    assert Path(seen["reference_images"][0]).is_file()
    assert seen["model_id"] == "krea2-turbo" and seen["backend"]["workflow"] == "krea2_edit_refs.json"
    assert seen["width"] == 1024 and seen["steps"] == 8
    assert any("verbatim" in e["message"] for e in store.events(job))

