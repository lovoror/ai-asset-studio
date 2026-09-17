"""Turn a generated turnaround sheet into the per-view images the multi-view 3D lane consumes.

The 2D stage asks a Krea-2 editor for "one horizontal row of four views". What comes back is a row of copies of
the object, and there are four things to get right before those tiles can be fed to Pixal3D's rig.

* **Finding the cells.** The obvious approach - one background colour, and a cell is a run of columns that differ
  from it - does not survive the sheets this generator actually produces. It paints its own source image into the
  row (see below), and on a real sheet that source cell came back on *white* while the four generated views sat
  on a grey studio sweep with a floor gradient under them. Taking the commonest border colour therefore picks the
  white of the source cell, every grey column of the other four then reads as "object", and the whole row merges
  into two enormous cells - which is what happened: front+left came back as one 1022px tile with two cars in it,
  and that tile was fed to the reconstruction as the "front" view. So cells are found from the object's **own
  edges** instead: an object always has a sharp boundary where a sweep and its shadow are smooth, and that holds
  whatever colour the backdrop is. Measured on real sheets this finds the same five cells for every threshold
  from 10 to 30, while the shadow's soft edge (6-9) merges neighbours below that. A cell's vertical extent is the
  rows its own edges reach.

  A cell is not always the object itself, either. Measured on the next sheets out of the same workflow: one drew
  the views straight onto a single sweep, so a cell *is* the object, and the next drew each view as a framed
  studio photo inset on a plain canvas, so a cell is the frame and the object inside it is a third of its width.
  Which one it is decides where the object's backdrop is read from - outside the cell, or the frame's own
  backdrop inside it - and `is_framed` tells them apart.

* **Which cell is which view.** The model also paints the *source image itself* into the row: the reference is
  part of its context, and because the node pack resamples that source onto the canvas "at a centered offset",
  the model reproduces it in the middle of the row. That cell is not a view and is dropped. Its *position* is the
  reliable tell, not its appearance: measured on real sheets the extra cell sits within 4px of the centre while
  the nearest real view is 360px away, whereas comparing cells against the reference image only rates the
  duplicate 1.26x above the runner-up (it is a re-render, not a copy). Whatever is left goes into the slots in
  row order. The sides are named by the direction the object faces in the frame - which is the convention the
  rig's own azimuths encode - and the pair is checked afterwards, so a model that draws the same side twice gets
  the right view mirrored rather than two contradictory views.

* **The frame the rig expects: the subject on black.** This is the part that is easy to get wrong, and getting it
  wrong is what a "the car is not a car" result looks like. `Pixal3DMultiViewConditioning` documents its views as
  "square view of the object's <side>, with alpha or on a black background, framed like the rig". That is not
  cosmetic: the single-view path feeds Pixal3D through `ImageCropToMask`, whose background default is `#000000`
  and which composites `img * mask`, and the multi-view node hands its images straight to DINOv3 - there is no
  mask anywhere in the node, and the same RGB is also the composite that *guides* the NAF upsampling. So a tile
  that is a grey sweep with a car drawn on it tells the shape decoder that the object is a grey slab. Each cell
  is cut to its own object and composited onto black: the object is whatever differs from that cell's own
  backdrop, and the background it encloses is filled back in, because the object's glass is a colour match for
  the sweep behind it. The soft contact shadow the object sits in survives this (and cannot be thresholded away
  from the object's own dark underside - see `object_mask`), so a thin dark skirt can remain under the object.

* **One scale for every view.** The model drew the views consistently, so the crop must not re-normalise each
  object to its own silhouette: a car is wide from the side and narrow from the front, and per-object cropping
  would blow the front view up until it matched. Every object is cut with the *same* square window, sized from
  the widest object times `VIEW_PAD` - so the widest view spans 1/1.1 of its frame, exactly the framing
  `Pixal3DMultiViewConditioning` documents, and the narrow views stay proportionally narrower. The window is
  sized from the object's own box rather than from its cell, which is what keeps a framed photo's framing off
  the object.
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
# How strong a vertical neighbour difference has to be before a pixel counts as an object edge. Measured on two
# real sheets: the sweep's own drift and the floor shadow stay under 9, while a silhouette, a window and a wheel
# are 20+. Every threshold from 10 to 30 finds the same panels, so 12 sits in the middle of that plateau.
EDGE_TOLERANCE = 12
# How many edge pixels a column needs before it counts as part of an object. Every column crossing an object
# crosses at least its top and bottom silhouette, so 3 leaves room for one weak boundary.
EDGE_MIN_ROWS = 3
# How far a pixel has to sit from *its own cell's* backdrop before it is the object rather than the sweep. The
# shadow under the object sits between the two, so this is the high end of what still separates them.
MASK_TOLERANCE = 35
# A mask thinner than this fraction of the object's own box is not believable - a white object on a light sweep,
# or a backdrop estimate that landed on the object. The box is then used whole rather than cut down.
MASK_MIN_FRAC = 0.05
# The band at each side of a cell that its backdrop is read from, as a fraction of the cell's width. Wide enough
# that the object cannot fill it, narrow enough to stay inside the cell.
BACKDROP_BAND = 0.10
# How much of a cell's own top edge has to be a step before the cell counts as a framed photo rather than the
# object itself. A frame meets the canvas it is inset on across its whole width; an object's top edge is its
# silhouette, which is narrow there. Measured: a frame covers the whole cell, an object's top edge a quarter.
FRAME_COVERAGE = 0.75
# A row or column only counts towards the object's box when it carries this fraction of the cell's other
# dimension in object pixels. See `object_bounds`.
BOUNDS_MIN_FRAC = 0.02
# Structures thinner than this are not the object: a framed photo's torn border leaves dashes a pixel or two
# across down the cell's sides, and everything an object is made of is thicker.
MASK_OPEN_RADIUS = 1
# Panels narrower than this fraction of the sheet are noise, not views.
MIN_PANEL_FRAC = 0.02
# How close to the middle of the sheet a panel has to sit to count as the painted-in source image. Measured:
# the duplicate is ~0.2% off centre, the nearest real view 17% off, so anything in between separates cleanly.
CENTRE_TOLERANCE_FRAC = 0.05
# How much better one reading of the side pair has to be before it is believed. Measured margins: a proper
# mirrored pair wins by 0.43, the same side drawn twice by 0.30 - so 0.10 separates them with room to spare.
SIDE_PAIR_MARGIN = 0.10
# How alike two views have to be before one of them cannot be the view its slot needs. Measured across real
# sheets: two cuts of the same view read +0.98 to +1.00 with or without a mirror between them, while genuinely
# different views of the same object read +0.39 to +0.80 - and the top of that range is front against rear, the
# closest pair there is. So the gap is wide and 0.90 sits inside it.
DUP_CORRELATION = 0.90


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
    """The commonest colour around the edge of the sheet, as a last resort for a panel whose own backdrop
    cannot be sampled (its object reaches both the top and the bottom of the canvas)."""
    ring = np.concatenate([arr[:border].reshape(-1, 3), arr[-border:].reshape(-1, 3),
                           arr[:, :border].reshape(-1, 3), arr[:, -border:].reshape(-1, 3)])
    values, counts = np.unique(ring, axis=0, return_counts=True)
    return values[counts.argmax()]


def _runs(flags: np.ndarray) -> list[tuple[int, int]]:
    """The [start, end] spans of the True runs in a 1-D boolean array, ends inclusive."""
    padded = np.concatenate([[False], flags, [False]])
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(a), int(b) - 1) for a, b in zip(edges[::2], edges[1::2])]


def edge_strength(arr: np.ndarray) -> np.ndarray:
    """How sharply each pixel differs from its vertical neighbours, as a (h-2, w) map.

    An object's boundary, windows and wheels produce a large value here; a sweep, its vignette and the soft
    shadow on it produce small ones. That is what makes this usable when the sheet's backdrop is not one
    colour - the source panel comes back on white and the generated views on a grey sweep.
    """
    return np.abs(arr[2:] - arr[:-2]).max(axis=2)


def find_panels(image, tolerance: int = EDGE_TOLERANCE, min_rows: int = EDGE_MIN_ROWS,
                min_frac: float = MIN_PANEL_FRAC) -> list[Panel]:
    """Segment a sheet into panels by finding the runs of columns that contain an object's own edges."""
    arr = np.asarray(image.convert("RGB")).astype(np.int16)
    height, width, _ = arr.shape
    strength = edge_strength(arr)
    columns = (strength > tolerance).sum(axis=0) >= min_rows
    # a one-column gap inside an object (a smooth highlight crossing its silhouette) must not split it
    columns[1:-1] |= ~columns[1:-1] & columns[:-2] & columns[2:]
    panels: list[Panel] = []
    minimum = max(4, int(min_frac * width))
    for x0, x1 in _runs(columns):
        if x1 - x0 + 1 < minimum:
            continue
        # the rows this panel's own edges reach, i.e. the object's vertical extent (strength drops 2 rows)
        rows = np.flatnonzero((strength[:, x0:x1 + 1] > tolerance).any(axis=1)) + 1
        if rows.size == 0:
            continue
        panels.append(Panel(x0, x1, int(rows.min()), int(rows.max())))
    return panels


def is_framed(arr: np.ndarray, panel: Panel, tolerance: int = EDGE_TOLERANCE) -> bool:
    """Whether a cell is a framed photo of the object rather than the object itself.

    Both shapes came out of the same workflow, measured: one sheet drew the views straight onto a single sweep,
    so a cell *is* the object, and the next drew each view as a studio photo inset on a plain canvas, so a cell
    is the frame and the object inside it is a third of its width.

    The tell is the cell's own top edge. A frame meets the canvas it is inset on in a step that runs across the
    whole cell, while an object's top edge is its silhouette and is narrow there by definition - measured at a
    quarter of the cell for an object against all of it for a frame.

    Which one it is decides where the backdrop is read from, and there is no reading that works for both: a
    cell that is the object is surrounded by backdrop, and a framed photo has the object sitting on the photo's
    own backdrop with the canvas outside the frame instead.
    """
    row = panel.y0
    if not 1 <= row < arr.shape[0] - 1:
        return False  # the cell starts at the sheet's edge, so its own edge cannot be a frame
    strength = np.abs(arr[row + 1] - arr[row - 1]).max(axis=1)[panel.x0:panel.x1 + 1]
    return float((strength > tolerance).mean()) >= FRAME_COVERAGE


def panel_backdrop(arr: np.ndarray, panel: Panel, framed: bool = False, band: float = BACKDROP_BAND,
                   columns: int = 6) -> np.ndarray:
    """One cell's own backdrop, **row by row**, and from the side of the cell the object is not on.

    Per row, because the backdrop is flat in neither shape a sheet comes in: a sweep darkens towards the floor
    and a framed photo carries its own vignette, and against one flat colour the darkening under the object
    reads as part of it and comes back as a grey band along the bottom of the tile.

    From a band at the cell's own edges for a framed photo, where that band is the photo's backdrop; and from
    the columns just *outside* the cell otherwise, where the cell is the object and the band at its own edges is
    the object itself at its widest rows - reading it there would take the object's colour for the backdrop and
    cut the object's sides away.

    Returns a (height, 3) array: row `y` of the sheet uses `backdrop[y]`.
    """
    height, width, _ = arr.shape
    band_px = max(2, int(round(band * panel.width)))
    if framed:
        xs = np.concatenate([np.arange(panel.x0, min(panel.x0 + band_px, panel.x1 + 1)),
                             np.arange(max(panel.x1 + 1 - band_px, panel.x0), panel.x1 + 1)])
    else:
        xs = np.concatenate([np.arange(max(0, panel.x0 - columns), panel.x0),
                             np.arange(panel.x1 + 1, min(width, panel.x1 + 1 + columns))])
        if xs.size == 0:  # the cell spans the whole sheet
            return np.repeat(_background(arr)[None, :], height, axis=0)
    return np.median(arr[:, xs], axis=1)


def _fill_holes(mask: np.ndarray, max_passes: int = 12) -> np.ndarray:
    """Mark the background the object encloses as object too, so the silhouette is solid.

    The object's glass is a problem for a purely colour-based mask: a windscreen reflecting the sweep sits
    within `MASK_TOLERANCE` of the backdrop, so it comes out as a hole and would composite to black - a hole
    through the middle of the car. Background *enclosed* by the object is the object; background that reaches
    the edge of the panel is not (which is what keeps the see-through gap under a car a gap).
    """
    background = ~mask
    reached = np.zeros_like(mask)
    reached[0] = background[0]
    reached[-1] = background[-1]
    reached[:, 0] |= background[:, 0]
    reached[:, -1] |= background[:, -1]
    reached &= background
    for _ in range(max_passes):
        before = reached.copy()
        for y in range(1, reached.shape[0]):
            reached[y] |= reached[y - 1] & background[y]
        for y in range(reached.shape[0] - 2, -1, -1):
            reached[y] |= reached[y + 1] & background[y]
        for x in range(1, reached.shape[1]):
            reached[:, x] |= reached[:, x - 1] & background[:, x]
        for x in range(reached.shape[1] - 2, -1, -1):
            reached[:, x] |= reached[:, x + 1] & background[:, x]
        if np.array_equal(reached, before):
            break
    return mask | ~reached


def _shift(mask: np.ndarray, dy: int, dx: int) -> np.ndarray:
    """`mask` moved by (dy, dx), with the space it leaves behind filled with False."""
    out = np.zeros_like(mask)
    ys = slice(max(0, dy), mask.shape[0] + min(0, dy))
    yd = slice(max(0, -dy), mask.shape[0] + min(0, -dy))
    xs = slice(max(0, dx), mask.shape[1] + min(0, dx))
    xd = slice(max(0, -dx), mask.shape[1] + min(0, -dx))
    out[yd, xd] = mask[ys, xs]
    return out


def _open(mask: np.ndarray, radius: int = MASK_OPEN_RADIUS) -> np.ndarray:
    """Erase everything thinner than the kernel, then grow what is left back to size (a morphological opening).

    A framed photo's torn white border leaves dashes of "object" a pixel or two across down the cell's sides.
    They are thinner than anything an object is made of, so an opening takes them out; an object's own thin
    parts - a roof rack's bars - are a little wider and survive.
    """
    solid = mask
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            solid = solid & _shift(mask, dy, dx)
    grown = solid
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            grown = grown | _shift(solid, dy, dx)
    return grown


def object_mask(arr: np.ndarray, panel: Panel, backdrop: np.ndarray) -> np.ndarray:
    """Which pixels of a cell are the object rather than its backdrop, over the cell's own bounding box.

    A contrast test against the cell's own backdrop, with the background the object *encloses* filled in
    afterwards - see `_fill_holes` for why that fill is not optional (the object's glass is a colour match for
    the sweep behind it).

    Two things this deliberately does not try to do, both measured on a real sheet:

    * It does not use the object's outline to separate object from backdrop. Flooding the cell from its border
      through everything that is not an outline edge sounds better, and it does recover the glass on its own,
      but where the object's dark underside meets its own soft contact shadow there is no edge at all (measured:
      |dy| of 2 on a boundary whose two sides sit 4 units apart in colour), so the flood walks in and eats the
      object's whole lower body. It does not drop the shadow either: the shadow is bounded by the same dark
      sill above it and a strong edge below it, so it comes back as object under both tests.
    * It cannot threshold the shadow away. The shadow under the object and the object's own body overlap in
      contrast against the sweep (measured: 86-115 against 62-136), so no tolerance separates them. A sheet where
      the floor shadow is stronger than this needs a real segmentation, not a colour rule.
    """
    window = arr[panel.y0:panel.y1 + 1, panel.x0:panel.x1 + 1]
    own = backdrop[panel.y0:panel.y1 + 1][:, None, :] if backdrop.ndim == 2 else backdrop
    return _fill_holes(_open(np.abs(window - own).max(axis=2) > MASK_TOLERANCE))


def _longest_run(flags: np.ndarray) -> np.ndarray:
    """The longest unbroken run of True in each row of a 2-D boolean array."""
    counts = np.zeros(flags.shape, dtype=np.int32)
    counts[:, 0] = flags[:, 0]
    for j in range(1, flags.shape[1]):
        counts[:, j] = np.where(flags[:, j], counts[:, j - 1] + 1, 0)
    return counts.max(axis=1)


def object_bounds(mask: np.ndarray, min_frac: float = BOUNDS_MIN_FRAC) -> tuple[int, int, int, int] | None:
    """The object's own box inside its cell as (y0, y1, x0, x1) relative to the cell, or None if it is empty.

    This is what the frame is measured from, not the cell: a cell can be a whole framed photo, and framing the
    tiles on the photo would leave the object at a third of the width the rig asks for (it wants the object
    spanning about 1/1.1 of the frame at its widest).

    A row or column only counts when it carries an unbroken run of object at least `min_frac` of the cell's
    other dimension long. That is what a cell's ragged border cannot pass: measured, a framed photo's torn
    white edge leaves a trail of specks down the cell's sides, and counting those stretches the box across the
    whole cell, dragging a streak of border into the tile and shrinking the object with it. A run is what tells
    a streak - a few pixels here and there - from the object's body.
    """
    height, width = mask.shape
    rows = np.flatnonzero(_longest_run(mask) >= max(3, min_frac * width))
    cols = np.flatnonzero(_longest_run(mask.T) >= max(3, min_frac * height))
    if rows.size == 0 or cols.size == 0:
        return None
    return int(rows[0]), int(rows[-1]), int(cols[0]), int(cols[-1])


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


def _view_signature(path: Path) -> np.ndarray:
    """A view's signature taken from the *object*, not from its frame.

    Every tile shares one window sized from the widest object, so a narrow view sits in far more black than a
    wide one: comparing whole frames would mostly be comparing how much black is around each object, and on a
    real sheet that reads a front and a rear as the same picture. The object is the only non-black thing in a
    tile - they are composited onto black precisely so that it is - so cropping to it is what makes two views
    comparable. (`mirror_side_views` compares whole tiles instead, and can: it only ever compares two views of
    the same size against each other.)
    """
    with Image.open(path) as tile:
        tile = tile.convert("RGB")
    content = tile.convert("L").point(lambda v: 255 if v > 12 else 0).getbbox()
    if content:
        tile = tile.crop(content)
    return signature(tile, Panel(0, tile.width - 1, 0, tile.height - 1))


def drop_duplicate_views(views: dict[str, Path], warnings: list) -> None:
    """Drop a view that is another view's picture, so the rig is never told a pose that is not so.

    Measured on real sheets: the model drew its "front, left, rear, right" row as a front plus the *same side
    three times*, so the panel in the rear slot was a side profile. Handed to the rig as the rear pose, that says
    the object's back looks like its side, and the reconstruction resolves it into a deformed back. Two views
    that are the same picture - up to a mirror, which for a left-right symmetric object carries the same
    information - hold one view's worth of information between them, so the second one is dropped and the rig is
    rebuilt from the rest. The view is deleted rather than left on disk, so the directory keeps matching what the
    stage actually fed the reconstruction.

    The (left, right) pair is deliberately not checked: it is *expected* to be a mirrored pair, and
    `mirror_side_views` has already dealt with it by the time this runs.
    """
    if len(views) < 2:
        return
    signatures = {slot: _view_signature(path) for slot, path in views.items()}
    kept: list[str] = []
    for slot in VIEW_SLOTS:
        if slot not in views:
            continue
        twin = None
        for other in kept:
            if {slot, other} == {"left", "right"}:
                continue
            same = float((signatures[slot] * signatures[other]).sum())
            flipped = float((signatures[slot] * signatures[other][:, ::-1]).sum())
            if max(same, flipped) >= DUP_CORRELATION:
                twin = (other, max(same, flipped))
                break
        if twin is None:
            kept.append(slot)
            continue
        other, score = twin
        views.pop(slot).unlink(missing_ok=True)
        warnings.append(f"the sheet's {slot} panel is the same picture as its {other} one ({score:+.2f} "
                        f"correlated), so it cannot be the {slot} view the rig needs; it was left out and the "
                        f"reconstruction runs on {len(views)} view(s)")


def split_sheet(sheet_path: Path, out_dir: Path,
                slots: tuple[str, ...] = VIEW_SLOTS) -> tuple[dict[str, Path], list[str]]:
    """Cut a turnaround sheet into one square image per slot. Returns ({slot: path}, warnings)."""
    if Image is None:
        raise RuntimeError("splitting a turnaround sheet needs Pillow, which the control plane installs "
                           "for thumbnails; install it on the machine running the control plane")
    warnings: list[str] = []
    with Image.open(sheet_path) as sheet:
        sheet = sheet.convert("RGB")
        width = sheet.width
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

        # What each surviving cell is showing: the object, and the box the object occupies inside it. The frame
        # is measured from those boxes rather than from the cells, because a cell is not always the object.
        pixels = np.asarray(sheet).astype(np.int16)
        pieces: list[tuple[Panel, np.ndarray, tuple[int, int, int, int]]] = []
        for panel in panels:
            framed = is_framed(pixels, panel)
            mask = object_mask(pixels, panel, panel_backdrop(pixels, panel, framed=framed))
            box = object_bounds(mask)
            share = 0.0 if box is None else mask[box[0]:box[1] + 1, box[2]:box[3] + 1].mean()
            if framed and share < MASK_MIN_FRAC:
                # a "frame" that turns out to have no object on it was not a frame: the cell's top edge is a
                # full-width step simply because the object itself has a flat top (a crate, a wall, a book), so
                # the backdrop is outside the cell after all
                mask = object_mask(pixels, panel, panel_backdrop(pixels, panel, framed=False))
                box = object_bounds(mask)
                share = 0.0 if box is None else mask[box[0]:box[1] + 1, box[2]:box[3] + 1].mean()
            if box is None:
                warnings.append(f"nothing object-shaped was found in the panel at x {panel.x0}..{panel.x1}; "
                                f"it was left out")
                continue
            if share < MASK_MIN_FRAC:
                warnings.append(f"only {share * 100:.1f}% of the object found in the panel at x {panel.x0} "
                                f"differs from that panel's own backdrop; it was used whole rather than risk "
                                f"cutting the object away, but it may carry its backdrop into the reconstruction")
            pieces.append((panel, mask, box))
        if not pieces:
            raise RuntimeError(f"{sheet_path.name}: no object found on the sheet (is it blank?)")

        # one square window for every view, from the widest *object*, so relative scale survives
        side = int(math.ceil(max(max(bx1 - bx0 + 1, by1 - by0 + 1)
                                 for _, _, (by0, by1, bx0, bx1) in pieces) * VIEW_PAD))
        # in sheet coordinates: the boxes are relative to their own cell, and the cells sit at different x
        centre_y = float(np.median([panel.y0 + (by0 + by1) / 2 for panel, _, (by0, by1, _, _) in pieces]))
        out_dir.mkdir(parents=True, exist_ok=True)
        views: dict[str, Path] = {}
        for slot, (panel, mask, (by0, by1, bx0, bx1)) in zip(slots, pieces):
            left = int(round(panel.x0 + (bx0 + bx1 + 1) / 2 - side / 2))
            top = int(round(centre_y - side / 2))
            # The rig wants the object on black (see the module docstring), cut to its own silhouette: only the
            # object's own box is taken, so a neighbouring view or a cell's ragged border cannot leak into the
            # tile, and the sweep inside that box would otherwise be read as part of the object. Image.paste
            # clips the half of the window that falls outside the sheet for us.
            window = pixels[panel.y0 + by0:panel.y0 + by1 + 1, panel.x0 + bx0:panel.x0 + bx1 + 1]
            sub = mask[by0:by1 + 1, bx0:bx1 + 1]
            if sub.mean() < MASK_MIN_FRAC:
                sub = np.ones_like(sub)
            cut = Image.fromarray((window * sub[..., None]).astype(np.uint8))
            tile = Image.new("RGB", (side, side), (0, 0, 0))
            tile.paste(cut, (panel.x0 + bx0 - left, panel.y0 + by0 - top))
            path = out_dir / f"{slot}.png"
            tile.save(path)
            views[slot] = path
        # The rig needs the two side views to be opposite sides; the generator does not always oblige.
        mirror_side_views(views, warnings)
        # And a view that is another view's picture is not the view its slot needs - the model has already been
        # caught drawing the same side three times and leaving the rear slot holding a side profile.
        drop_duplicate_views(views, warnings)
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
