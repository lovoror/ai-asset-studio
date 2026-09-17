// Typed client for the asset-studio API. Bearer token (optional) comes from localStorage.

export type JobStatus = "held" | "queued" | "running" | "completed" | "failed" | "cancelled";
export type JobKind = "image" | "asset";

export interface Candidate {
  file: string;
  seed: number | null;
  time_s: number | null;
  score: number | null;
  reasons: string[] | string | null;
  url: string;
  thumb: string | null;
  partial?: boolean;
}

export interface Job {
  id: string;
  kind: JobKind;
  status: JobStatus;
  stage: string;
  progress: number;
  created_at: number;
  updated_at: number;
  started_at: number | null;
  finished_at: number | null;
  request: Record<string, any>;
  settings: Record<string, any>;
  warnings: string[];
  timings: Record<string, number>;
  error: { stage?: string; message?: string; details?: any } | Record<string, never> | null;
  cancel_requested: boolean;
  attempt: number;
  parent_id: string | null;
  candidate: string | null;
  title: string | null;
  favorite: boolean;
  archived: boolean;
  notes: string | null;
  priority: number;
  thumb: string | null;
  events?: { ts: number; level: string; message: string }[];
  candidates?: Candidate[];
  children?: Job[];
  parent?: Job | null;
  asset?: { optimized?: string; lods?: { file: string; triangles: number }[]; collision?: string; previews?: string[]; master?: string };
  summary?: AssetSummary;
  manifest_url?: string | null;
  position?: number;
  children_count?: number;
  candidate_count?: number;
}

export interface AssetSummary {
  triangles: number | null;
  master_triangles: number | null;
  method: string | null;
  reduction: any;
  maps: Record<string, string> | null;
  lods: { file: string; triangles: number }[];
  collision_triangles: number | null;
  resolution: number | null;
  timings_s: Record<string, number> | null;
  validation_ok: boolean | null;
  vram_mib: { reference: number | null; pixal3d: number | null };
  warnings: string[];
}

export interface ImageModel {
  id: string;
  label: string;
  tier: "fast" | "quality";
  family: string;
  est_s: number;
  description: string;
  params: { steps?: number; width?: number; height?: number };
  available: boolean | null;
  reason?: string;
}

export interface Settings {
  auto_process: boolean;
  default_image_model: string;
  default_variations: number;
  default_quality: "balanced" | "quality";
  default_style: string;
  default_target_triangles: number;
  default_texture_size: number;
  style_edits?: Record<string, StyleEditRecord>;
  // generation backends (which engine each GPU stage uses)
  image_kind: "local" | "comfyui";
  image_url: string;
  image_workflow: string;
  three_d_kind: "local" | "comfyui";
  three_d_url: string;
  three_d_workflow: string;
  trellis2: boolean;
  nodes: Record<string, string>;
  // stage worker addresses, overriding config.toml [servers]
  worker_endpoints: Record<string, string>;
}

/** One reason new work cannot start, as returned by /v1/readiness and the 422 from a refused submission. */
export interface GateProblem {
  code: "worker_unreachable" | "backend_unreachable" | "backend_not_configured" | string;
  role?: string;
  lane?: string;
  url?: string;
  missing?: string;
  detail?: string;
}

export interface GateState { ready: boolean; problems: GateProblem[] }

export interface ProbeResult {
  ok: boolean;
  target: "worker" | "comfyui";
  role?: string;
  url?: string;
  detail?: string;
  elapsed_ms?: number;
  cached?: boolean;
}

export interface StyleEditRecord {
  label?: string; style_clause?: string; background?: string; negative_extra?: string; target_triangles?: number; texture_size?: number;
}

export interface Example {
  id: string;
  title: string;
  prompt: string;
  style: string;
  height_m?: number;
  target_triangles?: number;
}

/** One conditioning image for an image job: exactly one of `image_b64` or `job_id` + `file` (the API refuses both
    or neither). `job_id` names a file the control plane already has, which is how one canvas card feeds another
    without pushing megabytes through the browser. */
export interface ImageReferenceBody {
  label?: string;
  image_b64?: string;
  job_id?: string;
  file?: string;
}

/** POST /v1/image-jobs. Only `prompt` is required; an omitted field keeps the pipeline's own value, which is why
    the canvas leaves width/height/steps/cfg out until the user pins one.
    A `type` and not an `interface` on purpose: TypeScript gives a type alias an implicit index signature, so the
    canvas can annotate its body with this (and get the excess-property check) while `createImageJob` still takes
    the loose Record the older call sites pass. */
export type ImageJobBody = {
  prompt: string;
  style?: string;
  custom_style?: Record<string, unknown>;
  model?: string;
  variations?: number;
  seed?: number;
  materials?: string;
  palette?: string[];
  negative_extra?: string;
  height_m?: number;
  title?: string;
  references?: ImageReferenceBody[];
  workflow?: string;
  width?: number;
  height?: number;
  steps?: number;
  cfg?: number;
  final_prompt?: string;
  final_negative_prompt?: string;
};

/** POST /v1/prompt/preview: the instruction the pipeline would actually send, plus the parts it came from. */
export interface PromptPreview {
  prompt: string;
  negative_prompt: string;
  template: {
    subject?: string;
    style?: string;
    style_clause?: string;
    materials?: string | null;
    palette?: string[] | null;
    dimensions?: string[] | null;
    background?: string;
    view?: string;
    lighting?: string;
    constraints?: string;
  };
  style_label?: string | null;
}

export interface PromptPreviewBody {
  prompt: string;
  style: string;
  custom_style?: Record<string, unknown>;
  materials?: string;
  palette?: string[];
  negative_extra?: string;
  height_m?: number;
  width_m?: number;
  depth_m?: number;
}

export interface JobCreated { job_id: string; status: string }

export const TOKEN_KEY = "as-token";
export type Lang = "en" | "zh";
/** Where the UI language is remembered. Owned here because api.ts is the shared leaf module (it imports
    nothing itself), so both the store and i18n.ts can use it without an import cycle. */
export const LANG_KEY = "as-lang";
export const token = () => {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
};
export const withToken = (url: string) => {
  const t = token();
  if (!t) return url;
  return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(t);
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const t = token();
  if (t) headers.Authorization = "Bearer " + t;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      msg = typeof j.detail === "string" ? j.detail : j.detail ? JSON.stringify(j.detail) : j.error || msg;
    } catch {}
    throw new ApiError(r.status, msg);
  }
  if (r.status === 204) return undefined as T;
  return (await r.json()) as T;
}

export const api = {
  health: () => req<any>("GET", "/health"),
  capabilities: () => req<any>("GET", "/capabilities"),
  examples: () => req<{ items: Example[] }>("GET", "/v1/examples"),
  settings: () => req<Settings>("GET", "/v1/settings"),
  putSettings: (s: Partial<Settings>) => req<Settings>("PUT", "/v1/settings", s),
  readiness: () => req<{ workers: Record<string, ProbeResult>; endpoints: Record<string, string>;
                         backends: Record<string, ProbeResult>; create_image: GateState; create_asset: GateState }>(
    "GET", "/v1/readiness"),
  testBackend: (body: { target: "worker" | "comfyui"; role: string; url?: string; workflow?: string }) =>
    req<ProbeResult>("POST", "/v1/backends/test", body),
  promptPreview: (body: PromptPreviewBody) => req<PromptPreview>("POST", "/v1/prompt/preview", body),
  createImageJob: (body: Record<string, unknown>) => req<JobCreated>("POST", "/v1/image-jobs", body),
  createAssetJob: (body: Record<string, unknown>) => req<JobCreated>("POST", "/v1/asset-jobs", body),
  job: (id: string, events = 30) => req<Job>("GET", `/v1/jobs/${id}?events=${events}`),
  library: (p: { kind?: string; status?: string; q?: string; favorite?: boolean; archived?: boolean; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(p).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    });
    return req<{ items: Job[]; limit: number; offset: number }>("GET", "/v1/library?" + qs.toString());
  },
  queue: () => req<{ items: Job[]; gpu: any; settings: Settings }>("GET", "/v1/queue"),
  cancel: (id: string) => req<any>("POST", `/v1/jobs/${id}/cancel`),
  release: (id: string) => req<any>("POST", `/v1/jobs/${id}/release`),
  hold: (id: string) => req<any>("POST", `/v1/jobs/${id}/hold`),
  retry: (id: string, body?: { from_stage?: string; optimize?: Record<string, unknown> }) => req<any>("POST", `/v1/jobs/${id}/retry`, body),
  patch: (id: string, body: Record<string, unknown>) => req<Job>("PATCH", `/v1/jobs/${id}`, body),
  remove: (id: string, purge = false) => req<any>("DELETE", `/v1/jobs/${id}?purge=${purge}`),
  log: async (id: string, stage: string) => {
    const r = await fetch(withToken(`/v1/jobs/${id}/logs/${stage}?tail=12000`));
    return r.text();
  },
  artifacts: (id: string) => req<{ artifacts: { name: string; bytes: number; url: string }[] }>("GET", `/v1/jobs/${id}/artifacts`),
};

export const fmtInt = (n?: number | null) => (n == null ? "–" : n.toLocaleString());
// fmtAgo / fmtDuration / stageLabel moved to i18n.ts: they are presentation text and need the current language.
