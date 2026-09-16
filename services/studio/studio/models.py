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


class CustomStyle(BaseModel):
    """Style overrides. With `style == "custom"` this is the whole style (style_clause required); with a preset id, the fields
    given here replace the preset's (edit the look of a preset without losing its budgets). The clause goes into the
    reference prompt exactly like a preset's style_clause."""
    label: Optional[str] = Field(None, max_length=40)
    style_clause: Optional[str] = Field(None, min_length=10, max_length=600, description="how the object should look, e.g. 'chunky voxel-style ...'")
    background: Optional[str] = Field(None, max_length=120)
    negative_extra: Optional[str] = Field(None, max_length=300)
    target_triangles: Optional[int] = Field(None, ge=200, le=2_000_000)
    texture_size: Optional[Literal[256, 512, 1024, 2048, 4096]] = None

    @field_validator("label", "style_clause", "background", "negative_extra", mode="before")
    @classmethod
    def _clean(cls, v):
        if v is None:
            return v
        v = " ".join(str(v).split())
        if any(ord(ch) < 32 for ch in v):
            raise ValueError("control characters not allowed")
        return v or None  # an empty field means "not set", so a cleared clause resets to the preset


class JobRequest(BaseModel):
    prompt: str = Field(..., min_length=3, max_length=1500)
    style: str = Field("mobile_factory", description="a preset id from /capabilities.styles, or 'custom' with `custom_style`")
    custom_style: Optional[CustomStyle] = None
    model: Optional[str] = Field(None, pattern=r"^[a-z0-9\-_.]{2,60}$", description="image model id from /capabilities.image_models")
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
    title: Optional[str] = Field(None, max_length=120, description="library title (defaults to the prompt)")

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

    @model_validator(mode="after")
    def _custom_style_present(self):
        if self.style == "custom" and (self.custom_style is None or not self.custom_style.style_clause):
            raise ValueError("style 'custom' requires custom_style.style_clause")
        return self

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


class ImageJobRequest(BaseModel):
    """Ideation step: generate several reference-image variations of one idea (no 3D)."""
    prompt: str = Field(..., min_length=3, max_length=1500)
    style: str = Field("mobile_factory", description="a preset id from /capabilities.styles, or 'custom' with `custom_style`")
    custom_style: Optional[CustomStyle] = None
    model: Optional[str] = Field(None, pattern=r"^[a-z0-9\-_.]{2,60}$", description="image model id (default from settings)")
    variations: int = Field(4, ge=1, le=8)
    seed: Optional[int] = Field(None, ge=0, le=2**31 - 1)
    materials: Optional[str] = Field(None, max_length=300)
    palette: Optional[list[str]] = Field(None, max_length=8)
    negative_extra: Optional[str] = Field(None, max_length=300)
    height_m: Optional[float] = Field(None, gt=0.001, le=1000, description="hint for proportions; reused as default when making 3D")
    title: Optional[str] = Field(None, max_length=120)

    _clean = field_validator("prompt", "materials", "negative_extra")(JobRequest._clean_text.__func__)
    _style = field_validator("style")(JobRequest._style.__func__)
    _pal = field_validator("palette")(JobRequest._palette.__func__)
    _custom = model_validator(mode="after")(JobRequest._custom_style_present)


class AssetFromCandidateRequest(BaseModel):
    """Turn one selected variation of an image job into a 3D asset (full pipeline minus the image stage)."""
    image_job_id: str = Field(..., min_length=8, max_length=64)
    candidate: str = Field(..., pattern=r"^cand_\d{2}\.png$")
    quality: Literal["balanced", "quality"] = "balanced"
    hold: bool = Field(True, description="true: wait in the review queue; false: start as soon as the worker is free")
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
    # The 3D generator's own budget, before Blender reduces from it. The one-shot /v1/jobs request has always
    # accepted these; the two-step (image job -> asset job) path, which is what the portal uses, did not, so the
    # master budget could not be controlled there at all.
    master_texture_size: Optional[Literal[1024, 2048, 4096]] = None
    master_triangles: Optional[int] = Field(None, ge=10000, le=2_000_000)
    title: Optional[str] = Field(None, max_length=120)

    _lods = field_validator("lod_fractions")(JobRequest._lods.__func__)


class JobPatch(BaseModel):
    title: Optional[str] = Field(None, max_length=120)
    favorite: Optional[bool] = None
    archived: Optional[bool] = None
    notes: Optional[str] = Field(None, max_length=4000)
    priority: Optional[int] = Field(None, ge=-100, le=100)


class BackendNodes(BaseModel):
    """ComfyUI node-id mapping for the 3D lane. These are node IDs, not values: the value each node receives
    comes from the job (resolution <- the quality preset, face count <- the master budget, texture size <-
    master.texture_size, seed <- the job seed) and the proxy worker patches it in just before submitting.

    A field left None is not patched, so the workflow's own value stays in effect. Each role also accepts an
    explicit input key as "<node id>:<input key>" for when the default guess is wrong.

    Sent as a whole object - a partial one replaces the whole map. The image lane needs none of this: its
    prompt / negative / latent / sampler nodes are derived from the workflow graph instead (see
    comfy_worker.comfy_client.derive_sampler_graph).
    """
    branch: Optional[str] = Field(None, max_length=32, pattern=r"^\d{1,8}(:[\w.\-]+)?$", description="PrimitiveBoolean switching TRELLIS.2 / Pixal3D")
    seed: Optional[str] = Field(None, max_length=32, pattern=r"^\d{1,8}(:[\w.\-]+)?$", description="seed node; without it a retry is not reproducible")
    resolution: Optional[str] = Field(None, max_length=32, pattern=r"^\d{1,8}(:[\w.\-]+)?$", description="Trellis2UpsampleStage.target_resolution")
    decimate: Optional[str] = Field(None, max_length=32, pattern=r"^\d{1,8}(:[\w.\-]+)?$", description="DecimateMesh.target_face_count")
    texture: Optional[str] = Field(None, max_length=32, pattern=r"^\d{1,8}(:[\w.\-]+)?$", description="PrimitiveInt feeding the master texture bake")
    normal: Optional[str] = Field(None, max_length=32, pattern=r"^\d{1,8}(:[\w.\-]+)?$", description="BakeNormalMapFromMesh.resolution")
    image: Optional[str] = Field(None, max_length=32, pattern=r"^\d{1,8}(:[\w.\-]+)?$", description="LoadImage receiving the reference; only needed if there are several")
    output: Optional[str] = Field(None, max_length=32, pattern=r"^\d{1,8}(:[\w.\-]+)?$", description="the Save3DAdvanced whose file is the deliverable")


class SettingsPatch(BaseModel):
    auto_process: Optional[bool] = None
    default_image_model: Optional[str] = Field(None, pattern=r"^[a-z0-9\-_.]{2,60}$")
    default_variations: Optional[int] = Field(None, ge=1, le=8)
    default_quality: Optional[Literal["balanced", "quality"]] = None
    default_style: Optional[str] = Field(None, pattern=r"^[a-z0-9_]{1,40}$")
    default_target_triangles: Optional[int] = Field(None, ge=200, le=2_000_000)
    default_texture_size: Optional[Literal[256, 512, 1024, 2048, 4096]] = None
    style_edits: Optional[dict[str, CustomStyle]] = Field(None, description="whole map; keys are style ids or 'custom'. Send {} to clear.")
    # Generation backends. Every key here must also exist in db.DEFAULT_SETTINGS or put_settings() drops it silently.
    image_kind: Optional[Literal["local", "comfyui"]] = None
    image_url: Optional[str] = Field(None, max_length=300)
    image_workflow: Optional[str] = Field(None, max_length=200, pattern=r"^([\w.\-]+\.json)?$")
    three_d_kind: Optional[Literal["local", "comfyui"]] = None
    three_d_url: Optional[str] = Field(None, max_length=300)
    three_d_workflow: Optional[str] = Field(None, max_length=200, pattern=r"^([\w.\-]+\.json)?$")
    three_d_multiview_workflow: Optional[str] = Field(None, max_length=200, pattern=r"^([\w.\-]+\.json)?$",
                                                      description="workflow for multi-view jobs; empty = the shipped 3d_pixal3d_multi_views.json")
    trellis2: Optional[bool] = None
    nodes: Optional[BackendNodes] = None
    # Stage worker addresses, overriding config.toml [servers]. Send {} to fall back to config.toml.
    worker_endpoints: Optional[dict[str, str]] = Field(None, description="{image,pixal3d,blender} -> 'local' or a URL")

    @field_validator("worker_endpoints")
    @classmethod
    def _worker_endpoints(cls, v):
        if v is None:
            return v
        allowed = {"image", "pixal3d", "blender"}
        if set(v) - allowed:
            raise ValueError(f"unknown stage(s): {', '.join(sorted(set(v) - allowed))}; expected {', '.join(sorted(allowed))}")
        out = {}
        for role, raw in v.items():
            s = " ".join(str(raw).split())
            if len(s) > 300:
                raise ValueError(f"{role}: address is too long")
            if s and s.lower() not in ("local", "-"):
                m = re.match(r"^(?:https?://)?([^/:?#\s]+)(?::(\d{1,5}))?(?:/.*)?$", s)
                if not m:
                    raise ValueError(f"{role}: must be 'local', 'host:port' or an http(s) URL")
                if m.group(2) and not 1 <= int(m.group(2)) <= 65535:
                    raise ValueError(f"{role}: port out of range")
            out[role] = s
        return out

    @field_validator("image_url", "three_d_url")
    @classmethod
    def _backend_url(cls, v):
        # None must stay None: api.py dumps with exclude_none=True, so returning "" here would write an empty
        # string over the stored address whenever a client omits the field. "" is only produced when the
        # caller explicitly sends an empty string, which means "clear it".
        if v is None:
            return None
        v = v.strip().rstrip("/")
        if not v:
            return ""
        m = re.match(r"^https?://([^/:?#]+)(?::(\d{1,5}))?(?:/.*)?$", v)
        if not m:
            raise ValueError("must look like http://host:port")
        host, port = m.group(1), m.group(2)
        if port and not 1 <= int(port) <= 65535:
            raise ValueError("port out of range")
        # Not a security boundary - whoever can PUT /v1/settings is already authenticated and can submit jobs.
        # This just stops the worker container from being pointed at the cloud metadata endpoint by accident.
        if host.lower().startswith("169.254.") or host.lower() in ("metadata.google.internal", "metadata"):
            raise ValueError("link-local / metadata addresses are not allowed")
        return v

    @field_validator("style_edits")
    @classmethod
    def _style_edit_keys(cls, v):
        if v is None:
            return v
        if len(v) > 64:
            raise ValueError("too many style edits")
        for k in v:
            if not re.match(r"^[a-z0-9_]{1,40}$", k):
                raise ValueError(f"invalid style id {k!r}")
        return v


class JobCreated(BaseModel):
    job_id: str
    status: str
    status_url: str
    artifacts_url: str


class BackendTest(BaseModel):
    """Connectivity test for one address. `target` picks what is being probed:

    * `worker`  - a stage worker (`role`), i.e. the process that runs the stage.
    * `comfyui` - a ComfyUI server reached *through* that stage's worker, because that worker is what will talk
      to it. The values are the ones currently in the form, so an address can be tested before it is saved.
    """
    target: Literal["worker", "comfyui"]
    role: Literal["image", "pixal3d", "blender"] = "image"
    url: Optional[str] = Field(None, max_length=300)
    workflow: Optional[str] = Field(None, max_length=200)

    @field_validator("url")
    @classmethod
    def _url(cls, v):
        if v is None:
            return v
        v = v.strip()
        if not v:
            return ""
        if not re.match(r"^(?:https?://)?[^/:?#\s]+(?::\d{1,5})?(?:/.*)?$", v):
            raise ValueError("must look like http://host:port")
        return v
