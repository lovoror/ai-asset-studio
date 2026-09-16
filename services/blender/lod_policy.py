"""The Blender stage's LOD policy: which levels to export, and when to say something about it.

Kept out of process_asset.py so it can be imported and tested without bpy (see tests/test_lod_policy.py).
The numbers behind it come from measuring the UV-preserving reducer on real atlas-baked assets
(var/lod_fractions_sweep.py); the summary is above `lod_from_asset` in process_asset.py.
"""
from __future__ import annotations

# Fractions of the optimized asset, for the LOD chain a job asks for by default.
DEFAULT_LOD_FRACTIONS = (0.5, 0.25)
# A level landing within this multiple of its budget is simply what the reducer could do on this mesh. Its real
# triangle count is recorded either way (outputs.lods / the manifest); only a larger miss is worth a warning.
LOD_OVERSHOOT_WARN = 1.25
# A level landing within this multiple of the level above it is dropped rather than exported: every LOD carries
# its own copy of the shared texture set, so a few percent of triangles costs another multi-MB .glb (measured:
# 10,350 triangles against 11,177, for a second 9.3 MB file).
LOD_REDUNDANT = 0.90


def lod_verdict(triangles: int, target: int, level_above: int) -> tuple[str, str]:
    """What to do with one reduced LOD level: ("skip"|"export", message).

    "skip" when the level is not meaningfully below the level it was derived from - it would ship a near-duplicate
    copy of the shared textures for a few percent of triangles. Otherwise "export", and the message is non-empty
    only when the budget was missed badly enough to be worth reporting: a level that merely stopped a little early
    is what the reducer could do on this mesh, not something the caller did wrong.
    """
    if triangles > level_above * LOD_REDUNDANT:
        return "skip", (f"only {100 * (1 - triangles / level_above):.1f}% below the level above it "
                        f"({level_above} triangles); the mesh has no UV-preserving collapse left, so this "
                        f"level would be a near-duplicate")
    if triangles > target * LOD_OVERSHOOT_WARN:
        return "export", (f"the mesh has no UV-preserving collapse left above this, so the level is "
                          f"{triangles / target:.0%} of its {target}-triangle budget")
    return "export", ""
