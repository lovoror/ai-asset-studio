"""The turnaround-sheet splitter: panel segmentation, dropping the panel the model paints from its own source,
and the one-window crop that keeps the views at a consistent scale.

No GPU and no model: the sheets are synthesised with Pillow, which is what the control plane already installs
for thumbnails.
"""
import math
import sys
from pathlib import Path

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


def save(im, path):
    im.save(path)
    return path


# --------------------------------------------------------------------------- segmentation

def test_finds_one_panel_per_object(tmp_path):
    im = sheet_image((900, 300), [(40, 80, 200, 260), (380, 90, 520, 250), (700, 85, 860, 255)])
    panels = sh.find_panels(im)
    assert len(panels) == 3
    assert [(p.x0, p.x1) for p in panels] == [(40, 200), (380, 520), (700, 860)]
    assert all(p.y0 >= 80 and p.y1 <= 260 for p in panels)


def test_a_one_pixel_background_gap_does_not_split_a_panel():
    im = sheet_image((300, 200), [(50, 50, 250, 150)])
    draw = ImageDraw.Draw(im)
    draw.line((150, 50, 150, 150), fill=BG)  # a hairline of background through the middle
    assert len(sh.find_panels(im)) == 1


def test_speckles_are_not_panels():
    im = sheet_image((600, 200), [(200, 60, 400, 160)])
    ImageDraw.Draw(im).point((10, 10), fill=(0, 0, 0))
    assert len(sh.find_panels(im)) == 1


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
    """Four evenly spread panels are all views: the nearest is 256px off centre, well outside the tolerance."""
    boxes = [(156, 80, 356, 260), (668, 80, 868, 260), (1180, 80, 1380, 260), (1692, 80, 1892, 260)]
    p = save(sheet_image((2048, 340), boxes), tmp_path / "sheet.png")
    views, warnings = sh.split_sheet(p, tmp_path / "out")
    assert len(views) == 4
    assert warnings == []


def test_keeps_every_panel_when_the_count_already_matches(tmp_path):
    # every centre at least 45px off the middle of the 900px sheet, so none looks like the painted-in source
    sheet = sheet_image((900, 300), [(20, 80, 140, 260), (180, 80, 300, 260), (520, 80, 640, 260),
                                     (720, 80, 840, 260)])
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
        arr = Image.open(path).convert("L")
        return sum(1 for px in arr.getdata() if px < 150)
    assert ink(views["left"]) > 4 * ink(views["front"])


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
