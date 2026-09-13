"""Pydantic request/response models. These are application-level parameters; the pipeline maps them to real backend options."""
import base64
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

_PALETTE_RE = re.compile(r"^[\w\s\-#]+$", re.UNICODE)


class MultiViewFrame(BaseModel):
    file_path: str = Field(..., description="Key into images_b64 (file name)")
    transform_matrix: list[list[float]] = Field(..., description="4x4 camera-to-world, Z-up, camera looks down -Z, +Y up")
    camera_angle_x: Optional[float] = None
    name: Optional[str] = None


class MultiViewInput(BaseModel):
    """Calibrated multi-view input for Pixal3D's inference_mv path (views + transforms.json semantics)."""
    images_b64: dict[str, str] = Field(..., description="file name -> base64 PNG/JPEG (RGBA alpha used as mask)")
    camera_angle_x: float = Field(..., gt=0.05, lt=2.8, description="horizontal FOV in radians")
    mesh_scale: float = Field(1.0, gt=0, le=10)
    frames: list[MultiViewFrame] = Field(..., min_length=2, max_length=12)
    camera_source: Literal["measured", "rig", "approximate"] = Field(
        "rig", description="'approximate' is recorded as a warning: poses are assumed, not measured")


class JobRequest(BaseModel):
    prompt: str = Field(..., min_length=3, max_length=1500)
    style: str = Field("mobile_factory")
    quality: Literal["balanced", "quality"] = "balanced"
    seed: Optional[int] = Field(None, ge=0, le=2**31 - 1)
    height_m: Optional[float] = Field(None, gt=0.001, le=1000)
    width_m: Optional[float] = Field(None, gt=0.001, le=1000)
    depth_m: Optional[float] = Field(None, gt=0.001, le=1000)
    target_triangles: Optional[int] = Field(None, ge=200, le=2_000_000)
    texture_size: Optional[Literal[256, 512, 1024, 2048, 4096]] = None
    generate_lods: bool = True
    lod_fractions: Optional[list[float]] = None
    generate_collision: bool = True
    collision_triangles: Optional[int] = Field(None, ge=12, le=5000)
    allow_quality_fallback: bool = True
    materials: Optional[str] = Field(None, max_length=300)
    palette: Optional[list[str]] = Field(None, max_length=8)
    negative_extra: Optional[str] = Field(None, max_length=300)
    reference_candidates: Optional[int] = Field(None, ge=1, le=6)
    reference_image_b64: Optional[str] = Field(None, description="Skip Qwen: base64 PNG/JPEG used as the reference")
    multiview: Optional[MultiViewInput] = None
    render_previews: bool = True
    preview_size: int = Field(512, ge=128, le=2048)
    master_texture_size: Optional[Literal[1024, 2048, 4096]] = None
    master_triangles: Optional[int] = Field(None, ge=10000, le=2_000_000)

    @field_validator("prompt", "materials", "negative_extra")
    @classmethod
    def _clean_text(cls, v):
        if v is None:
            return v
        v = " ".join(v.split())
        if any(ord(ch) < 32 for ch in v):
            raise ValueError("control characters not allowed")
        return v

    @field_validator("style")
    @classmethod
    def _style(cls, v):
        if not re.match(r"^[a-z0-9_]{1,40}$", v):
            raise ValueError("style must be a preset id (lowercase letters, digits, underscore)")
        return v

    @field_validator("palette")
    @classmethod
    def _palette(cls, v):
        if v is None:
            return v
        out = []
        for c in v:
            c = " ".join(str(c).split())
            if not (1 <= len(c) <= 40) or not _PALETTE_RE.match(c):
                raise ValueError(f"invalid palette entry: {c!r}")
            out.append(c)
        return out

    @field_validator("lod_fractions")
    @classmethod
    def _lods(cls, v):
        if v is None:
            return v
        if not (1 <= len(v) <= 4) or any(not (0.02 <= f < 1.0) for f in v):
            raise ValueError("lod_fractions must be 1-4 values in [0.02, 1.0)")
        return sorted(v, reverse=True)

    @field_validator("reference_image_b64")
    @classmethod
    def _b64(cls, v):
        if v is None:
            return v
        try:
            raw = base64.b64decode(v, validate=True)
        except Exception as e:  # noqa: BLE001
            raise ValueError("reference_image_b64 is not valid base64") from e
        if len(raw) > 40 * 1024 * 1024:
            raise ValueError("reference image too large (>40 MB)")
        if not (raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[:3] == b"\xff\xd8\xff"):
            raise ValueError("reference image must be PNG or JPEG")
        return v

    @model_validator(mode="after")
    def _mv(self):
        if self.multiview is not None:
            names = set(self.multiview.images_b64)
            for f in self.multiview.frames:
                if f.file_path not in names:
                    raise ValueError(f"multiview frame references unknown image {f.file_path!r}")
                if len(f.transform_matrix) != 4 or any(len(r) != 4 for r in f.transform_matrix):
                    raise ValueError("transform_matrix must be 4x4")
            for name, b in self.multiview.images_b64.items():
                if not re.match(r"^[\w\-. ]{1,80}$", name):
                    raise ValueError(f"invalid multiview image name {name!r}")
                JobRequest._b64(b)
        return self


class JobCreated(BaseModel):
    job_id: str
    status: str
    status_url: str
    artifacts_url: str
