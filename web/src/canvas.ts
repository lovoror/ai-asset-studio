// Model, geometry and persistence for the /canvas page.
//
// Deliberately *not* the zustand store: the canvas is one page's scratch space, and a pan gesture updating the
// app store would re-render every subscriber on the route. The whole document (cards, where they sit, how the
// view was framed) lives in one localStorage key, so a reload puts the workspace back as it was.
//
// Only the *type* of a reference body is borrowed from the API module, never a value: api.ts imports nothing, so
// a type-only import here keeps this module free of a runtime cycle in either direction.

import type { ImageReferenceBody } from "./api";

export const STORE_KEY = "as-canvas";
export const MIN_ZOOM = 0.25;
export const MAX_ZOOM = 2.5;
/** Mirrors ImageJobRequest.references' max_length; the API's own JSON schema is preferred when it is available. */
export const MAX_REFERENCES = 6;
/** Card footprint used to place a new card and as a fallback when measuring real cards for "fit". The height is an
    estimate of a card with a result strip under it; the page measures the real cards for "fit", so this only decides
    how much room a *new* card leaves below the previous one. */
export const CARD_W = 344;
export const CARD_H = 700;

export interface CanvasReference {
  id: string;
  /** "job" names a file the control plane already has; "upload" travels as base64. */
  kind: "job" | "upload";
  label: string;
  job_id?: string;
  /** Path inside the job directory, e.g. "artifacts/reference.png". */
  file?: string;
  /** Thumbnail for a job reference (a served URL); an upload draws from `image_b64` instead. */
  url?: string;
  /** Bare base64 (no data: prefix) - what ReferenceImage.image_b64 takes. */
  image_b64?: string;
  /** True when the bytes could not be stored in the browser: the slot is a placeholder until re-attached. */
  dropped?: boolean;
}

export interface CanvasCard {
  id: string;
  x: number;
  y: number;
  prompt: string;
  style: string;
  model: string;
  variations: number;
  /** null = leave it to the model preset (the API documents exactly that for width/height/steps/cfg). */
  width: number | null;
  height: number | null;
  steps: number | null;
  cfg: number | null;
  references: CanvasReference[];
  /** Send `finalPrompt`/`finalNegative` verbatim instead of letting the pipeline compose one. */
  verbatim: boolean;
  finalPrompt: string;
  finalNegative: string;
  /** The image job this card started, if any. */
  jobId: string | null;
  /** A submission that never became a job (422/404 from the API); a *failed job* reports through its own job. */
  error: string | null;
}

export interface CanvasView { x: number; y: number; k: number }
export interface CanvasDoc { cards: CanvasCard[]; view: CanvasView }
export type SaveResult = "ok" | "trimmed" | "failed";

export const newId = () => Math.random().toString(36).slice(2, 10);

export const emptyView = (): CanvasView => ({ x: 40, y: 40, k: 1 });

export function newReference(): CanvasReference {
  return { id: newId(), kind: "upload", label: "" };
}

/** A card with no prompt: the caller fills in the defaults it read from /capabilities. */
export function newCard(patch: Partial<CanvasCard> = {}): CanvasCard {
  return {
    id: newId(),
    x: 0,
    y: 0,
    prompt: "",
    style: "mobile_factory",
    model: "",
    variations: 4,
    width: null,
    height: null,
    steps: null,
    cfg: null,
    references: [],
    verbatim: false,
    finalPrompt: "",
    finalNegative: "",
    jobId: null,
    error: null,
    ...patch,
  };
}

/** Where to put the next card: a left-to-right grid, but stacked on the *measured* bottom of its column so two
    cards never land on top of each other, whatever height they have grown to. */
export function nextSpot(cards: CanvasCard[]): { x: number; y: number } {
  const gap = 26;
  const cols = 3;
  if (!cards.length) return { x: 40, y: 40 };
  const x = 40 + (cards.length % cols) * (CARD_W + gap);
  // Cards in this column are the ones whose left edge is nearest this slot; anything else is another column.
  const inColumn = cards.filter((c) => Math.abs(c.x - x) < CARD_W / 2);
  const y = inColumn.length ? Math.max(...inColumn.map((c) => c.y)) + CARD_H + gap : 40;
  return { x, y };
}

export const clampZoom = (k: number) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, k));

/** Zoom by `factor` while keeping the world point under (px, py) - viewport coordinates - exactly put. */
export function zoomAbout(view: CanvasView, factor: number, px: number, py: number): CanvasView {
  const k = clampZoom(view.k * factor);
  if (k === view.k) return view;
  // world = (pointer - offset) / k, so the new offset is pointer - world * k'. Keeping the same world point
  // under the pointer is what makes wheel-zoom feel anchored to the cursor instead of the origin.
  const wx = (px - view.x) / view.k;
  const wy = (py - view.y) / view.k;
  return { x: px - wx * k, y: py - wy * k, k };
}

/** Frame `boxes` (world coordinates) in a viewport of the given size, or reset when there is nothing to frame. */
export function fitView(boxes: { x: number; y: number; w: number; h: number }[], vw: number, vh: number): CanvasView {
  if (!boxes.length || vw <= 0 || vh <= 0) return emptyView();
  const pad = 48;
  const x0 = Math.min(...boxes.map((b) => b.x));
  const y0 = Math.min(...boxes.map((b) => b.y));
  const x1 = Math.max(...boxes.map((b) => b.x + b.w));
  const y1 = Math.max(...boxes.map((b) => b.y + b.h));
  const k = clampZoom(Math.min((vw - pad * 2) / Math.max(1, x1 - x0), (vh - pad * 2) / Math.max(1, y1 - y0)));
  // centre the union: whatever is left over after scaling is split evenly
  return { x: (vw - (x1 - x0) * k) / 2 - x0 * k, y: (vh - (y1 - y0) * k) / 2 - y0 * k, k };
}

/** Read the stored document. Anything malformed falls back to the defaults rather than breaking the page. */
export function loadDoc(): CanvasDoc {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (!raw) return { cards: [], view: emptyView() };
    const parsed = JSON.parse(raw) as Partial<CanvasDoc>;
    const cards = (Array.isArray(parsed.cards) ? parsed.cards : []).map((c) => newCard({
      ...c,
      references: (Array.isArray(c?.references) ? c.references : []).map((r) => ({ ...newReference(), ...r })),
    }));
    const v = parsed.view;
    const view = v && typeof v.x === "number" && typeof v.y === "number" && typeof v.k === "number"
      ? { x: v.x, y: v.y, k: clampZoom(v.k) }
      : emptyView();
    return { cards, view };
  } catch {
    return { cards: [], view: emptyView() };
  }
}

/** The same document without the uploaded bytes: what a full localStorage keeps. Exported because the caller has to
    apply it to its own state as well, or every later save would report the same trim again. */
export function stripUploads(doc: CanvasDoc): CanvasDoc {
  return {
    ...doc,
    cards: doc.cards.map((c) => ({
      ...c,
      references: c.references.map((r) => (r.image_b64 ? { ...r, image_b64: undefined, dropped: true } : r)),
    })),
  };
}

/** Persist the document. Uploads are the only large part of it, so a full quota costs the bytes, not the canvas. */
export function saveDoc(doc: CanvasDoc): SaveResult {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(doc));
    return "ok";
  } catch {}
  // A browser quota is around 5 MB and a photo is a megabyte of base64, so drop the *bytes* and keep the rest:
  // the slot stays on its card, marked, and the card says so instead of generating from something that is gone.
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(stripUploads(doc)));
    return "trimmed";
  } catch {
    return "failed";
  }
}

/** The reference an upload contributes: bare base64, which is what `ReferenceImage.image_b64` validates. */
export function dataUrlToBase64(dataUrl: string): string {
  const comma = dataUrl.indexOf(",");
  return comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl;
}

/** Is this reference usable as it stands? A dropped upload has no bytes left, and the API needs one of the two. */
export function referenceReady(r: CanvasReference): boolean {
  if (r.kind === "upload") return !!r.image_b64;
  return !!(r.job_id && r.file);
}

/** The body half of a reference: exactly one of the two shapes the API accepts. */
export function referenceBody(r: CanvasReference): ImageReferenceBody | null {
  const label = r.label.trim() || undefined;
  if (r.kind === "upload" && r.image_b64) return { image_b64: r.image_b64, ...(label ? { label } : {}) };
  if (r.kind === "job" && r.job_id && r.file) return { job_id: r.job_id, file: r.file, ...(label ? { label } : {}) };
  return null;
}
