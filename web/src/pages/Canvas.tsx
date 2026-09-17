import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Box, Check, ImagePlus, Maximize2, Minus, Palette, Plus, RotateCcw, Sparkles, SquarePlus, Type,
} from "lucide-react";
import { api, Candidate, ImageModel, Job, withToken } from "../api";
import {
  CanvasGraph, CanvasNode as Node, CanvasView, MAX_REFERENCES, NODE_WIDTH, NodeKind, WireProblem,
  acceptsAnywhere, assetSource, clampLabel, clampZoom, connect, dataUrlToBase64, emptyView, fitView,
  generateBody, jobIdOf, loadDoc, makeNode, moveInputEdge, newReference, nextSpot, nodeById, removeEdge,
  removeNode, resolveGenerate, saveDoc, stripUploads, zoomAbout,
} from "../canvas";
import { useT } from "../i18n";
import { useStore } from "../store";
import CanvasNode from "../components/CanvasNode";
import CanvasEdges, { PendingWire } from "../components/CanvasEdges";
import Lightbox, { LightboxItem } from "../components/Lightbox";
import MakeAssetDialog from "../components/MakeAssetDialog";
import ReferencePicker, { PickedReference, PickerSource } from "../components/ReferencePicker";
import { Empty } from "../components/ui";

/** Mirrors what ReferenceImage._b64 accepts: at most 40 MB once decoded, and only a PNG or a JPEG. Checking the file
    here keeps a megabyte of base64 out of a request the server is going to refuse. */
const MAX_UPLOAD = 40 * 1024 * 1024;
/** Statuses whose job is still going to change; anything else is fetched once and left alone. */
const ACTIVE = new Set(["queued", "running", "held"]);
/** Dot grid spacing in world units. */
const GRID = 26;

const KIND_ICON: Record<NodeKind, typeof Type> = {
  prompt: Type, style: Palette, image: ImagePlus, generate: Sparkles, asset: Box,
};

/** The infinite canvas: a typed node graph, where a result can be wired into the next generation.
 *
 * The graph and the view transform live in localStorage (server-side boards are a later step), so the workspace
 * survives a reload. Job *responses* are not persisted - only their ids are - and are refetched here, because a
 * board that showed stale candidates after a reload would be worse than one that shows a spinner for a moment.
 *
 * The graph and the view are two states on purpose: panning changes the view on every frame, and keeping the
 * graph's identity stable across a pan is what lets the memoised nodes skip re-rendering while the board moves.
 */
export default function Canvas() {
  const t = useT();
  const { settings, toast, tick } = useStore();
  const [initial] = useState(loadDoc);
  const [graph, setGraph] = useState<CanvasGraph>(initial.graph);
  const [view, setView] = useState<CanvasView>(initial.view);
  const [caps, setCaps] = useState<any>(null);
  const [jobs, setJobs] = useState<Record<string, Job>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [picker, setPicker] = useState<string | null>(null);
  const [lightbox, setLightbox] = useState<{ id: string; index: number } | null>(null);
  const [asset, setAsset] = useState<{ id: string; job: Job; file: string } | null>(null);
  const [wire, setWire] = useState<PendingWire | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [panning, setPanning] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [spaceDown, setSpaceDown] = useState(false);

  const viewportRef = useRef<HTMLDivElement | null>(null);
  const pan = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null);
  const drag = useRef<{ id: string; x: number; y: number; cx: number; cy: number } | null>(null);
  const pending = useRef<{ from: string; fromPort: string } | null>(null);
  const spaceRef = useRef(false);
  const warned = useRef(false);

  // Refs, so the handlers passed to the (memoised) nodes can stay stable while still seeing current state.
  const tRef = useRef(t);
  tRef.current = t;
  const graphRef = useRef(graph);
  graphRef.current = graph;
  const jobsRef = useRef(jobs);
  jobsRef.current = jobs;
  const viewRef = useRef(view);
  viewRef.current = view;
  const selectedRef = useRef<string | null>(null);
  selectedRef.current = selected;

  useEffect(() => { api.capabilities().then(setCaps).catch(() => {}); }, []);

  // A board that came from the card model is upgraded on load, and saying so once is what stops the user thinking
  // their old board was thrown away.
  useEffect(() => {
    if (initial.migrated) toast(tRef.current("canvas.migrated"));
  }, [initial.migrated, toast]);

  /** Every default here is read from the running server (or the saved settings) rather than written into the page. */
  const defaults = useMemo(() => {
    const styles = Object.keys(caps?.styles || {});
    const models: ImageModel[] = caps?.image_models || [];
    const preferred = settings?.default_style || "";
    const style = styles.includes(preferred) ? preferred : styles[0] || "mobile_factory";
    const wanted = settings?.default_image_model || "";
    const model = models.some((m) => m.id === wanted && m.available !== false)
      ? wanted
      : models.find((m) => m.available !== false)?.id || "";
    const vs = caps?.image_job_schema?.properties?.variations || {};
    const varMin = typeof vs.minimum === "number" ? vs.minimum : 1;
    const varMax = typeof vs.maximum === "number" ? vs.maximum : 8;
    const want = settings?.default_variations ?? vs.default ?? 4;
    return {
      style, model, varMin, varMax,
      variations: Math.min(varMax, Math.max(varMin, want)),
      maxRefs: caps?.image_job_schema?.properties?.references?.maxItems || MAX_REFERENCES,
    };
  }, [caps, settings]);
  const defaultsRef = useRef(defaults);
  defaultsRef.current = defaults;

  /** Saving is debounced: a pan changes the view on every frame, and localStorage is synchronous. */
  useEffect(() => {
    const h = window.setTimeout(() => {
      const r = saveDoc(graph, view);
      if (r === "ok") { warned.current = false; return; }
      if (r === "trimmed") {
        // Apply the same trim the save had to make, or every later save would report the same thing again.
        setGraph(stripUploads(graph));
        toast(tRef.current("canvas.trimmed"), true);
        return;
      }
      if (!warned.current) { warned.current = true; toast(tRef.current("canvas.saveFailed"), true); }
    }, 400);
    return () => window.clearTimeout(h);
  }, [graph, view]);

  const reloadJob = useCallback(async (id: string) => {
    try {
      const j = await api.job(id, 20);
      setJobs((p) => ({ ...p, [id]: j }));
    } catch {}
  }, []);

  // Poll only what is missing or still moving; `refresh` reads the ids from a ref so the interval never goes stale.
  const idsRef = useRef<string[]>([]);
  idsRef.current = graph.nodes.flatMap((n) => (jobIdOf(n) ? [jobIdOf(n) as string] : []));
  const jobKey = idsRef.current.join(",");
  const refresh = useCallback(async () => {
    const want = idsRef.current.filter((id) => {
      const j = jobsRef.current[id];
      return !j || ACTIVE.has(j.status);
    });
    if (!want.length) return;
    const got = await Promise.all(want.map((id) => api.job(id, 20).catch(() => null)));
    const add: Record<string, Job> = {};
    got.forEach((j, i) => { if (j) add[want[i]] = j; });
    if (Object.keys(add).length) setJobs((p) => ({ ...p, ...add }));
  }, []);
  useEffect(() => { void refresh(); }, [jobKey, tick, refresh]);
  useEffect(() => {
    const h = window.setInterval(() => void refresh(), 3500);
    return () => window.clearInterval(h);
  }, [refresh]);

  const patchNode = useCallback((id: string, p: Partial<Node>) => {
    setGraph((g) => ({ ...g, nodes: g.nodes.map((n) => (n.id === id ? { ...n, ...p } as Node : n)) }));
  }, []);

  const applyGraph = useCallback((next: CanvasGraph) => {
    graphRef.current = next;
    setGraph(next);
  }, []);

  /** Pointer position in world units: the surface is translated and scaled, so this is the inverse of that. */
  const worldPoint = (clientX: number, clientY: number) => {
    const r = viewportRef.current?.getBoundingClientRect();
    const v = viewRef.current;
    return {
      x: (clientX - (r?.left ?? 0) - v.x) / v.k,
      y: (clientY - (r?.top ?? 0) - v.y) / v.k,
    };
  };

  /** Bring one node into view, after adding it or adding to it.
   *
   * Panning is not enough on its own: a generate node showing its composed prompt is taller than the viewport
   * (measured 1027 px against 885 px), so a node added below the fold would leave its Generate button out of
   * sight. Zoom out, but only as far as it takes to fit this one node and never in, then pan the minimum. */
  const revealNode = useCallback((id: string) => {
    const el = viewportRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const dom = el.querySelector<HTMLElement>(`[data-node-id="${id}"]`);
    const node = nodeById(graphRef.current, id);
    if (!node || !r.width || !r.height) return;
    const w = dom?.clientWidth || NODE_WIDTH[node.kind];
    const h = dom?.clientHeight || 300;
    const pad = 24;
    setView((v) => {
      const k = Math.min(v.k, clampZoom(Math.min((r.width - pad * 2) / w, (r.height - pad * 2) / h)));
      const bw = w * k;
      const bh = h * k;
      // The node's top-left in screen space at the new zoom, before any pan.
      const sx = node.x * k;
      const sy = node.y * k;
      let x = v.x;
      let y = v.y;
      if (sx + x < pad) x = pad - sx;
      else if (sx + x + bw > r.width - pad) x = r.width - pad - bw - sx;
      if (sy + y < pad) y = pad - sy;
      else if (sy + y + bh > r.height - pad) y = r.height - pad - bh - sy;
      return { x, y, k };
    });
  }, []);

  const addNode = useCallback((kind: NodeKind) => {
    const d = defaultsRef.current;
    const g0 = graphRef.current;
    const spot = nextSpot(g0.nodes);
    const extra: Partial<Node> =
      kind === "generate" ? { model: d.model, variations: d.variations }
      : kind === "style" ? { style: d.style }
      : {};
    const node = makeNode(kind, { ...extra, x: spot.x, y: spot.y });
    // Pushed onto the ref as well, so two nodes added in the same tick still stack instead of landing on top.
    applyGraph({ ...g0, nodes: [...g0.nodes, node] });
    // Measured after it renders: how tall the node really is is the whole point of the reveal.
    window.setTimeout(() => revealNode(node.id), 60);
  }, [applyGraph, revealNode]);

  const removeNodeById = useCallback((id: string) => {
    const node = nodeById(graphRef.current, id);
    if (!node) return;
    // Only a node that started a job is worth a question: the others are inputs that cost a few seconds to redo.
    const hasJob = !!jobIdOf(node);
    if (!confirm(tRef.current(hasJob ? "canvas.nodeDeleteJobConfirm" : "canvas.nodeDeleteConfirm"))) return;
    applyGraph(removeNode(graphRef.current, id));
    setSelected(null);
  }, [applyGraph]);

  /** Why a refused wire is refused, in words. A wire onto an output nothing accepts - the asset port today - is a
      different situation from a mismatched pair and gets its own sentence. */
  const wireMessage = useCallback((p: WireProblem) => {
    const tr = tRef.current;
    switch (p.code) {
      case "same": return tr("canvas.wire.same");
      case "cycle": return tr("canvas.wire.cycle");
      case "many": return tr("canvas.wire.many", { n: p.limit ?? 0 });
      case "duplicate": return tr("canvas.wire.duplicate");
      case "unknown": return tr("canvas.wire.unknown");
      default:
        return acceptsAnywhere(p.from)
          ? tr("canvas.wire.type", { from: tr(`canvas.type.${p.from}`), to: tr(`canvas.type.${p.to}`) })
          : tr("canvas.wire.noConsumer", { type: tr(`canvas.type.${p.from}`) });
    }
  }, []);

  const connectWire = useCallback((from: string, fromPort: string, to: string, toPort: string) => {
    const res = connect(graphRef.current, from, fromPort, to, toPort, defaultsRef.current.maxRefs);
    if (res.problem) {
      // A refusal has to be visible: a drop that quietly does nothing reads as a broken page.
      toast(wireMessage(res.problem), true);
      return;
    }
    applyGraph(res.graph);
    setSelected(null);
  }, [applyGraph, toast, wireMessage]);

  const generate = useCallback(async (id: string) => {
    const node = nodeById(graphRef.current, id);
    if (!node || node.kind !== "generate") return;
    setBusyId(id);
    patchNode(id, { error: null });
    try {
      // The body is resolved from the graph, so what is sent is exactly what the wires say.
      const { body } = generateBody(graphRef.current, node, {
        defaultStyle: defaultsRef.current.style, jobs: jobsRef.current,
      });
      const created = await api.createImageJob(body);
      patchNode(id, { jobId: created.job_id });
      void reloadJob(created.job_id);
    } catch (e: any) {
      // 422 (references on a lane that ignores them, an editable graph that is not there) and 404 (a reference file
      // that is gone) land here, and the message belongs on the node that caused it rather than in a toast.
      patchNode(id, { error: e.message || String(e) });
    } finally {
      setBusyId(null);
    }
  }, [patchNode, reloadJob]);

  const uploadInto = useCallback((id: string, file: File) => {
    if (file.size > MAX_UPLOAD) return toast(tRef.current("canvas.uploadTooBig"), true);
    if (!/^image\/(png|jpeg)$/.test(file.type)) return toast(tRef.current("canvas.uploadType"), true);
    const fr = new FileReader();
    fr.onload = () => {
      // Bare base64: the validator decodes with `validate=True`, so a data: URL prefix would be refused.
      const image_b64 = dataUrlToBase64(String(fr.result || ""));
      patchNode(id, { ref: { ...newReference(), kind: "upload", image_b64, label: clampLabel(file.name) } });
    };
    fr.onerror = () => toast(tRef.current("canvas.uploadFailed"), true);
    fr.readAsDataURL(file);
  }, [patchNode, toast]);

  const makeAsset = useCallback((id: string) => {
    const node = nodeById(graphRef.current, id);
    if (!node || node.kind !== "asset") return;
    const src = assetSource(graphRef.current, node, jobsRef.current);
    const job = src ? jobsRef.current[src.jobId] : null;
    if (!src || !job) return;
    setAsset({ id, job, file: src.file });
  }, []);

  /** Start a wire. The capture goes on the viewport, which is where the move and up handlers already are. */
  const onPortDown = useCallback((id: string, port: string, e: React.PointerEvent<HTMLElement>) => {
    e.stopPropagation();
    const p = worldPoint(e.clientX, e.clientY);
    pending.current = { from: id, fromPort: port };
    setWire({ from: id, fromPort: port, x: p.x, y: p.y });
    setDragging(true);
    viewportRef.current?.setPointerCapture(e.pointerId);
  }, []);

  const onDragStart = useCallback((id: string, e: React.PointerEvent<HTMLElement>) => {
    // A button inside the header (delete) is a click, not a drag.
    if (e.button !== 0 || spaceRef.current || (e.target as HTMLElement).closest("button")) return;
    const node = nodeById(graphRef.current, id);
    if (!node) return;
    drag.current = { id, x: e.clientX, y: e.clientY, cx: node.x, cy: node.y };
    setDragging(true);
    viewportRef.current?.setPointerCapture(e.pointerId);
    e.stopPropagation();
  }, []);

  /** Start a pan: the empty background, the middle button anywhere, or a left drag while space is held. Never from
      a node, a port or a wire - those have their own gestures. */
  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (drag.current || pending.current) return;
    const el = e.target as HTMLElement;
    const busy = !!(el.closest("[data-node-id],[data-port-in],[data-port-out],[data-edge]"));
    const middle = e.button === 1;
    if (!middle && !(e.button === 0 && (spaceRef.current || !busy))) return;
    if (!busy) setSelected(null);
    pan.current = { x: e.clientX, y: e.clientY, vx: viewRef.current.x, vy: viewRef.current.y };
    setPanning(true);
    viewportRef.current?.setPointerCapture(e.pointerId);
    e.preventDefault();
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (pending.current) {
      const p = worldPoint(e.clientX, e.clientY);
      setWire((w) => (w ? { ...w, x: p.x, y: p.y } : w));
      return;
    }
    const p = pan.current;
    if (p) {
      setView({ x: p.vx + (e.clientX - p.x), y: p.vy + (e.clientY - p.y), k: viewRef.current.k });
      return;
    }
    const d = drag.current;
    if (d) {
      // Screen pixels to world units: a drag has to move the node by what the hand moved, at any zoom.
      const k = viewRef.current.k;
      patchNode(d.id, { x: Math.round(d.cx + (e.clientX - d.x) / k), y: Math.round(d.cy + (e.clientY - d.y) / k) });
    }
  };

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    const pw = pending.current;
    pending.current = null;
    setWire(null);
    pan.current = null;
    drag.current = null;
    setPanning(false);
    setDragging(false);
    const el = viewportRef.current;
    if (el?.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId);

    if (!pw) return;
    // The pointer is captured by the viewport, so `e.target` is the viewport and the drop target has to be hit
    // tested: dropping on nothing cancels the wire, which needs no message.
    const over = document.elementFromPoint(e.clientX, e.clientY) as HTMLElement | null;
    const port = over?.closest("[data-port-in]") as HTMLElement | null;
    const to = port?.closest("[data-node-id]")?.getAttribute("data-node-id") || "";
    const key = port?.getAttribute("data-port-in") || "";
    if (to && key) connectWire(pw.from, pw.fromPort, to, key);
  };

  // React attaches `wheel` passively at the root, where preventDefault() is refused: the zoom has to be a native
  // listener on the viewport itself.
  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      // deltaMode 1 counts lines (Firefox), so a notch would otherwise zoom by a barely visible amount.
      const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
      const factor = Math.exp(-dy * (e.ctrlKey ? 0.01 : 0.0015));
      setView((v) => zoomAbout(v, factor, e.clientX - r.left, e.clientY - r.top));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  // Space-drag panning, the gesture people expect from a canvas; typing a space in a field still types a space.
  useEffect(() => {
    const typing = (el: EventTarget | null) =>
      el instanceof HTMLElement && !!el.closest("input, textarea, select, button, [contenteditable=true]");
    const down = (e: KeyboardEvent) => {
      if (e.code !== "Space" || typing(e.target)) return;
      e.preventDefault();
      spaceRef.current = true;
      setSpaceDown(true);
    };
    const up = (e: KeyboardEvent) => {
      if (e.code !== "Space") return;
      spaceRef.current = false;
      setSpaceDown(false);
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => { window.removeEventListener("keydown", down); window.removeEventListener("keyup", up); };
  }, []);

  // Delete removes the selected wire. Guarded against typing so Backspace in a textarea never deletes a wire.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Delete" && e.key !== "Backspace") return;
      const el = e.target;
      if (el instanceof HTMLElement && el.closest("input, textarea, select, [contenteditable=true]")) return;
      const id = selectedRef.current;
      if (!id) return;
      e.preventDefault();
      setGraph((g) => removeEdge(g, id));
      setSelected(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const zoomBy = (factor: number) => {
    const el = viewportRef.current;
    const r = el?.getBoundingClientRect();
    setView((v) => zoomAbout(v, factor, (r?.width ?? 900) / 2, (r?.height ?? 600) / 2));
  };

  const fit = () => {
    const el = viewportRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    // Measured, not assumed: `clientWidth` is the layout size, which a transform does not touch, so it is exactly
    // the node's size in world units however far the canvas is zoomed out.
    const boxes = graphRef.current.nodes.map((n) => {
      const dom = el.querySelector<HTMLElement>(`[data-node-id="${n.id}"]`);
      return { x: n.x, y: n.y, w: dom?.clientWidth || NODE_WIDTH[n.kind], h: dom?.clientHeight || 300 };
    });
    setView(fitView(boxes, r.width, r.height));
  };

  const openNode = lightbox ? graph.nodes.find((n) => n.id === lightbox.id) || null : null;
  const openJobId = openNode ? jobIdOf(openNode) : null;
  const openJob = openJobId ? jobs[openJobId] || null : null;
  const openCandidates = openJob?.candidates || [];
  const items: LightboxItem[] = openCandidates.map((c) => ({
    url: withToken(c.url),
    preview: c.thumb ? withToken(c.thumb) : undefined,
    caption: c.file,
    note: c.seed != null ? t("session.seedLabel", { n: c.seed }) : "",
  }));

  const picking = picker ? graph.nodes.find((n) => n.id === picker) || null : null;
  const sources: PickerSource[] = graph.nodes
    .filter((n): n is Extract<Node, { kind: "generate" }> => n.kind === "generate" && !!n.jobId && !!jobs[n.jobId])
    .map((n) => ({
      key: n.id,
      label: resolveGenerate(graph, n, { defaultStyle: defaults.style }).prompt.slice(0, 40) || t("canvas.node.generate"),
      job: jobs[n.jobId as string],
    }));

  // Everything a node needs is handed over as one stable bundle, so a pan (which re-renders this page on every
  // frame) does not re-render the memoised nodes.
  const nodeProps = {
    graph, jobs, caps, busy: false,
    maxRefs: defaults.maxRefs, varMin: defaults.varMin, varMax: defaults.varMax,
    fallbackStyle: defaults.style,
    onDragStart, onChange: patchNode, onRemove: removeNodeById, onPortDown,
    onUpload: uploadInto, onPick: setPicker, onGenerate: generate,
    onOpenResult: (id: string, index: number) => setLightbox({ id, index }),
    onSelectOutput: (id: string, file: string) => patchNode(id, { output: file }),
    onMakeAsset: makeAsset,
    onMoveRef: (id: string, edgeId: string, delta: number) =>
      setGraph((g) => moveInputEdge(g, edgeId, delta)),
    onRemoveRef: (id: string, edgeId: string) => setGraph((g) => removeEdge(g, edgeId)),
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{t("canvas.title")}</h1>
          <p>{t("canvas.subtitle")}</p>
        </div>
        <div className="canvas-add">
          <span className="small muted">{t("canvas.addNode")}</span>
          {(["prompt", "style", "image", "generate", "asset"] as NodeKind[]).map((kind) => {
            const Icon = KIND_ICON[kind];
            return (
              <button key={kind} className="btn sm" onClick={() => addNode(kind)}
                      title={`${t("canvas.addNode")}: ${t(`canvas.node.${kind}`)}`}>
                <Icon size={13} /> {t(`canvas.node.${kind}`)}
              </button>
            );
          })}
        </div>
      </div>

      <div className="canvas-wrap">
        <div
          ref={viewportRef}
          className={"canvas-viewport" + (spaceDown ? " grab" : "") + (panning ? " panning" : "")
            + (dragging ? " dragging" : "") + (wire ? " wiring" : "")}
          style={{
            backgroundSize: `${GRID * view.k}px ${GRID * view.k}px`,
            backgroundPosition: `${view.x}px ${view.y}px`,
          }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
        >
          <div className="canvas-surface" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})` }}>
            {/* The wires sit under the nodes, so a wire that crosses a node passes behind it. */}
            <CanvasEdges graph={graph} selected={selected} pending={wire}
                         onSelect={setSelected} onDelete={(id) => { setGraph((g) => removeEdge(g, id)); setSelected(null); }} />
            {graph.nodes.map((n) => (
              <CanvasNode key={n.id} node={n} {...nodeProps} busy={busyId === n.id} />
            ))}
          </div>

          {!graph.nodes.length && (
            <div className="canvas-empty">
              <Empty
                icon={<SquarePlus size={22} />}
                title={t("canvas.emptyTitle")}
                hint={t("canvas.emptyHint")}
                action={<button className="btn primary" onClick={() => addNode("generate")}>
                  <SquarePlus size={14} /> {t("canvas.node.generate")}
                </button>}
              />
            </div>
          )}

          <div className="canvas-bar">
            <button className="btn ghost icon sm" onClick={() => zoomBy(1 / 1.25)} title={t("canvas.zoomOut")} aria-label={t("canvas.zoomOut")}>
              <Minus size={14} />
            </button>
            <span className="canvas-zoom">{Math.round(view.k * 100)}%</span>
            <button className="btn ghost icon sm" onClick={() => zoomBy(1.25)} title={t("canvas.zoomIn")} aria-label={t("canvas.zoomIn")}>
              <Plus size={14} />
            </button>
            <button className="btn ghost sm" onClick={fit} title={t("canvas.fitHint")} disabled={!graph.nodes.length}>
              <Maximize2 size={13} /> {t("canvas.fit")}
            </button>
            <button className="btn ghost sm" onClick={() => setView(emptyView())} title={t("canvas.resetHint")}>
              <RotateCcw size={13} /> {t("canvas.reset")}
            </button>
            <span className="canvas-hint small muted">{t("canvas.panHint")}</span>
          </div>
        </div>
      </div>

      {picking && picker && (
        <ReferencePicker
          sources={sources}
          onClose={() => setPicker(null)}
          onPick={(ref: PickedReference) => {
            patchNode(picker, { ref: { ...newReference(), kind: "job", ...ref } });
            setPicker(null);
          }}
        />
      )}

      {openNode && items.length > 0 && (
        <Lightbox
          items={items}
          index={Math.min(lightbox?.index ?? 0, items.length - 1)}
          onIndex={(n) => setLightbox({ id: openNode.id, index: n })}
          onClose={() => setLightbox(null)}
          action={(n) => {
            const c = openCandidates[n];
            // A candidate still being written has no file under artifacts/ yet, so it cannot be an output.
            if (!c || c.partial) return null;
            return (
              <button className="btn sm primary" onClick={() => { patchNode(openNode.id, { output: c.file }); setLightbox(null); }}>
                <Check size={13} /> {t("canvas.setAsOutput")}
              </button>
            );
          }}
        />
      )}

      {asset && (
        <MakeAssetDialog
          job={asset.job}
          files={[asset.file]}
          onClose={() => setAsset(null)}
          onDone={(created) => {
            patchNode(asset.id, { jobId: created[0]?.job_id ?? null });
            setAsset(null);
            toast(t("canvas.assetJobStarted", { n: created.length }));
          }}
        />
      )}
    </>
  );
}
