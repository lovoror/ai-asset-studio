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

export interface Settings {
  auto_process: boolean;
  default_variations: number;
  default_quality: "balanced" | "quality";
  default_style: string;
  default_target_triangles: number;
  default_texture_size: number;
}

export interface Example {
  id: string;
  title: string;
  prompt: string;
  style: string;
  height_m?: number;
  target_triangles?: number;
}

export const TOKEN_KEY = "as-token";
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
  createImageJob: (body: Record<string, unknown>) => req<{ job_id: string; status: string }>("POST", "/v1/image-jobs", body),
  createAssetJob: (body: Record<string, unknown>) => req<{ job_id: string; status: string }>("POST", "/v1/asset-jobs", body),
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

export const fmtDuration = (s?: number | null) => {
  if (s == null) return "–";
  if (s < 90) return `${Math.round(s)} s`;
  const m = Math.floor(s / 60);
  return `${m} min ${Math.round(s - m * 60)} s`;
};
export const fmtAgo = (ts: number) => {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString();
};
export const fmtInt = (n?: number | null) => (n == null ? "–" : n.toLocaleString());
export const stageLabel: Record<string, string> = {
  queued: "Queued",
  held: "Held for review",
  reference: "Generating image",
  pixal3d: "Building 3D model",
  blender: "Optimising & baking",
  validate: "Validating",
  package: "Packaging",
  done: "Done",
  cancelled: "Cancelled",
};
