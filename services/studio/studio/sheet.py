"""Turn a generated turnaround sheet into the per-view images the multi-view 3D lane consumes.

The 2D stage asks a Krea-2 editor for "one horizontal row of four views". What comes back is a plain
background with several copies of the object on it, and there are two things to get right before those tiles
can be fed to Pixal3D's rig:

* **Which panel is which view.** The model also paints the *source image itself* into the row: the reference is
  part of its context, and because the node pack resamples that source onto the canvas "at a centered offset",
  the model reproduces it in the middle of the row. That panel is not a view and is dropped. Its *position* is
  the reliable tell, not its appearance: measured on real sheets the extra panel sits within 4px of the centre
  while the nearest real view is 360px away, whereas comparing panels against the reference image only rates the
  duplicate 1.26x above the runner-up (it is a re-render, not a copy). Whatever is left goes into the slots in
  row order.

* **One scale for every view.** The model drew the views consistently, so the crop must not re-normalise each
  panel to its own silhouette: a car is wide from the side and narrow from the front, and per-panel cropping
  would blow the front view up until it matched. Every panel is cut with the *same* square window, sized from
  the widest panel times `_VIEW_PAD` - so the widest view spans 1/1.1 of its frame, exactly the framing
  `Pixal3DMultiViewConditioning` documents, and the narrow views stay proportionally narrower.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from .multiview import SLOT_AZIMUTH, VIEW_SLOTS

try:  # the control plane installs Pillow for thumbnails; the splitter is only used when views are generated
    from PIL import Image
except ImportError:  # pragma: no cover - reported by the caller
    Image = None

import numpy as np

# Pixal3DMultiViewConditioning's own rig framing: the object spans about 1/1.1 of the frame at its widest.
VIEW_PAD = 1.1
# How far a channel may sit from the background colour before it counts as part of the object.
BG_TOLERANCE = 18
# Panels narrower than this fraction of the sheet are noise, not views.
MIN_PANEL_FRAC = 0.02
# How close to the middle of the sheet a panel has to sit to count as the painted-in source image. Measured:
# the duplicate is ~0.2% off centre, the nearest real view 17% off, so anything in between separates cleanly.
CENTRE_TOLERANCE_FRAC = 0.05
# How much better one reading of the side pair has to be before it is believed. Measured margins: a proper
# mirrored pair wins by 0.43, the same side drawn twice by 0.30 - so 0.10 separates them with room to spare.
SIDE_PAIR_MARGIN = 0.10


@dataclass
class Panel:
    x0: int
    x1: int
    y0: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0 + 1

    @property
    def height(self) -> int:
        return self.y1 - self.y0 + 1

    @property
    def centre_x(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def centre_y(self) -> float:
        return (self.y0 + self.y1) / 2


def _background(arr: np.ndarray, border: int = 8) -> np.ndarray:
    """The sheet is painted on a flat colour, so take the commonest colour around the edge."""
    ring = np.concatenate([arr[:border].reshape(-1, 3), arr[-border:].reshape(-1, 3),
                           arr[:, :border].reshape(-1, 3), arr[:, -border:].reshape(-1, 3)])
    values, counts = np.unique(ring, axis=0, return_counts=True)
    return values[counts.argmax()]


def find_panels(image, tolerance: int = BG_TOLERANCE, min_frac: float = MIN_PANEL_FRAC) -> list[Panel]:
    """Segment a sheet into panels by finding the runs of columns that contain any object pixels."""
    arr = np.asarray(image.convert("RGB")).astype(np.int16)
    height, width, _ = arr.shape
    foreground = np.abs(arr - _background(arr)).max(axis=2) > tolerance
    columns = foreground.any(axis=0)
    # a one-column gap inside an object (a thin highlight matching the background) must not split it
    for i in range(1, width - 1):
        if not columns[i] and columns[i - 1] and columns[i + 1]:
            columns[i] = True
    panels: list[Panel] = []
    start = None
    for i, filled in enumerate(columns):
        if filled and start is None:
            start = i
        elif not filled and start is not None:
            panels.append((start, i - 1))
            start = None
    if start is not None:
        panels.append((start, width - 1))
    minimum = max(4, int(min_frac * width))
    out = []
    for x0, x1 in panels:
        if x1 - x0 + 1 < minimum:
            continue
        rows = np.where(foreground[:, x0:x1 + 1].any(axis=1))[0]
        out.append(Panel(x0, x1, int(rows.min()), int(rows.max())))
    return out


def signature(image, box: Panel, size: int = 48) -> np.ndarray:
    """A small grey, contrast-normalised fingerprint of one view, for comparing two of them."""
    crop = image.convert("L").crop((box.x0, box.y0, box.x1 + 1, box.y1 + 1)).resize((size, size))
    arr = np.asarray(crop, dtype=np.float32) / 255.0
    arr -= arr.mean()
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm > 1e-6 else arr


def mirror_side_views(views: dict[str, Path], warnings: list) -> None:
    """Make sure `left` and `right` show opposite sides, mirroring one when the model drew the same side twice.

    The generator is not reliable about which side it draws. Measured on real sheets: one run produced a proper
    mirrored pair (0.95 correlated once one is flipped) and another the same side twice (0.65 correlated
    unflipped). Handing the rig the same side as both "left" and "right" tells it two contradictory things about
    one half of the object, and that reconstructs as a malformed side - which is what a "the car is not a car"
    result looks like. So a duplicate pair is turned into a mirrored pair by flipping the right view: exact for a
    symmetric object, approximate for an asymmetric one, and better than a contradiction either way.
    """
    if not {"left", "right"} <= set(views):
        return
    with Image.open(views["left"]) as a, Image.open(views["right"]) as b:
        a = a.convert("RGB")
        b = b.convert("RGB")
        left_sig = signature(a, Panel(0, a.width - 1, 0, a.height - 1))
        right_sig = signature(b, Panel(0, b.width - 1, 0, b.height - 1))
        same = float((left_sig * right_sig).sum())
        flipped = float((left_sig * right_sig[:, ::-1]).sum())
    if same > flipped + SIDE_PAIR_MARGIN:
        with Image.open(views["right"]) as b:
            b.transpose(Image.FLIP_LEFT_RIGHT).save(views["right"])
        warnings.append(f"the sheet drew the same side twice ({same:+.2f} correlated unflipped against "
                        f"{flipped:+.2f} flipped); the right view was mirrored from the left, which is exact for a "
                        f"symmetric object and approximate for an asymmetric one")
    elif abs(same - flipped) <= SIDE_PAIR_MARGIN:
        warnings.append(f"the two side views are neither a clean mirrored pair nor an obvious duplicate "
                        f"({same:+.2f} unflipped against {flipped:+.2f} flipped); they were used as drawn")


def split_sheet(sheet_path: Path, out_dir: Path,
                slots: tuple[str, ...] = VIEW_SLOTS) -> tuple[dict[str, Path], list[str]]:
    """Cut a turnaround sheet into one square image per slot. Returns ({slot: path}, warnings)."""
    if Image is None:
        raise RuntimeError("splitting a turnaround sheet needs Pillow, which the control plane installs "
                           "for thumbnails; install it on the machine running the control plane")
    warnings: list[str] = []
    with Image.open(sheet_path) as sheet:
        sheet = sheet.convert("RGB")
        width, height = sheet.size
        panels = find_panels(sheet)
        if not panels:
            raise RuntimeError(f"{sheet_path.name}: no object found on the sheet (is it blank?)")

        # The source the model was given is resampled onto the canvas at a centred offset, so the frame it
        # paints back is the middle of the row. Position is a much cleaner signal than appearance here.
        mid = width / 2
        nearest = min(range(len(panels)), key=lambda i: abs(panels[i].centre_x - mid))
        offset = abs(panels[nearest].centre_x - mid)
        if offset <= CENTRE_TOLERANCE_FRAC * width:
            warnings.append(f"dropped panel {nearest + 1} of {len(panels)}: its centre is {offset:.0f}px from the "
                            f"middle of the sheet, where the model paints its own source image rather than a view")
            panels = [p for i, p in enumerate(panels) if i != nearest]
        if len(panels) > len(slots):
            warnings.append(f"the sheet has {len(panels)} panels for {len(slots)} views and none of them is "
                            f"centred; kept the first {len(slots)} in row order")
            panels = panels[:len(slots)]
        elif len(panels) < len(slots):
            warnings.append(f"the sheet has only {len(panels)} panels for {len(slots)} views; the missing views "
                            f"were left out (Pixal3D reconstructs from fewer, just less surely)")

        # one square window for every view, from the widest panel, so relative scale survives
        side = int(math.ceil(max(max(p.width, p.height) for p in panels) * VIEW_PAD))
        centre_y = float(np.median([p.centre_y for p in panels]))
        background = tuple(int(v) for v in _background(np.asarray(sheet).astype(np.int16)))
        out_dir.mkdir(parents=True, exist_ok=True)
        views: dict[str, Path] = {}
        for slot, panel in zip(slots, panels):
            left = int(round(panel.centre_x - side / 2))
            top = int(round(centre_y - side / 2))
            # Take only this panel's own pixels. The shared window is wider than the gap between panels, so
            # pasting the whole window would drag a sliver of the neighbouring view into this tile; and filling
            # the rest with the sheet's background keeps Image.crop()'s black out-of-bounds padding out of the
            # frame, because black is not background as far as the node's automatic masking is concerned.
            tile = Image.new("RGB", (side, side), background)
            x0 = max(panel.x0, left, 0)
            x1 = min(panel.x1 + 1, left + side, width)
            y0 = max(panel.y0, top, 0)
            y1 = min(panel.y1 + 1, top + side, height)
            if x1 > x0 and y1 > y0:
                tile.paste(sheet.crop((x0, y0, x1, y1)), (x0 - left, y0 - top))
            path = out_dir / f"{slot}.png"
            tile.save(path)
            views[slot] = path
        # The rig needs the two side views to be opposite sides; the generator does not always oblige.
        mirror_side_views(views, warnings)
    return views, warnings


def rig_frames(slots, fov_degrees: float, distance: float = 3.0) -> list[dict]:
    """Camera-to-world frames for the rig Pixal3DMultiViewConditioning builds.

    The node places each camera at (sin az, -cos az, sin el) * distance looking back at the origin, and re-bases
    the rig on the first connected view, so a row of front/left/back/right is described by exactly those
    azimuths at elevation 0. `camera_angle_x` is in radians, like the rest of the multi-view input.
    """
    from .multiview import SLOT_AZIMUTH

    frames = []
    for slot in slots:
        az = math.radians(SLOT_AZIMUTH[slot])
        right = (math.cos(az), math.sin(az), 0.0)
        back = (math.sin(az), -math.cos(az), 0.0)
        up = (back[1] * right[2] - back[2] * right[1],
              back[2] * right[0] - back[0] * right[2],
              back[0] * right[1] - back[1] * right[0])
        frames.append({
            "file_path": f"{slot}.png",
            "name": slot,
            "camera_angle_x": math.radians(fov_degrees),
            "transform_matrix": [
                [right[0], up[0], back[0], back[0] * distance],
                [right[1], up[1], back[1], back[1] * distance],
                [right[2], up[2], back[2], back[2] * distance],
                [0.0, 0.0, 0.0, 1.0]],
        })
    return frames
