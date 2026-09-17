"""Multi-view input: the frame -> slot mapping the control plane does, and the graph wiring the ComfyUI
worker does with the result.

Both halves can be wrong *quietly*. A frame in the wrong slot produces a plausible asset facing the wrong
way; a flat patch of every LoadImage (which is what the single-image path does) would feed the same picture
into all four views and the reconstruction would look merely blurry. So both are tested directly, against
the workflow that actually ships.

No GPU, no network, no ComfyUI server.
"""
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in ("services/studio", "services"):
    sys.path.insert(0, str(ROOT / p))

from comfy_worker import three_d_generate as td          # noqa: E402
from studio import multiview as mv                        # noqa: E402

MULTIVIEW_WORKFLOW = ROOT / "presets" / "workflows" / "3d_pixal3d_multi_views.json"
SINGLE_WORKFLOW = ROOT / "presets" / "workflows" / "3d_pixal3d_trellis2_image_to_model.json"


def c2w(az_deg: float, el_deg: float = 0.0, dist: float = 3.0) -> list[list[float]]:
    """A camera-to-world matrix for the rig Pixal3DMultiViewConditioning builds.

    That node places each camera at (sin az, -cos az, sin el) * distance, looking back at the origin, so
    these are the matrices the control plane has to be able to invert.
    """
    az, el = math.radians(az_deg), math.radians(el_deg)
    back = (math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el))
    right = (math.cos(az), math.sin(az), 0.0)
    up = (back[1] * right[2] - back[2] * right[1],
          back[2] * right[0] - back[0] * right[2],
          back[0] * right[1] - back[1] * right[0])
    return [[right[0], up[0], back[0], back[0] * dist],
            [right[1], up[1], back[1], back[1] * dist],
            [right[2], up[2], back[2], back[2] * dist],
            [0.0, 0.0, 0.0, 1.0]]


def frame(name: str, az: float, **kw) -> dict:
    return {"file_path": f"{name}.png", "name": name, "transform_matrix": c2w(az), **kw}


def views_dir(tmp_path: Path, meta: dict) -> Path:
    d = tmp_path / "views"
    d.mkdir()
    for fr in meta["frames"]:
        (d / fr["file_path"]).write_bytes(b"not really a png")
    (d / "transforms.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


# --------------------------------------------------------------------------- the rig's own azimuth convention

@pytest.mark.parametrize("az_deg,slot", [(0, "front"), (90, "left"), (180, "back"), (270, "right")])
def test_camera_azimuth_inverts_the_node_rig(az_deg, slot):
    az, el = mv.camera_azimuth_elevation(c2w(az_deg))
    assert round(az) == az_deg
    assert round(el) == 0
    assert mv.nearest_slot(az)[0] == slot
    assert mv.nearest_slot(az)[1] == pytest.approx(0.0)


def test_azimuth_wraps_around_zero():
    # 350 degrees is 10 off the front, not 350 off it
    assert mv.nearest_slot(350.0) == ("front", pytest.approx(10.0))
    assert mv.nearest_slot(-90.0 % 360) == ("right", pytest.approx(0.0))


# --------------------------------------------------------------------------- frame -> slot

def test_unnamed_frames_are_placed_by_their_measured_azimuth():
    frames = [frame("a", 180), frame("b", 0), frame("c", 270), frame("d", 90)]
    slots, warnings = mv.assign_slots(frames)
    assert slots == {"back": 0, "front": 1, "right": 2, "left": 3}
    assert warnings == []


def test_explicit_names_win_and_a_contradiction_is_reported():
    slots, warnings = mv.assign_slots([frame("front", 0), frame("back", 90)])
    assert slots == {"front": 0, "back": 1}
    assert len(warnings) == 1
    assert "one of the two is wrong" in warnings[0]


def test_two_views_named_the_same_do_not_share_a_slot():
    slots, warnings = mv.assign_slots([frame("front", 0), frame("front", 270)])
    assert slots == {"front": 0, "right": 1}
    assert any("two views are named" in w for w in warnings)


def test_identical_matrices_fall_back_to_input_order():
    """A caller that fills in placeholder poses must not collapse all four views onto the front slot."""
    slots, warnings = mv.assign_slots([frame(f"v{i}", 0) for i in range(4)])
    assert sorted(slots) == ["back", "front", "left", "right"]
    assert any("by position" in w for w in warnings)


def test_a_fifth_view_is_dropped_with_a_warning():
    frames = [frame("a", 0), frame("b", 90), frame("c", 180), frame("d", 270), frame("e", 45)]
    slots, warnings = mv.assign_slots(frames)
    assert len(slots) == 4
    assert any("was dropped" in w for w in warnings)


def test_an_elevated_view_is_reported_because_the_rig_is_level():
    _, warnings = mv.assign_slots([frame("front", 0, el=None) if False else
                                   {"file_path": "front.png", "transform_matrix": c2w(0, 30)},
                                   frame("left", 90)])
    assert any("above the horizon" in w for w in warnings)


# --------------------------------------------------------------------------- FOV

def test_approximate_poses_defer_the_fov_to_moge(tmp_path):
    d = views_dir(tmp_path, {"camera_source": "approximate", "camera_angle_x": math.radians(20),
                             "frames": [frame("front", 0), frame("left", 90)]})
    views, fov, warnings = mv.view_inputs(d)
    assert fov is None                     # the workflow measures it from the front view instead
    assert sorted(views) == ["front", "left"]
    assert any("approximate" in w for w in warnings)


@pytest.mark.parametrize("source", ["rig", "measured"])
def test_declared_poses_pin_the_fov_in_degrees(tmp_path, source):
    d = views_dir(tmp_path, {"camera_source": source, "camera_angle_x": math.radians(20),
                             "frames": [frame("front", 0), frame("left", 90)]})
    views, fov, warnings = mv.view_inputs(d)
    assert fov == pytest.approx(20.0)
    assert warnings == []


def test_an_impossible_fov_is_refused_rather_than_passed_on(tmp_path):
    # 3.0 rad = 172 degrees, past the node's 1..170 range
    d = views_dir(tmp_path, {"camera_source": "rig", "camera_angle_x": 3.0,
                             "frames": [frame("front", 0), frame("left", 90)]})
    _, fov, warnings = mv.view_inputs(d)
    assert fov is None
    assert any("outside the multi-view node" in w for w in warnings)


def test_a_frames_own_fov_wins_over_the_inputs(tmp_path):
    d = views_dir(tmp_path, {"camera_source": "measured", "camera_angle_x": math.radians(30),
                             "frames": [frame("front", 0, camera_angle_x=math.radians(20)),
                                        frame("left", 90)]})
    assert mv.view_inputs(d)[1] == pytest.approx(20.0)


def test_a_fov_in_degrees_is_rejected_rather_than_read_as_radians(tmp_path):
    """20 as a per-frame value is 1146 degrees if read as radians; the API declares radians."""
    d = views_dir(tmp_path, {"camera_source": "measured", "camera_angle_x": math.radians(20),
                             "frames": [frame("front", 0, camera_angle_x=20.0), frame("left", 90)]})
    _, fov, warnings = mv.view_inputs(d)
    assert fov == pytest.approx(20.0)      # fell back to the input's own value
    assert any("outside the radians range" in w for w in warnings)


def test_a_missing_front_slot_still_maps_the_views(tmp_path):
    d = views_dir(tmp_path, {"camera_source": "rig", "camera_angle_x": math.radians(20),
                             "frames": [frame("left", 90), frame("back", 180)]})
    views, fov, warnings = mv.view_inputs(d)
    assert sorted(views) == ["back", "left"]
    assert fov == pytest.approx(20.0)
    assert warnings == []


def test_no_frames_is_reported_not_crashed(tmp_path):
    d = views_dir(tmp_path, {"camera_source": "rig", "camera_angle_x": math.radians(20), "frames": []})
    views, fov, warnings = mv.view_inputs(d)
    assert views == {} and fov is None
    assert warnings


# --------------------------------------------------------------------------- worker graph wiring

def test_view_slots_are_traced_back_to_their_own_loadimage():
    """Each slot must reach a *different* LoadImage; the slot wiring lives in the workflow, not in settings."""
    wf = json.loads(MULTIVIEW_WORKFLOW.read_text(encoding="utf-8"))
    mv_id = td.multiview_node(wf)
    assert mv_id is not None
    loaders = td.view_loaders(wf, mv_id)
    assert sorted(loaders) == ["back", "front", "left", "right"]
    assert len(set(loaders.values())) == 4
    for nid in loaders.values():
        assert wf[nid]["class_type"] == "LoadImage"


def test_the_single_image_workflow_has_no_multiview_node():
    wf = json.loads(SINGLE_WORKFLOW.read_text(encoding="utf-8"))
    assert td.multiview_node(wf) is None


def test_patch_multiview_input_uploads_one_image_per_slot(monkeypatch):
    wf = json.loads(MULTIVIEW_WORKFLOW.read_text(encoding="utf-8"))
    uploaded = []

    def fake_upload(server, path, prefix=None):
        uploaded.append((Path(path).name, prefix))
        return f"remote_{prefix}_{Path(path).name}"

    monkeypatch.setattr(td, "upload_image", fake_upload)
    views = {"front": "/j/views/front.png", "left": "/j/views/left.png",
             "back": "/j/views/back.png", "right": "/j/views/right.png"}
    warnings: list[str] = []
    td.patch_multiview_input(wf, "http://server", views, 20.0, warnings)

    # one upload per slot, each named for its slot: four views of one asset often share a basename, and the
    # upload replaces by name, so two of them sharing one would silently turn four views into two pictures
    assert sorted(uploaded) == [("back.png", "back"), ("front.png", "front"),
                                ("left.png", "left"), ("right.png", "right")]
    mv_id = td.multiview_node(wf)
    loaders = td.view_loaders(wf, mv_id)
    # the whole point: four different pictures, not the single-image path's "patched all of them"
    assert {wf[nid]["inputs"]["image"] for nid in loaders.values()} == {
        "remote_front_front.png", "remote_left_left.png", "remote_back_back.png", "remote_right_right.png"}
    # the caller's FOV replaces the workflow's MoGeGeometryToFOV link
    assert wf[mv_id]["inputs"]["fov"] == 20.0
    assert warnings == []


def test_patch_multiview_input_leaves_moge_in_place_without_a_declared_fov(monkeypatch):
    wf = json.loads(MULTIVIEW_WORKFLOW.read_text(encoding="utf-8"))
    before = wf[td.multiview_node(wf)]["inputs"]["fov"]
    monkeypatch.setattr(td, "upload_image", lambda server, path, prefix=None: "remote.png")
    td.patch_multiview_input(wf, "http://server", {"front": "/j/views/front.png"}, None, [])
    assert wf[td.multiview_node(wf)]["inputs"]["fov"] == before
    assert isinstance(before, list) and before[0] == "242"


def test_patch_multiview_input_warns_about_a_slot_the_workflow_does_not_wire(monkeypatch):
    wf = json.loads(MULTIVIEW_WORKFLOW.read_text(encoding="utf-8"))
    # unwire the right view, as a user editing the workflow might
    del wf[td.multiview_node(wf)]["inputs"]["right"]
    monkeypatch.setattr(td, "upload_image", lambda server, path, prefix=None: "remote.png")
    warnings: list[str] = []
    td.patch_multiview_input(wf, "http://server", {"right": "/j/views/right.png"}, None, warnings)
    assert any("right view" in w for w in warnings)


def test_patch_multiview_input_refuses_a_workflow_without_the_node(monkeypatch):
    wf = json.loads(SINGLE_WORKFLOW.read_text(encoding="utf-8"))
    monkeypatch.setattr(td, "upload_image", lambda server, path, prefix=None: "remote.png")
    with pytest.raises(td.ComfyError):
        td.patch_multiview_input(wf, "http://server", {"front": "/j/views/front.png"}, None, [])


def _upstream(wf: dict, nid: str) -> set[str]:
    """Every node the given one depends on, itself included."""
    seen: set[str] = set()
    stack = [nid]
    while stack:
        n = stack.pop()
        if n in seen or n not in wf:
            continue
        seen.add(n)
        for val in (wf[n].get("inputs") or {}).values():
            if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str):
                stack.append(val[0])
    return seen


def test_a_slot_with_no_view_is_disconnected(monkeypatch):
    """Fewer views than slots is normal - the 2D stage produces it whenever the sheet comes back short.

    A slot left wired would keep the filename the workflow ships with, and that is a hard validation error:
    measured on a real job with two usable panels, "node 410 (LoadImage): image - Invalid image file:
    view_410.png", which failed the stage before it rendered anything. The node takes its views as optional
    inputs and re-bases its rig on the first one it is given, so fewer views reconstruct fine.
    """
    wf = json.loads(MULTIVIEW_WORKFLOW.read_text(encoding="utf-8"))
    monkeypatch.setattr(td, "upload_image", lambda server, path, prefix=None: "remote.png")
    warnings: list[str] = []
    td.patch_multiview_input(wf, "http://server",
                             {"front": "/j/views/front.png", "back": "/j/views/back.png"}, None, warnings)

    mv_id = td.multiview_node(wf)
    assert mv_id in wf, "the conditioning node still feeds the sampler, so it has to survive"
    assert sorted(td.view_loaders(wf, mv_id)) == ["back", "front"]
    assert "left" not in wf[mv_id]["inputs"] and "right" not in wf[mv_id]["inputs"]
    # the unused views' branches are still in the graph, but nothing the sampler needs can reach them any more
    # - which is what keeps ComfyUI from validating their missing files at all
    reachable = set().union(*(_upstream(wf, val[0]) for val in wf[mv_id]["inputs"].values()
                              if isinstance(val, list)))
    assert reachable & {nid for nid, n in wf.items() if n.get("class_type") == "LoadImage"} == {"122", "410"}
    assert warnings == []


# --------------------------------------------------------------------------- settings

def test_the_multiview_workflow_defaults_to_the_shipped_one_and_can_be_overridden():
    from studio.presets import MULTIVIEW_WORKFLOW, backend_config

    assert backend_config(None)["three_d"]["multiview_workflow"] == MULTIVIEW_WORKFLOW
    assert backend_config({"three_d_multiview_workflow": "mine.json"})["three_d"]["multiview_workflow"] == "mine.json"
    assert (ROOT / "presets" / "workflows" / MULTIVIEW_WORKFLOW).is_file()
