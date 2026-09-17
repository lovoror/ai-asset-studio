"""The turnaround-sheet splitter: panel segmentation, dropping the panel the model paints from its own source,
and the one-window crop that keeps the views at a consistent scale.

No GPU and no model: the sheets are synthesised with Pillow, which is what the control plane already installs
for thumbnails.
"""
import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "studio"))

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402

from studio import sheet as sh  # noqa: E402

BG = (200, 200, 198)


def sheet_image(size, boxes, colour=(60, 120, 90)):
    """A plain sheet with one filled rectangle per panel."""
    im = Image.new("RGB", size, BG)
    draw = ImageDraw.Draw(im)
    for (x0, y0, x1, y1) in boxes:
        draw.rectangle((x0, y0, x1, y1), fill=colour)
    return im


# An asymmetric outline, so that mirroring it actually changes pixels - a rectangle would not, and the side-pair
# logic would then look like a no-op. A wedge (measured in var/pick_shape.py) separates the two readings by 0.51,
# where a hexagon only separates them by 0.07 and would land inside SIDE_PAIR_MARGIN as "ambiguous".
SHAPE = [(-24, 16), (-24, -16), (24, 16)]


def shaped_sheet(size, centres, mirror=()):
    """A sheet with the asymmetric shape at each centre; `mirror` holds the indices drawn mirrored."""
    im = Image.new("RGB", size, BG)
    draw = ImageDraw.Draw(im)
    for i, (cx, cy) in enumerate(centres):
        pts = [(-x, y) if i in mirror else (x, y) for x, y in SHAPE]
        draw.polygon([(cx + x, cy + y) for x, y in pts], fill=(60, 120, 90))
    return im


def save(im, path):
    im.save(path)
    return path


# --------------------------------------------------------------------------- segmentation

def test_finds_one_panel_per_object(tmp_path):
    im = sheet_image((900, 300), [(40, 80, 200, 260), (380, 90, 520, 250), (700, 85, 860, 255)])
    panels = sh.find_panels(im)
    assert len(panels) == 3
    assert [(p.x0, p.x1) for p in panels] == [(40, 200), (380, 520), (700, 860)]
    # the edge strength either side of a boundary is what the row span is read from, so it lands a pixel or so
    # outside the object; the box only has to cover it, and a pixel of backdrop around an object is harmless
    assert all(p.y0 <= y0 and p.y1 >= y1 for p, (_, y0, _, y1) in
               zip(panels, [(40, 80, 200, 260), (380, 90, 520, 250), (700, 85, 860, 255)]))
    assert all(p.y0 >= 77 and p.y1 <= 263 for p in panels)


def test_panels_do_not_share_one_backdrop():
    """The regression this whole segmentation exists for: the panels do not have to share a background.

    Measured on a real sheet, the four generated views sat on a grey sweep while the panel the model painted
    from its own source was white. Reading one sheet-wide backdrop off the border therefore picked the white,
    every grey column of the other four then counted as object, and the row came back as two huge panels with
    two objects in each - which is what reached the reconstruction as the "front" view. Object edges do not
    care what colour the backdrop is.
    """
    im = sheet_image((900, 300), [(40, 80, 200, 260), (700, 85, 860, 255)])
    ImageDraw.Draw(im).rectangle((380, 90, 520, 250), fill=(240, 240, 240))  # a second panel's own white
    assert [(p.x0, p.x1) for p in sh.find_panels(im)] == [(40, 200), (380, 520), (700, 860)]


def test_a_one_pixel_background_gap_does_not_split_a_panel():
    im = sheet_image((300, 200), [(50, 50, 250, 150)])
    draw = ImageDraw.Draw(im)
    draw.line((150, 50, 150, 150), fill=BG)  # a hairline of background through the middle
    assert len(sh.find_panels(im)) == 1


def test_speckles_are_not_panels():
    im = sheet_image((600, 200), [(200, 60, 400, 160)])
    ImageDraw.Draw(im).point((10, 10), fill=(0, 0, 0))
    assert len(sh.find_panels(im)) == 1


def test_a_soft_shadow_does_not_join_two_panels():
    """A floor shadow is smooth and an object's boundary is not, which is the whole basis of the split.

    Measured: a shadow's own gradient stays under 9 while a silhouette is over 20, and a threshold of 6 merges
    the two panels either side of it.
    """
    im = sheet_image((600, 200), [(40, 60, 200, 160), (400, 60, 560, 160)])
    draw = ImageDraw.Draw(im)
    for x in range(200, 400):  # a wide, soft, dark shadow lying between the two objects
        shade = BG[0] - int(8 * math.sin(math.pi * (x - 200) / 200))
        draw.line((x, 150, x, 190), fill=(shade, shade, shade - 2))
    assert len(sh.find_panels(im)) == 2


def test_a_blank_sheet_has_no_panels_and_splitting_says_so(tmp_path):
    p = save(sheet_image((400, 200), []), tmp_path / "blank.png")
    assert sh.find_panels(Image.open(p)) == []
    with pytest.raises(RuntimeError, match="no object found"):
        sh.split_sheet(p, tmp_path / "out")


# --------------------------------------------------------------------------- dropping the painted-in source

def test_drops_the_panel_the_model_paints_from_its_own_source(tmp_path):
    """The reference is resampled onto the canvas at a centred offset, so the model paints it mid-row.

    Measured on real sheets: the duplicate's centre lands within 4px of the middle while the nearest real
    view is 360px away, so the position is the tell - comparing panels by appearance only puts the duplicate
    1.26x above the runner-up, because it is a re-render rather than a copy.
    """
    boxes = [(20, 80, 140, 260), (200, 80, 320, 260), (460, 90, 540, 250), (700, 80, 820, 260)]
    p = save(sheet_image((1000, 300), boxes), tmp_path / "sheet.png")  # panel 3 is centred at x=500

    views, warnings = sh.split_sheet(p, tmp_path / "out", slots=("front", "left", "back", "right"))
    assert len(views) == 3
    assert any("dropped panel 3" in w and "middle of the sheet" in w for w in warnings)


def test_keeps_every_view_when_no_panel_sits_at_the_centre(tmp_path):
    """Four evenly spread panels are all views: the nearest is 256px off centre, well outside the tolerance.

    The last panel is drawn mirrored so the side pair really is a pair - otherwise the mirror check below would
    (correctly) flag it and this test would be measuring the wrong thing.
    """
    centres = [(256, 170), (768, 170), (1280, 170), (1792, 170)]
    p = save(shaped_sheet((2048, 340), centres, mirror=(3,)), tmp_path / "sheet.png")
    views, warnings = sh.split_sheet(p, tmp_path / "out")
    assert len(views) == 4
    assert warnings == []


def test_keeps_every_panel_when_the_count_already_matches(tmp_path):
    # every centre at least 45px off the middle of the 900px sheet, so none looks like the painted-in source
    centres = [(80, 170), (240, 170), (580, 170), (780, 170)]
    sheet = shaped_sheet((900, 340), centres, mirror=(3,))
    p = save(sheet, tmp_path / "sheet.png")
    views, warnings = sh.split_sheet(p, tmp_path / "out", slots=("front", "left", "back", "right"))
    assert len(views) == 4
    assert warnings == []


# --------------------------------------------------------------------------- one window for every view

def test_every_view_is_cut_with_the_same_window(tmp_path):
    """A car is wide from the side and narrow head-on: per-panel cropping would rescale the front view."""
    wide, narrow = (40, 100, 440, 220), (600, 120, 680, 210)
    p = save(sheet_image((800, 300), [narrow, wide]), tmp_path / "sheet.png")
    views, _ = sh.split_sheet(p, tmp_path / "out", slots=("front", "left"))

    sizes = {Image.open(v).size for v in views.values()}
    assert len(sizes) == 1
    side = sizes.pop()[0]
    # the widest panel spans 1/1.1 of the frame, which is the framing the Pixal3D node documents
    assert side == pytest.approx(400 * sh.VIEW_PAD, abs=2)


def test_the_narrow_view_stays_narrow(tmp_path):
    """If the crop re-normalised per panel, both tiles would contain the same amount of object.

    Panels are read left to right, so the narrow (head-on) one has to be listed first to land in `front`.
    """
    boxes = [(40, 120, 120, 210), (140, 100, 540, 220)]  # narrow first, then the wide side view
    p = save(sheet_image((800, 300), boxes), tmp_path / "sheet.png")
    views, _ = sh.split_sheet(p, tmp_path / "out", slots=("front", "left"))

    def ink(path):
        # the frame is black now, so "ink" is what is not black rather than what is dark
        return int((np.asarray(Image.open(path).convert("L")) > 32).sum())
    assert ink(views["left"]) > 4 * ink(views["front"])


# --------------------------------------------------------------------------- the object on black

def test_each_panel_gets_its_own_backdrop():
    """Panels do not share a background, so each panel's backdrop is read from the columns beside it."""
    im = sheet_image((900, 300), [(40, 80, 200, 260), (700, 85, 860, 255)])
    draw = ImageDraw.Draw(im)
    draw.rectangle((300, 0, 620, 300), fill=(240, 240, 240))   # one panel's own pale background
    draw.rectangle((380, 90, 520, 250), fill=(60, 120, 90))
    arr = np.asarray(im).astype(np.int16)
    panels = sh.find_panels(im)
    assert len(panels) == 3
    shades = [tuple(int(v) for v in sh.panel_backdrop(arr, p)[p.y0]) for p in panels]
    assert shades[0] == BG
    assert shades[1] == (240, 240, 240)


def test_the_object_is_composited_onto_black(tmp_path):
    """The rig wants the object on black, not on the sweep it was drawn on.

    Pixal3D's single-view path reaches the model through ImageCropToMask, whose background default is `#000000`,
    and the multi-view node feeds the same RGB both to DINOv3 and to the upsampler that shapes the mesh - so a
    tile that carries its grey sweep into the frame says "the object is a grey slab".
    """
    p = save(sheet_image((800, 300), [(40, 100, 240, 220), (400, 90, 700, 230)]), tmp_path / "sheet.png")
    views, _ = sh.split_sheet(p, tmp_path / "out", slots=("front", "left"))
    tile = Image.open(views["front"]).convert("RGB")
    assert tile.getpixel((0, 0)) == (0, 0, 0)
    assert tile.getpixel((tile.width - 1, tile.height - 1)) == (0, 0, 0)
    assert ((np.asarray(tile) == (60, 120, 90)).all(axis=2)).any(), "the object has to survive the cut-out"


def test_background_the_object_encloses_is_put_back(tmp_path):
    """A windscreen reflecting the sweep is a colour match for the backdrop behind it.

    Punching it out would put a hole through the middle of the object, so background that the object encloses
    is the object - while the see-through gap under a car stays a gap, because the flood from the panel's edge
    reaches it.
    """
    im = sheet_image((400, 300), [(60, 60, 340, 240)])
    ImageDraw.Draw(im).rectangle((140, 90, 260, 150), fill=BG)  # a "window" the colour of the sweep
    arr = np.asarray(im).astype(np.int16)
    panels = sh.find_panels(im)
    assert len(panels) == 1
    panel = panels[0]
    mask = sh.object_mask(arr, panel, sh.panel_backdrop(arr, panel))
    assert not mask[0, 0], "the backdrop the panel's box reaches outside the object is not object"
    window = mask[100 - panel.y0:145 - panel.y0, 150 - panel.x0:250 - panel.x0]
    assert window.all(), "the enclosed window has to be filled back in"


# --------------------------------------------------------------------------- opposite sides, not the same one twice

def _blob(path, mirror=False, colour=(60, 120, 90)):
    """An deliberately asymmetric car-like shape, optionally mirrored, on a plain background."""
    im = Image.new("RGB", (64, 64), BG)
    pts = [(6, 40), (14, 24), (44, 24), (52, 34), (46, 46), (10, 46)]
    if mirror:
        pts = [(64 - x, y) for x, y in pts]
    ImageDraw.Draw(im).polygon(pts, fill=colour)
    im.save(path)
    return path


def _pair_reading(left: Path, right: Path) -> tuple[float, float]:
    a = sh.signature(Image.open(left).convert("RGB"), sh.Panel(0, 63, 0, 63))
    b = sh.signature(Image.open(right).convert("RGB"), sh.Panel(0, 63, 0, 63))
    return float((a * b).sum()), float((a * b[:, ::-1]).sum())


def test_mirrors_the_right_view_when_the_model_drew_the_same_side_twice(tmp_path):
    """Measured on a real sheet: the same side twice reads +0.99 correlated unflipped against +0.76 flipped,
    and handing that pair to the rig reconstructs a malformed half of the object."""
    left = _blob(tmp_path / "left.png")
    right = _blob(tmp_path / "right.png")
    warnings: list = []

    sh.mirror_side_views({"left": left, "right": right}, warnings)
    assert any("same side twice" in w for w in warnings)
    same, flipped = _pair_reading(left, right)
    assert flipped > same, "after the fix the pair has to read as two opposite sides"


def test_leaves_a_proper_mirrored_pair_alone(tmp_path):
    left = _blob(tmp_path / "left.png")
    right = _blob(tmp_path / "right.png", mirror=True)
    before = Image.open(right).tobytes()
    warnings: list = []

    sh.mirror_side_views({"left": left, "right": right}, warnings)
    assert warnings == []
    assert Image.open(right).tobytes() == before


def test_a_missing_side_view_is_not_invented(tmp_path):
    """With only one of the two sides cut there is nothing to compare, so nothing is changed."""
    left = _blob(tmp_path / "left.png")
    warnings: list = []

    sh.mirror_side_views({"left": left}, warnings)
    assert warnings == []


def test_split_sheet_makes_the_side_pair_opposite(tmp_path):
    """End to end through the splitter: a sheet whose two side panels are the same side comes out mirrored."""
    p = save(shaped_sheet((530, 300), [(70, 150), (200, 150), (330, 150), (460, 150)]), tmp_path / "sheet.png")

    views, warnings = sh.split_sheet(p, tmp_path / "out", slots=("front", "left", "back", "right"))
    assert len(views) == 4
    assert any("same side twice" in w for w in warnings)
    assert Image.open(views["left"]).tobytes() != Image.open(views["right"]).tobytes()


# --------------------------------------------------------------------------- rig frames

def test_rig_frames_describe_the_nodes_own_rig():
    frames = sh.rig_frames(["front", "left", "back", "right"], fov_degrees=20.0)
    assert [f["file_path"] for f in frames] == ["front.png", "left.png", "back.png", "right.png"]
    assert all(f["camera_angle_x"] == pytest.approx(math.radians(20.0)) for f in frames)

    for f, slot in zip(frames, ["front", "left", "back", "right"]):
        m = f["transform_matrix"]
        x, y, z = m[0][3], m[1][3], m[2][3]
        az = math.degrees(math.atan2(x, -y)) % 360
        assert az == pytest.approx(sh.SLOT_AZIMUTH[slot], abs=1e-6)
        assert z == pytest.approx(0.0, abs=1e-9)          # the rig is level
        assert m[3] == [0.0, 0.0, 0.0, 1.0]


def test_rig_frames_round_trip_through_the_control_planes_own_reader():
    """The pipeline reads azimuths back out of these matrices; they have to survive the trip."""
    from studio import multiview as mv

    frames = sh.rig_frames(["front", "left", "back", "right"], fov_degrees=20.0)
    slots, warnings = mv.assign_slots(frames)
    assert warnings == []
    assert slots == {"front": 0, "left": 1, "back": 2, "right": 3}
