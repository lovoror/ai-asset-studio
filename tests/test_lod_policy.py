"""The Blender stage's LOD policy: which reduced levels are worth exporting, and which are worth complaining about.

No GPU and no bpy - the policy is a pure function over triangle counts (services/blender/lod_policy.py), kept
out of process_asset.py so that it can be tested anywhere, on every run:

    python -m pytest -q tests

The numbers in the tests are real measurements from the atlas-baked car asset in var/jobs (see the LOD comment
above `lod_from_asset` in process_asset.py, and var/lod_fractions_sweep.py).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))

from blender import lod_policy as lp  # noqa: E402


def test_skips_a_level_that_barely_shrank():
    """LOD2 of the car: 10,451 triangles against LOD1's 11,348 - 7.9% for another 9.3 MB copy of the textures."""
    verdict, why = lp.lod_verdict(10451, 4852, 11348)
    assert verdict == "skip"
    assert "7.9% below the level above it" in why
    assert "near-duplicate" in why


def test_exports_a_level_that_simply_stopped_early():
    """LOD1 of that asset: 11,348 against a 9,704 budget. Shallower than asked for, still worth shipping - and
    deliberately not worth a warning, which is the whole point of having a threshold at all."""
    verdict, why = lp.lod_verdict(11348, 9704, 19409)
    assert verdict == "export"
    assert why == ""


def test_warns_only_when_the_budget_was_missed_badly():
    verdict, why = lp.lod_verdict(9000, 4000, 30000)  # 2.25x its budget
    assert verdict == "export"
    assert "225% of its 4000-triangle budget" in why


def test_both_thresholds_are_exclusive():
    # 10% below the level above still counts as a level; anything shallower does not
    assert lp.lod_verdict(9000, 4000, 10000)[0] == "export"
    assert lp.lod_verdict(9001, 4000, 10000)[0] == "skip"
    # exactly 25% over budget is quiet; one triangle more is not
    assert lp.lod_verdict(5000, 4000, 10000)[1] == ""
    assert lp.lod_verdict(5001, 4000, 10000)[1] != ""


def test_can_skip_even_the_first_level():
    """A mesh the reducer can barely touch gets no LOD at all, rather than a duplicate of LOD0."""
    verdict, why = lp.lod_verdict(19000, 9704, 19409)
    assert verdict == "skip" and "near-duplicate" in why


def test_an_exact_hit_is_exported_silently():
    for target in (9704, 5000, 12000):
        assert lp.lod_verdict(target, target, target * 3) == ("export", "")


def test_the_shipped_defaults_are_the_aggressive_chain():
    """The default asks for the LODs a game wants and lets the policy cope with what the reducer reaches; the
    whole point of this module is that asking for 0.5/0.25 no longer drags a warning behind it."""
    assert tuple(lp.DEFAULT_LOD_FRACTIONS) == (0.5, 0.25)
    assert lp.LOD_OVERSHOOT_WARN > 1.0 and lp.LOD_REDUNDANT < 1.0
    assert lp.LOD_REDUNDANT < lp.LOD_OVERSHOOT_WARN
