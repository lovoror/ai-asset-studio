"""Unit tests for the Blender stage helpers that do not need a GPU: fill_missed correctness, tri_count, and the
UV-preserving LOD path on a synthetic textured mesh.

  python scripts/bootstrap.py --role blender        # once: installs bpy + meshoptimizer
  python -m pytest -q tests/test_blender_units.py
(bpy has no wheel for Python 3.13: use a 3.11 or 3.12 environment.)
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
pa = pytest.importorskip("blender.process_asset")


def test_fill_missed_fills_neighbours_and_stops():
    arr = np.zeros((8, 8, 3), dtype=np.float32)
    hit = np.zeros((8, 8), dtype=bool)
    arr[2:6, 2:6] = 1.0
    hit[2:6, 2:6] = True
    arr[3, 3] = 0.0
    hit[3, 3] = False  # an interior miss
    out = pa.fill_missed(arr, hit, iters=1)
    assert np.allclose(out[3, 3], 1.0)  # interior miss took its neighbours' value
    assert np.allclose(out[1, 2], 1.0) and np.allclose(out[0, 2], 0.0)  # one-texel margin only after one iteration
    out2 = pa.fill_missed(arr, hit, iters=8)
    assert np.allclose(out2[0, 0], 1.0)  # everything reachable within 8 iterations on an 8x8 image
    stacked = pa.fill_missed(np.concatenate([arr, arr * 0.5], axis=-1), hit, iters=8)
    assert np.allclose(stacked[..., :3], out2) and np.allclose(stacked[..., 3:], out2 * 0.5)


def test_tri_count_and_lod_from_asset_preserve_uvs():
    import bpy

    pa.reset_scene()
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32)
    obj = bpy.context.active_object
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.quads_convert_to_tris()
    bpy.ops.uv.smart_project(angle_limit=1.1, island_margin=0.01)
    bpy.ops.object.mode_set(mode="OBJECT")
    n = pa.tri_count(obj)
    assert n == len(obj.data.polygons) > 3000
    mat = bpy.data.materials.new("m")
    obj.data.materials.append(mat)
    info = pa.lod_from_asset(obj, n // 4)
    lod = info.pop("object")
    assert info["reached"] and abs(info["triangles"] - n // 4) <= n // 4 * 0.05
    assert lod.data.uv_layers.active is not None and lod.data.materials[0] is mat
    uv = np.empty(len(lod.data.loops) * 2, dtype=np.float32)
    lod.data.uv_layers.active.data.foreach_get("uv", uv)
    assert np.isfinite(uv).all() and uv.min() >= -1e-4 and uv.max() <= 1 + 1e-4
    # LOD geometry stays inside the source bounds (no exploded vertices)
    mn, mx = pa.bounds(obj)
    lmn, lmx = pa.bounds(lod)
    assert (lmn >= mn - 1e-3).all() and (lmx <= mx + 1e-3).all()
