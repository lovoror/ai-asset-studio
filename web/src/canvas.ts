// Model, geometry and persistence for the /canvas page: a small typed node graph, not a list of forms.
//
// Deliberately *not* the zustand store: the canvas is one page's scratch space, and a pan gesture updating the
// app store would re-render every subscriber on the route. The whole graph and the view transform live in one
// localStorage key, so a reload puts the workspace back as it was.
//
// Everything in here is pure and browser-free: the graph operations (connect, validate, order, resolve), the
// geometry the wires are drawn from, the v1 -> v2 migration and the storage helpers. The page only renders what
// these functions return, which is what makes the interesting behaviour testable outside a browser
// (`var/canvas.check.mjs`).
//
// Only the *type* of a reference body is borrowed from the API module, never a value: api.ts imports nothing, so
// a type-only import here keeps this module free of a runtime cycle in either direction.

import type { ImageJobBody, ImageReferenceBody } from "./api";

export const STORE_KEY = "as-canvas";
/** The stored document's shape. Bumped when the model changed from cards to nodes; `loadDoc` migrates a v1 board. */
export const DOC_VERSION = 2;
export const MIN_ZOOM = 0.25;
export const MAX_ZOOM = 2.5;
/** Mirrors ImageJobRequest.references' max_length. The API's own JSON schema is preferred when it is available. */
export const MAX_REFERENCES = 6;
/** `ReferenceImage.label` is capped at 40 characters and is only ever shown back to the user, so a long title is
    trimmed rather than refused by the API. */
export const clampLabel = (s: string) => s.slice(0, 40);
/** Where an image job keeps the candidates a reference may point at.
 *
 * `GET /v1/jobs/{id}` reports a candidate's `file` as a bare name (`cand_00.png`) and serves it from
 * `artifacts/reference_candidates/`; a reference `file` is resolved against the job's `artifacts/` directory, so
 * this prefix is what makes the two line up. Lives here rather than in the picker because both the picker and a
 * generate node feeding another generate node have to spell the same path. */
export const candidatePath = (name: string) => `artifacts/reference_candidates/${name}`;

// -------------------------------------------------------------------------------------------------- the port tables

export type PortType = "prompt" | "style" | "image" | "asset";
export type NodeKind = "prompt" | "style" | "image" | "generate" | "asset";
export type NodeDir = "in" | "out";

export interface PortDef {
  key: string;
  type: PortType;
  /** i18n key, so a port's name is translated like every other label. */
  label: string;
  /** Several wires may land here; their order is the order they were made in. One wire otherwise. */
  many?: boolean;
  /** Without it the node cannot do its job - not "it defaults", which would be a silent surprise. */
  required?: boolean;
}

/** What a kind takes and gives. A wire is legal exactly when the two types are the same, and the tables are what
    the whole page is driven from: the port rows it draws, the validation, and the resolution below.
 *
 * `asset` is a type with no consumer yet: an asset node produces one, and nothing in these five kinds takes one.
 * It is modelled anyway (rather than leaving the node without an output) because a port's type is what a future
 * consumer would attach to, and the refusal message says "nothing takes an asset" instead of pretending. */
export const PORTS: Record<NodeKind, { in: PortDef[]; out: PortDef[] }> = {
  prompt: { in: [], out: [{ key: "out", type: "prompt", label: "canvas.port.outPrompt" }] },
  style: { in: [], out: [{ key: "out", type: "style", label: "canvas.port.outStyle" }] },
  image: { in: [], out: [{ key: "out", type: "image", label: "canvas.port.outImage" }] },
  generate: {
    in: [
      { key: "prompt", type: "prompt", label: "canvas.port.inPrompt", required: true },
      { key: "style", type: "style", label: "canvas.port.inStyle" },
      { key: "images", type: "image", label: "canvas.port.inImages", many: true },
    ],
    out: [{ key: "out", type: "image", label: "canvas.port.outImage" }],
  },
  asset: {
    in: [{ key: "images", type: "image", label: "canvas.port.inGenerateImage", required: true }],
    out: [{ key: "out", type: "asset", label: "canvas.port.outAsset" }],
  },
};

export const NODE_KINDS: NodeKind[] = ["prompt", "style", "image", "generate", "asset"];

export function portDef(kind: NodeKind, dir: NodeDir, key: string): PortDef | null {
  return PORTS[kind][dir].find((p) => p.key === key) || null;
}

/** Whether any kind at all accepts this type. Used to explain a refusal honestly: an output nothing can take is a
    different situation from a mismatched pair, and it is the `asset` port's situation today. */
export function acceptsAnywhere(type: PortType): boolean {
  return NODE_KINDS.some((k) => PORTS[k].in.some((p) => p.type === type));
}

// -------------------------------------------------------------------------------------------------------- the nodes

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
  /** True when the bytes could not be stored in the browser: the node is a placeholder until re-attached. */
  dropped?: boolean;
}

interface NodeBase {
  id: string;
  kind: NodeKind;
  x: number;
  y: number;
}

/** The idea, and the optional extras that go with it. Its own node because the same prompt is worth reusing
    across several generations, and because a prompt that lived on the generation node could not be. */
export interface PromptNode extends NodeBase {
  kind: "prompt";
  prompt: string;
  materials: string;
  /** One line, comma separated: the API takes up to 8 colours and this is the least fiddly way to type them. */
  palette: string;
  /** The request's `negative_extra`: appended to the style's own negative list, not a replacement for it. */
  avoid: string;
  /** The "more" disclosure, collapsed by default so the node stays small. */
  more: boolean;
}

export interface StyleNode extends NodeBase {
  kind: "style";
  style: string;
}

export interface ImageNode extends NodeBase {
  kind: "image";
  ref: CanvasReference;
}

/** The 2D generation. Its inputs come from the graph, which is the point: the prompt, the style and the
    references are separate nodes that this one reads, not fields folded into it. */
export interface GenerateNode extends NodeBase {
  kind: "generate";
  model: string;
  variations: number;
  /** null = leave it to the model preset, which is what the API documents for these four. */
  width: number | null;
  height: number | null;
  steps: number | null;
  cfg: number | null;
  /** Send `finalPrompt`/`finalNegative` verbatim instead of letting the pipeline compose one. */
  verbatim: boolean;
  finalPrompt: string;
  finalNegative: string;
  /** Which candidate of this node's job its `image` output carries. null = the first one, so a fresh result is
      already usable without the user having to pick. */
  output: string | null;
  jobId: string | null;
  /** A submission that never became a job (422/404 from the API); a *failed job* reports through its own job. */
  error: string | null;
}

/** Turn one candidate of an upstream generate node into a 3D asset. */
export interface AssetNode extends NodeBase {
  kind: "asset";
  /** Which candidate to build, or null for the upstream node's own output. */
  candidate: string | null;
  /** The 3D job this started, so the node can link to it. */
  jobId: string | null;
  error: string | null;
}

export type CanvasNode = PromptNode | StyleNode | ImageNode | GenerateNode | AssetNode;

export interface CanvasEdge {
  id: string;
  from: string;
  fromPort: string;
  to: string;
  toPort: string;
}

export interface CanvasGraph {
  nodes: CanvasNode[];
  edges: CanvasEdge[];
}

export interface CanvasView { x: number; y: number; k: number }
export interface CanvasDoc extends CanvasGraph { v: number; view: CanvasView }
export type SaveResult = "ok" | "trimmed" | "failed";

export const newId = () => Math.random().toString(36).slice(2, 10);

export function newReference(): CanvasReference {
  return { id: newId(), kind: "upload", label: "" };
}

/** A node with no content: the caller fills in what it read from /capabilities. */
export function makeNode(kind: NodeKind, patch: Partial<CanvasNode> = {}): CanvasNode {
  const base = { id: newId(), kind, x: 0, y: 0 };
  switch (kind) {
    case "prompt":
      return { ...base, kind, prompt: "", materials: "", palette: "", avoid: "", more: false, ...patch } as PromptNode;
    case "style":
      return { ...base, kind, style: "mobile_factory", ...patch } as StyleNode;
    case "image":
      return { ...base, kind, ref: newReference(), ...patch } as ImageNode;
    case "generate":
      return {
        ...base, kind, model: "", variations: 4, width: null, height: null, steps: null, cfg: null,
        verbatim: false, finalPrompt: "", finalNegative: "", output: null, jobId: null, error: null, ...patch,
      } as GenerateNode;
    default:
      return { ...base, kind: "asset", candidate: null, jobId: null, error: null, ...patch } as AssetNode;
  }
}

export const kindOf = (graph: CanvasGraph, id: string): NodeKind | null =>
  graph.nodes.find((n) => n.id === id)?.kind ?? null;

export const nodeById = (graph: CanvasGraph, id: string): CanvasNode | null =>
  graph.nodes.find((n) => n.id === id) || null;

/** The job a node started, if its kind is one that starts jobs. Saves every caller narrowing the union first. */
export const jobIdOf = (node: CanvasNode): string | null =>
  node.kind === "generate" || node.kind === "asset" ? node.jobId : null;

// ------------------------------------------------------------------------------------------------------- geometry

/** Node widths, fixed per kind because the wires are drawn from them: a port's anchor is computed from the node's
    own box rather than measured from the DOM, which keeps the edge layer a pure function of the graph and means a
    pan or a zoom never has to re-measure anything. The port rows below are laid out to match, from the same
    constants, so the two cannot drift. */
export const NODE_WIDTH: Record<NodeKind, number> = { prompt: 300, style: 260, image: 220, generate: 344, asset: 300 };
/** The drag handle bar; every port row starts below it. */
export const NODE_HEAD = 34;
/** One port row, input or output. */
export const PORT_ROW = 26;
export const PORT_TOP = 8;
/** How far inside the frame a port dot sits.
 *
 * Not centred on the edge: a node clips its own overflow (the head is full-bleed), so a dot on the edge would be
 * half clipped away - half invisible, and only half clickable, which is worse than it sounds because a click on
 * the boundary can miss the dot entirely and start a pan instead. Measured while driving the page: one node's dot
 * took the press and the next one did not, purely on where the boundary rounded. The anchor below and the dot's
 * own inline position both come from this number. */
export const PORT_INSET = 7;
/** The dot's diameter. The node positions the dot inline from PORT_INSET and this, and the anchor above is their
    sum, so the drawing and the geometry cannot drift apart. */
export const PORT_DOT = 12;

/** Which row a port sits in: the inputs first, in table order, then the outputs - the same order the node renders
    them in, which is what makes the computed anchor land on the drawn handle. */
export function portRow(kind: NodeKind, dir: NodeDir, key: string): number {
  const table = PORTS[kind];
  const list = table[dir];
  const i = Math.max(0, list.findIndex((p) => p.key === key));
  return dir === "in" ? i : table.in.length + i;
}

/** Where a wire meets its node, in world coordinates: the centre of the port's dot, inside the frame. */
export function portAnchor(node: CanvasNode, dir: NodeDir, key: string): { x: number; y: number } {
  const y = node.y + NODE_HEAD + PORT_TOP + portRow(node.kind, dir, key) * PORT_ROW + PORT_ROW / 2;
  const w = NODE_WIDTH[node.kind];
  return { x: dir === "in" ? node.x + PORT_INSET : node.x + w - PORT_INSET, y };
}

/** A wire, as a cubic that leaves its output going right and arrives at its input going right: the shape people
    read as "this flows into that", and it loops tidily when the target sits to the left of the source. */
export function edgePath(a: { x: number; y: number }, b: { x: number; y: number }): string {
  const dx = Math.max(40, Math.min(180, Math.abs(b.x - a.x) * 0.5));
  return `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
}

/** A cubic's point at t = 0.5 with mirrored control offsets is just the midpoint of its ends, which is where the
    order badge and the delete handle go. */
export const edgeMid = (a: { x: number; y: number }, b: { x: number; y: number }) =>
  ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });

/** Estimated height per kind, for *placing* a new node so it does not land on another one. The real size is
    measured from the DOM where it matters (fit and reveal); this only has to be roughly right. */
export const NODE_H_EST: Record<NodeKind, number> = { prompt: 250, style: 120, image: 210, generate: 900, asset: 300 };

const COL_W = 500;
const COLS = 3;
const ORIGIN = 60;
const GAP = 30;

/** Where to put the next node: a left-to-right grid, stacked on the estimated bottom of its column so two nodes
    never land on top of each other whatever size the ones already there have grown to. */
export function nextSpot(nodes: CanvasNode[]): { x: number; y: number } {
  if (!nodes.length) return { x: ORIGIN, y: ORIGIN };
  const x = ORIGIN + (nodes.length % COLS) * COL_W;
  const inColumn = nodes.filter((n) => Math.abs(n.x - x) < COL_W / 2);
  const y = inColumn.length ? Math.max(...inColumn.map((n) => n.y + NODE_H_EST[n.kind])) + GAP : ORIGIN;
  return { x, y };
}

// ----------------------------------------------------------------------------------------------- graph traversal

/** The wires into one input, in the order they were made (which is the order the array holds them in). */
export const inputEdges = (graph: CanvasGraph, node: string, port: string): CanvasEdge[] =>
  graph.edges.filter((e) => e.to === node && e.toPort === port);

/** The wire out of one output. There is at most one per output port by construction. */
export const outputEdges = (graph: CanvasGraph, node: string, port: string): CanvasEdge[] =>
  graph.edges.filter((e) => e.from === node && e.fromPort === port);

/** Every node that can reach `id` by following wires *backwards*, itself excluded.
 *
 * Used for the cycle check: adding source -> target closes a loop exactly when the target is already upstream of
 * the source, and "upstream" is this walk from the source. */
export function ancestors(graph: CanvasGraph, id: string): Set<string> {
  const seen = new Set<string>();
  const stack = [id];
  while (stack.length) {
    const cur = stack.pop() as string;
    for (const e of graph.edges) {
      if (e.to !== cur || seen.has(e.from)) continue;
      seen.add(e.from);
      stack.push(e.from);
    }
  }
  return seen;
}

// ----------------------------------------------------------------------------------------------- connecting wires

export type WireProblemCode = "same" | "type" | "cycle" | "many" | "duplicate" | "unknown";
export interface WireProblem { code: WireProblemCode; from: PortType; to: PortType; limit?: number }

/** Why this wire may not be made, or null when it may. Pure, so the refusals are testable and the page only has
    to turn the code into a sentence. */
export function connectionProblem(
  graph: CanvasGraph, from: string, fromPort: string, to: string, toPort: string, maxRefs: number,
): WireProblem | null {
  const a = kindOf(graph, from);
  const b = kindOf(graph, to);
  const src = a ? portDef(a, "out", fromPort) : null;
  const dst = b ? portDef(b, "in", toPort) : null;
  if (!a || !b || !src || !dst) return { code: "unknown", from: src?.type ?? "image", to: dst?.type ?? "image" };
  if (from === to) return { code: "same", from: src.type, to: dst.type };
  if (src.type !== dst.type) return { code: "type", from: src.type, to: dst.type };
  const already = graph.edges.some(
    (e) => e.from === from && e.fromPort === fromPort && e.to === to && e.toPort === toPort);
  if (already) return { code: "duplicate", from: src.type, to: dst.type };
  if (dst.many && inputEdges(graph, to, toPort).length >= maxRefs) {
    return { code: "many", from: src.type, to: dst.type, limit: maxRefs };
  }
  if (ancestors(graph, from).has(to)) return { code: "cycle", from: src.type, to: dst.type };
  return null;
}

/** Make a wire. A single-input port is *replaced* (a second style has nowhere to go and silently keeping both
    would be worse than dropping the one the user just moved away from); a `many` port appends, so the array order
    is the creation order and therefore the reference order. */
export function connect(
  graph: CanvasGraph, from: string, fromPort: string, to: string, toPort: string, maxRefs: number,
): { graph: CanvasGraph; problem: WireProblem | null } {
  const problem = connectionProblem(graph, from, fromPort, to, toPort, maxRefs);
  if (problem) return { graph, problem };
  const many = (kindOf(graph, to) && portDef(kindOf(graph, to) as NodeKind, "in", toPort)?.many) || false;
  const kept = many ? graph.edges : graph.edges.filter((e) => !(e.to === to && e.toPort === toPort));
  return {
    graph: { ...graph, edges: [...kept, { id: newId(), from, fromPort, to, toPort }] },
    problem: null,
  };
}

export const removeEdge = (graph: CanvasGraph, id: string): CanvasGraph =>
  ({ ...graph, edges: graph.edges.filter((e) => e.id !== id) });

/** Move one wire of a `many` input earlier or later. Reference order is meaning, so this is a first-class edit. */
export function moveInputEdge(graph: CanvasGraph, id: string, delta: number): CanvasGraph {
  const at = graph.edges.findIndex((e) => e.id === id);
  if (at < 0) return graph;
  const edge = graph.edges[at];
  const siblings = graph.edges.filter((e) => e.to === edge.to && e.toPort === edge.toPort);
  const pos = siblings.findIndex((e) => e.id === id);
  const to = pos + delta;
  if (to < 0 || to >= siblings.length) return graph;
  const swapped = siblings[to];
  // Swap the two wires in place: the array is the order, so swapping beats splicing a copy somewhere else.
  const edges = graph.edges.slice();
  edges[graph.edges.indexOf(swapped)] = edge;
  edges[at] = swapped;
  return { ...graph, edges };
}

/** Remove a node and every wire that touched it: a dangling wire would be a reference to nothing. */
export function removeNode(graph: CanvasGraph, id: string): CanvasGraph {
  return {
    nodes: graph.nodes.filter((n) => n.id !== id),
    edges: graph.edges.filter((e) => e.from !== id && e.to !== id),
  };
}

/** The reference number to print on a wire: 1-based among the wires of a `many` input, or null when the input
    takes one wire and the number would say nothing. */
export function edgeBadge(graph: CanvasGraph, edge: CanvasEdge): string | null {
  const to = nodeById(graph, edge.to);
  const def = to ? portDef(to.kind, "in", edge.toPort) : null;
  if (!def?.many) return null;
  const i = inputEdges(graph, edge.to, edge.toPort).findIndex((e) => e.id === edge.id);
  return i < 0 ? null : String(i + 1);
}

// -------------------------------------------------------------------------------------------------- resolution

/** The bits of an image job a node needs to know about. Structural on purpose: the page passes real `Job`s and
    this module stays free of the API types it does not build. */
export interface JobLike {
  candidates?: { file: string; url?: string; thumb?: string | null; partial?: boolean }[];
}

export const readyCandidates = (job?: JobLike | null) => (job?.candidates || []).filter((c) => !c.partial);

/** What a generate node's `image` output carries: the chosen candidate, or the first one if none is chosen (or the
    chosen one is gone because the job was re-run). */
export function generateOutput(
  node: GenerateNode, jobs: Record<string, JobLike | undefined>,
): { job: JobLike; file: string; url: string | null } | null {
  const job = node.jobId ? jobs[node.jobId] : undefined;
  const ready = readyCandidates(job);
  if (!job || !ready.length) return null;
  const pick = ready.find((c) => c.file === node.output) || ready[0];
  return { job, file: pick.file, url: pick.thumb || pick.url || null };
}

export interface ResolvedRef {
  /** The wire it came in on, so the strip can reorder and delete it. */
  edge: string;
  node: string;
  kind: NodeKind;
  label: string;
  /** A served URL for a job reference, and the bare bytes for an upload: the strip draws whichever is there. Both
      are left raw (no token, no data: prefix) because turning them into an src is the view's business. */
  url: string | null;
  b64: string | null;
  /** null when this reference has nothing to send (an upload whose bytes were dropped, or an upstream generate
      node that has not produced anything) - which blocks the generation rather than dropping it silently. */
  body: ImageReferenceBody | null;
}

export interface GenInputs {
  prompt: string;
  materials: string;
  palette: string[];
  negativeExtra: string;
  /** The style in force, and where it came from: a style node, or the saved default because nothing is wired in.
      The UI says which, because a required input quietly defaulting would be the bundling this design removed. */
  style: string;
  styleFrom: "node" | "default";
  promptNode: string | null;
  styleNode: string | null;
  refs: ResolvedRef[];
  /** The usable ones, in edge order: exactly what goes into the request body. */
  references: ImageReferenceBody[];
  unusable: number;
}

/** Parse the palette line. The API takes at most 8 colours, so a longer line is capped rather than refused. */
export const paletteList = (s: string) =>
  s.split(",").map((x) => x.trim()).filter(Boolean).slice(0, 8);

/** Everything a generate node will send, read out of the graph. */
export function resolveGenerate(
  graph: CanvasGraph, node: GenerateNode, ctx: { defaultStyle: string; jobs?: Record<string, JobLike | undefined> },
): GenInputs {
  const jobs = ctx.jobs || {};
  const src = (port: string) => {
    const e = inputEdges(graph, node.id, port)[0];
    return e ? nodeById(graph, e.from) : null;
  };

  const promptNode = src("prompt");
  const p = promptNode && promptNode.kind === "prompt" ? promptNode : null;
  const styleNodeRaw = src("style");
  const s = styleNodeRaw && styleNodeRaw.kind === "style" ? styleNodeRaw : null;

  const refs: ResolvedRef[] = inputEdges(graph, node.id, "images").map((e) => {
    const from = nodeById(graph, e.from);
    if (!from) {
      return { edge: e.id, node: e.from, kind: "image", label: "", url: null, b64: null, body: null };
    }
    if (from.kind === "image") {
      return {
        edge: e.id, node: from.id, kind: from.kind,
        label: from.ref.label || "",
        url: from.ref.url || null,
        b64: from.ref.image_b64 || null,
        body: referenceBody(from.ref),
      };
    }
    if (from.kind === "generate") {
      const out = generateOutput(from, jobs);
      // A generate node only sends a picture once it has one; until then the wire is real but empty, and saying so
      // is better than sending a reference that points at a file that does not exist.
      if (!out || !from.jobId) {
        return { edge: e.id, node: from.id, kind: from.kind, label: "", url: null, b64: null, body: null };
      }
      return {
        edge: e.id, node: from.id, kind: from.kind,
        label: out.file,
        url: out.url,
        b64: null,
        body: { job_id: from.jobId, file: candidatePath(out.file), label: clampLabel(out.file) },
      };
    }
    return { edge: e.id, node: from.id, kind: from.kind, label: "", url: null, b64: null, body: null };
  });

  const references = refs.map((r) => r.body).filter((b): b is ImageReferenceBody => b !== null);
  return {
    prompt: p?.prompt.trim() || "",
    materials: p?.materials || "",
    palette: paletteList(p?.palette || ""),
    negativeExtra: p?.avoid || "",
    style: s?.style || ctx.defaultStyle,
    styleFrom: s ? "node" : "default",
    promptNode: p?.id ?? null,
    styleNode: s?.id ?? null,
    refs,
    references,
    unusable: refs.length - references.length,
  };
}

/** The request body a generate node would post, built from the graph. Pure, so what a wired graph *sends* is
    something a test can assert without a browser or a server. */
export function generateBody(
  graph: CanvasGraph, node: GenerateNode,
  ctx: { defaultStyle: string; jobs?: Record<string, JobLike | undefined> },
): { body: ImageJobBody; inputs: GenInputs } {
  const inputs = resolveGenerate(graph, node, ctx);
  const body: ImageJobBody = { prompt: inputs.prompt, style: inputs.style, variations: node.variations };
  if (node.model) body.model = node.model;
  // null means "let the model preset decide", which is what the API documents for these four fields.
  if (node.width != null) body.width = node.width;
  if (node.height != null) body.height = node.height;
  if (node.steps != null) body.steps = node.steps;
  if (node.cfg != null) body.cfg = node.cfg;
  if (inputs.materials) body.materials = inputs.materials;
  if (inputs.palette.length) body.palette = inputs.palette;
  if (inputs.negativeExtra) body.negative_extra = inputs.negativeExtra;
  if (inputs.references.length) body.references = inputs.references;
  if (node.verbatim) {
    body.final_prompt = node.finalPrompt.trim();
    // Only when there is one: `final_negative_prompt` replaces the style's list, so an empty box must not erase it.
    if (node.finalNegative.trim()) body.final_negative_prompt = node.finalNegative.trim();
  }
  return { body, inputs };
}

/** The generate node feeding an asset node, with the candidate to build and whether it is ready. */
export function assetSource(
  graph: CanvasGraph, node: AssetNode, jobs: Record<string, JobLike | undefined>,
): { gen: GenerateNode; jobId: string; file: string; url: string | null } | null {
  const edge = inputEdges(graph, node.id, "images")[0];
  const from = edge ? nodeById(graph, edge.from) : null;
  // Only a generate node has an image *job*, which is what POST /v1/asset-jobs needs; an image node wired in here
  // is type-legal but has no job behind it, so the node says so instead of failing at the server.
  if (!from || from.kind !== "generate" || !from.jobId) return null;
  const out = generateOutput(from, jobs);
  if (!out) return null;
  const file = node.candidate && readyCandidates(jobs[from.jobId]).some((c) => c.file === node.candidate)
    ? node.candidate
    : out.file;
  return { gen: from, jobId: from.jobId, file, url: out.url };
}

// ------------------------------------------------------------------------------------- view, storage, migration

export const emptyView = (): CanvasView => ({ x: 40, y: 40, k: 1 });

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

/** The graph a brand-new canvas starts with: a prompt already wired into a generate node, so the model explains
    itself the moment the page opens instead of presenting an empty board and a menu. */
export function starterGraph(): CanvasGraph {
  const prompt = makeNode("prompt", { x: ORIGIN, y: ORIGIN + 40 }) as PromptNode;
  const gen = makeNode("generate", { x: ORIGIN + NODE_WIDTH.prompt + 90, y: ORIGIN }) as GenerateNode;
  return {
    nodes: [prompt, gen],
    edges: [{ id: newId(), from: prompt.id, fromPort: "out", to: gen.id, toPort: "prompt" }],
  };
}

/** Keep only what the model understands: a stored board can be old, hand-edited or truncated, and a page that
    throws on load is worse than one that drops what it cannot read. */
function sanitizeGraph(raw: any): CanvasGraph {
  const nodes: CanvasNode[] = [];
  for (const n of Array.isArray(raw?.nodes) ? raw.nodes : []) {
    if (!n || !NODE_KINDS.includes(n.kind)) continue;
    const ref = n.kind === "image" ? { ref: { ...newReference(), ...(n.ref || {}) } } : {};
    const node = makeNode(n.kind, {
      ...n, ...ref,
      id: typeof n.id === "string" && n.id ? n.id : newId(),
      x: Number.isFinite(n.x) ? n.x : 0,
      y: Number.isFinite(n.y) ? n.y : 0,
    });
    nodes.push(node);
  }
  const ids = new Set(nodes.map((n) => n.id));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const edges: CanvasEdge[] = [];
  for (const e of Array.isArray(raw?.edges) ? raw.edges : []) {
    const a = e && byId.get(e.from);
    const b = e && byId.get(e.to);
    if (!a || !b || !ids.has(e.from) || !ids.has(e.to)) continue;
    const src = portDef(a.kind, "out", e.fromPort);
    const dst = portDef(b.kind, "in", e.toPort);
    if (!src || !dst || src.type !== dst.type) continue;
    if (!dst.many && edges.some((x) => x.to === e.to && x.toPort === e.toPort)) continue;
    edges.push({ id: typeof e.id === "string" && e.id ? e.id : newId(), from: e.from, fromPort: e.fromPort, to: e.to, toPort: e.toPort });
  }
  return { nodes, edges };
}

/** A v1 board (`cards`) as a graph: each card becomes a generate node with the prompt, the style and the
    references it used to hold pulled out into nodes of their own and wired in, so an existing board keeps
    working *and* demonstrates the new model. */
const MIG = { promptDx: -660, promptDy: -30, styleDy: 250, imageDx: -340, imageDy: 180 };

export function migrateV1(raw: any): CanvasGraph | null {
  if (!Array.isArray(raw?.cards)) return null;
  const nodes: CanvasNode[] = [];
  const edges: CanvasEdge[] = [];
  for (const c of raw.cards) {
    if (!c || typeof c !== "object") continue;
    const x = Number.isFinite(c.x) ? c.x : 0;
    const y = Number.isFinite(c.y) ? c.y : 0;
    const gen = makeNode("generate", {
      x, y, model: c.model || "", variations: Number.isFinite(c.variations) ? c.variations : 4,
      width: c.width ?? null, height: c.height ?? null, steps: c.steps ?? null, cfg: c.cfg ?? null,
      verbatim: !!c.verbatim, finalPrompt: c.finalPrompt || "", finalNegative: c.finalNegative || "",
      output: null, jobId: c.jobId || null, error: c.error || null,
    });
    const prompt = makeNode("prompt", {
      x: x + MIG.promptDx, y: y + MIG.promptDy, prompt: typeof c.prompt === "string" ? c.prompt : "",
    });
    const style = makeNode("style", { x: x + MIG.promptDx, y: y + MIG.styleDy, style: c.style || "mobile_factory" });
    nodes.push(gen, prompt, style);
    edges.push({ id: newId(), from: prompt.id, fromPort: "out", to: gen.id, toPort: "prompt" });
    edges.push({ id: newId(), from: style.id, fromPort: "out", to: gen.id, toPort: "style" });
    // Reference order was the slot order, so the wires are made in that order and keep it.
    (Array.isArray(c.references) ? c.references : []).forEach((r: any, i: number) => {
      const img = makeNode("image", { x: x + MIG.imageDx, y: y + MIG.imageDy * i, ref: { ...newReference(), ...r } });
      nodes.push(img);
      edges.push({ id: newId(), from: img.id, fromPort: "out", to: gen.id, toPort: "images" });
    });
  }
  return { nodes, edges };
}

/** Read the stored board. Anything malformed falls back to something usable rather than breaking the page. */
export function loadDoc(): { graph: CanvasGraph; view: CanvasView; migrated: boolean } {
  const fresh = () => ({ graph: starterGraph(), view: emptyView(), migrated: false });
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (!raw) return fresh();
    const parsed = JSON.parse(raw) as any;
    const view = parsed?.view && Number.isFinite(parsed.view.x) && Number.isFinite(parsed.view.y)
      && Number.isFinite(parsed.view.k)
      ? { x: parsed.view.x, y: parsed.view.y, k: clampZoom(parsed.view.k) }
      : emptyView();
    if (parsed?.v === DOC_VERSION && Array.isArray(parsed.nodes)) {
      return { graph: sanitizeGraph(parsed), view, migrated: false };
    }
    const migrated = migrateV1(parsed);
    if (migrated) return { graph: migrated, view, migrated: true };
    return fresh();
  } catch {
    return fresh();
  }
}

/** The same graph without the uploaded bytes: what a full localStorage keeps. Exported because the caller has to
    apply it to its own state as well, or every later save would report the same trim again. */
export function stripUploads(graph: CanvasGraph): CanvasGraph {
  return {
    ...graph,
    nodes: graph.nodes.map((n) => (n.kind === "image" && n.ref.image_b64
      ? { ...n, ref: { ...n.ref, image_b64: undefined, dropped: true } }
      : n)),
  };
}

/** Persist the board. Uploads are the only large part of it, so a full quota costs the bytes, not the graph. */
export function saveDoc(graph: CanvasGraph, view: CanvasView): SaveResult {
  const doc: CanvasDoc = { v: DOC_VERSION, nodes: graph.nodes, edges: graph.edges, view };
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(doc));
    return "ok";
  } catch {}
  // A browser quota is around 5 MB and a photo is a megabyte of base64, so drop the *bytes* and keep the rest:
  // the node stays on the board, marked, and it says so instead of generating from something that is gone.
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({ ...doc, nodes: stripUploads(graph).nodes }));
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
