"""Map a job's multi-view input onto the four named slots of ComfyUI's Pixal3DMultiViewConditioning.

The local Pixal3D path hands `views/transforms.json` to the library, which poses every camera from its
matrix. The ComfyUI node cannot do that: it takes four *named* views and rebuilds a fixed, level,
90-degrees-apart orbit rig. So the control plane has to decide which of the caller's frames is "front",
"left", "back" and "right", and what horizontal FOV the views were shot at.

Both decisions are quiet when they are wrong - the asset simply comes out facing the wrong way, or at the
wrong scale - so every fallback is reported instead of guessed silently.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

# The rig the node builds, as azimuth in degrees: position = (sin az, -cos az, sin el) * distance.
VIEW_SLOTS = ("front", "left", "back", "right")
SLOT_AZIMUTH = {"front": 0.0, "left": 90.0, "back": 180.0, "right": 270.0}
# How far a camera may sit from a slot before we stop believing it belongs there.
AZIMUTH_TOLERANCE_DEG = 15.0
# The rig has no elevation: a frame shot from above is silently levelled, which is worth saying out loud.
ELEVATION_TOLERANCE_DEG = 10.0
# Pixal3DMultiViewConditioning's own fov range (1..170) and the range the API accepts for radians.
NODE_FOV_RANGE = (1.0, 170.0)
RADIAN_RANGE = (0.05, 2.8)

_IDENTITY = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def camera_azimuth_elevation(matrix) -> tuple[float, float]:
    """The rig azimuth (degrees, 0 = front) and elevation of one camera-to-world matrix.

    Inverting the node's own rig: its camera position is (sin az, -cos az, sin el) * distance, so with the
    camera's world position p = (x, y, z) (the translation column of a c2w matrix) we get az = atan2(x, -y).
    """
    m = matrix or _IDENTITY
    try:
        x, y, z = float(m[0][3]), float(m[1][3]), float(m[2][3])
    except (IndexError, TypeError, ValueError):
        return 0.0, 0.0
    r = math.sqrt(x * x + y * y + z * z)
    if r < 1e-9:
        return 0.0, 0.0
    az = math.degrees(math.atan2(x, -y)) % 360.0
    el = math.degrees(math.asin(max(-1.0, min(1.0, z / r))))
    return az, el


def _gap(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def nearest_slot(azimuth: float) -> tuple[str, float]:
    slot = min(VIEW_SLOTS, key=lambda s: _gap(azimuth, SLOT_AZIMUTH[s]))
    return slot, _gap(azimuth, SLOT_AZIMUTH[slot])


def assign_slots(frames: list[dict]) -> tuple[dict[str, int], list[str]]:
    """{slot: frame index} plus warnings.

    Evidence, best first: an explicit `name`, then the camera's measured azimuth, then the order the frames
    were sent in. A slot is never handed to two frames.
    """
    warnings: list[str] = []
    slots: dict[str, int] = {}
    free = list(VIEW_SLOTS)

    def take(slot: str, i: int) -> None:
        slots[slot] = i
        free.remove(slot)

    # 1. what the caller called each frame
    for i, fr in enumerate(frames):
        name = str(fr.get("name") or "").strip().lower()
        if name not in VIEW_SLOTS:
            continue
        az, _ = camera_azimuth_elevation(fr.get("transform_matrix"))
        gap = _gap(az, SLOT_AZIMUTH[name])
        if name in slots:
            warnings.append(f"two views are named {name!r} (view {i + 1} ignored); each slot takes one view")
            continue
        if gap > AZIMUTH_TOLERANCE_DEG:
            warnings.append(f"view {i + 1} is named {name!r} but its camera sits {gap:.0f}° off that slot "
                            f"(azimuth {az:.0f}°); the name is used, but one of the two is wrong")
        take(name, i)

    # 2. the camera's own azimuth
    leftover: list[int] = []
    for i, fr in enumerate(frames):
        if i in slots.values():
            continue
        az, _ = camera_azimuth_elevation(fr.get("transform_matrix"))
        slot, gap = nearest_slot(az)
        if slot in free and gap <= AZIMUTH_TOLERANCE_DEG:
            take(slot, i)
        else:
            leftover.append(i)

    # 3. the order they arrived in
    for i in leftover:
        if not free:
            warnings.append(f"view {i + 1}: the multi-view rig has four slots; this view was dropped")
            continue
        warnings.append(f"view {i + 1} has no name and its camera azimuth does not match a slot; "
                        f"it was placed in the {free[0]!r} slot by position")
        take(free[0], i)

    # the rig is level, so an elevated input view is a real (if quiet) change of pose
    for i, fr in enumerate(frames):
        _, el = camera_azimuth_elevation(fr.get("transform_matrix"))
        if abs(el) > ELEVATION_TOLERANCE_DEG and i in slots.values():
            warnings.append(f"view {i + 1} is shot from {el:.0f}° above the horizon; the Pixal3D rig is a "
                            f"level orbit, so the pose is treated as level")
    return slots, warnings


def _declared_fov(meta: dict, frames: list[dict], slots: dict[str, int]) -> tuple[float | None, list[str]]:
    """The horizontal FOV in degrees the caller stated, or None when the workflow should measure it."""
    warnings: list[str] = []
    per_frame = [fr.get("camera_angle_x") for fr in frames if fr.get("camera_angle_x") is not None]
    usable = [v for v in per_frame if RADIAN_RANGE[0] < float(v) < RADIAN_RANGE[1]]
    dropped = [v for v in per_frame if v not in usable]
    if dropped:
        warnings.append(f"camera_angle_x {dropped[0]!r} on a frame is outside the radians range "
                        f"{RADIAN_RANGE[0]}..{RADIAN_RANGE[1]}; ignored (the API takes radians)")
    if len(set(round(float(v), 4) for v in usable)) > 1:
        warnings.append("the frames declare different horizontal FOVs; the multi-view rig has one camera, "
                        "so the front view's value is used")

    rad = None
    front = slots.get("front")
    if front is not None and frames[front].get("camera_angle_x") in usable:
        rad = float(frames[front]["camera_angle_x"])
    elif usable:
        rad = float(usable[0])
    elif meta.get("camera_angle_x") is not None:
        rad = float(meta["camera_angle_x"])
    if rad is None:
        return None, warnings

    deg = math.degrees(rad)
    if not (NODE_FOV_RANGE[0] <= deg <= NODE_FOV_RANGE[1]):
        warnings.append(f"the declared horizontal FOV works out to {deg:.1f}°, outside the multi-view node's "
                        f"{NODE_FOV_RANGE[0]:.0f}..{NODE_FOV_RANGE[1]:.0f}° range; the workflow's own value is used")
        return None, warnings
    return deg, warnings


def view_inputs(views_dir: str | Path) -> tuple[dict[str, str], float | None, list[str]]:
    """Turn a stage's `views/transforms.json` into ({slot: image path}, fov or None, warnings).

    `fov` is None when the caller's camera poses are only approximate: the poses are not trustworthy enough
    to derive a FOV from, so the workflow measures one from the front view with MoGeGeometryToFOV (which is
    what Pixal3DMultiViewConditioning's own fov tooltip recommends for photos).
    """
    d = Path(views_dir)
    meta = json.loads((d / "transforms.json").read_text(encoding="utf-8"))
    frames = list(meta.get("frames") or [])
    if not frames:
        return {}, None, ["the multi-view input has no frames"]
    slots, warnings = assign_slots(frames)
    views = {slot: str(d / str(frames[i]["file_path"])) for slot, i in slots.items()}

    if (meta.get("camera_source") or "rig") == "approximate":
        return views, None, warnings + [
            "the multi-view cameras are only approximate, so the workflow measures the horizontal FOV from "
            "the front view instead of trusting the poses"]
    fov, fov_warnings = _declared_fov(meta, frames, slots)
    return views, fov, warnings + fov_warnings
