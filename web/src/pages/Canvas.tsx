import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Box, Link2, Maximize2, Minus, Plus, RotateCcw, SquarePlus } from "lucide-react";
import { api, Candidate, ImageJobBody, ImageModel, ImageReferenceBody, Job, withToken } from "../api";
import {
  CARD_W, CanvasCard as Card, CanvasReference, CanvasView, MAX_REFERENCES, clampZoom, dataUrlToBase64,
  emptyView, fitView, loadDoc, newCard, newReference, nextSpot, referenceBody, saveDoc, stripUploads, zoomAbout,
} from "../canvas";
import { useT } from "../i18n";
import { useStore } from "../store";
import CanvasCard from "../components/CanvasCard";
import Lightbox, { LightboxItem } from "../components/Lightbox";
import MakeAssetDialog from "../components/MakeAssetDialog";
import ReferencePicker, { PickedReference, PickerSource, candidatePath, clampLabel } from "../components/ReferencePicker";
import { Empty } from "../components/ui";

/** Mirrors what ReferenceImage._b64 accepts: at most 40 MB once decoded, and only a PNG or a JPEG. Checking the file
    here keeps a megabyte of base64 out of a request the server is going to refuse. */
const MAX_UPLOAD = 40 * 1024 * 1024;
/** Statuses whose job is still going to change; anything else is fetched once and left alone. */
const ACTIVE = new Set(["queued", "running", "held"]);
/** Dot grid spacing in world units. */
const GRID = 26;

/** The infinite canvas: a board of generation requests, where a result can become the next request's reference.
 *
 * Cards and the view transform live in localStorage (server-side boards are a later step), so the workspace survives
 * a reload. Job *responses* are not persisted - only their ids are - and are refetched here, because a board that
 * showed stale candidates after a reload would be worse than one that shows a spinner for a moment.
 */
export default function Canvas() {
  const t = useT();
  const { settings, toast, tick } = useStore();
  const [initial] = useState(loadDoc);
  const [cards, setCards] = useState<Card[]>(initial.cards);
  const [view, setView] = useState<CanvasView>(initial.view);
  const [caps, setCaps] = useState<any>(null);
  const [jobs, setJobs] = useState<Record<string, Job>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [picker, setPicker] = useState<{ id: string; index: number } | null>(null);
  const [lightbox, setLightbox] = useState<{ id: string; index: number } | null>(null);
  const [asset, setAsset] = useState<{ job: Job; file: string } | null>(null);
  const [panning, setPanning] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [spaceDown, setSpaceDown] = useState(false);

  const viewportRef = useRef<HTMLDivElement | null>(null);
  const pan = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null);
  const drag = useRef<{ id: string; x: number; y: number; cx: number; cy: number } | null>(null);
  const spaceRef = useRef(false);
  const warned = useRef(false);

  // Refs, so the handlers passed to the (memoised) cards can stay stable while still seeing current state.
  const tRef = useRef(t);
  tRef.current = t;
  const cardsRef = useRef(cards);
  cardsRef.current = cards;
  const jobsRef = useRef(jobs);
  jobsRef.current = jobs;
  const viewRef = useRef(view);
  viewRef.current = view;

  useEffect(() => { api.capabilities().then(setCaps).catch(() => {}); }, []);

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
      const r = saveDoc({ cards, view });
      if (r === "ok") { warned.current = false; return; }
      if (r === "trimmed") {
        // Apply the same trim the save had to make, or every later save would report the same thing again.
        setCards((cs) => stripUploads({ cards: cs, view }).cards);
        toast(tRef.current("canvas.trimmed"), true);
        return;
      }
      if (!warned.current) { warned.current = true; toast(tRef.current("canvas.saveFailed"), true); }
    }, 400);
    return () => window.clearTimeout(h);
  }, [cards, view]);

  const reloadJob = useCallback(async (id: string) => {
    try {
      const j = await api.job(id, 20);
      setJobs((p) => ({ ...p, [id]: j }));
    } catch {}
  }, []);

  // Poll only what is missing or still moving; `refresh` reads the ids from a ref so the interval never goes stale.
  const idsRef = useRef<string[]>([]);
  idsRef.current = cards.map((c) => c.jobId).filter((v): v is string => !!v);
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

  const patch = useCallback((id: string, p: Partial<Card>) => {
    setCards((cs) => cs.map((c) => (c.id === id ? { ...c, ...p } : c)));
  }, []);

  /** Bring one card into view. Used after adding one.
   *
   * Panning is not enough on its own: a card that is showing its composed prompt is **taller than the viewport**
   * (measured 1027 px against 885 px), so a new card's Generate button would land below the fold and the user's
   * first move would be to hunt for it. So zoom out, but only as far as it takes to fit this one card and never
   * in, then pan the minimum that puts it inside the viewport.
   */
  const revealCard = useCallback((id: string) => {
    const el = viewportRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const node = el.querySelector<HTMLElement>(`[data-card-id="${id}"]`);
    const card = cardsRef.current.find((c) => c.id === id);
    if (!node || !card || !r.width || !r.height) return;
    const pad = 24;
    setView((v) => {
      const k = Math.min(v.k, clampZoom(Math.min((r.width - pad * 2) / node.clientWidth,
                                                  (r.height - pad * 2) / node.clientHeight)));
      const w = node.clientWidth * k;
      const h = node.clientHeight * k;
      // The card's top-left in screen space at the new zoom, before any pan.
      const sx = card.x * k;
      const sy = card.y * k;
      let x = v.x;
      let y = v.y;
      if (sx + x < pad) x = pad - sx;
      else if (sx + x + w > r.width - pad) x = r.width - pad - w - sx;
      if (sy + y < pad) y = pad - sy;
      else if (sy + y + h > r.height - pad) y = r.height - pad - h - sy;
      return { x, y, k };
    });
  }, []);

  const addCard = useCallback((extra: Partial<Card> = {}) => {
    const d = defaultsRef.current;
    const spot = nextSpot(cardsRef.current);
    const card = newCard({
      x: extra.x ?? spot.x, y: extra.y ?? spot.y,
      style: d.style, model: d.model, variations: d.variations, ...extra,
    });
    // Built outside the updater so the reveal below knows the id, and pushed onto the ref as well so two cards
    // added in the same tick still stack instead of landing on top of each other.
    cardsRef.current = [...cardsRef.current, card];
    setCards(cardsRef.current);
    // Measured after it renders: how tall the card really is is the whole point of the reveal.
    window.setTimeout(() => revealCard(card.id), 60);
  }, [revealCard]);

  /** Replace the reference in a slot, or append one when the slot is the "add" button's (index === length). */
  const setRef = useCallback((id: string, index: number, ref: CanvasReference) => {
    setCards((cs) => cs.map((c) => {
      if (c.id !== id) return c;
      const references = c.references.slice();
      if (index >= references.length) references.push(ref);
      else references[index] = ref;
      return { ...c, references };
    }));
  }, []);

  const generate = useCallback(async (id: string) => {
    const card = cardsRef.current.find((c) => c.id === id);
    if (!card) return;
    const prompt = card.prompt.trim();
    if (prompt.length < 3) return;
    setBusyId(id);
    patch(id, { error: null });
    try {
      const body: ImageJobBody = { prompt, style: card.style, variations: card.variations };
      if (card.model) body.model = card.model;
      // null means "let the model preset decide", which is what the API documents for these four fields.
      if (card.width != null) body.width = card.width;
      if (card.height != null) body.height = card.height;
      if (card.steps != null) body.steps = card.steps;
      if (card.cfg != null) body.cfg = card.cfg;
      const references = card.references.map(referenceBody).filter((r): r is ImageReferenceBody => r !== null);
      if (references.length) body.references = references;
      if (card.verbatim) {
        body.final_prompt = card.finalPrompt.trim();
        // Only when there is one: `final_negative_prompt` replaces the style's list, so an empty box must not erase it.
        if (card.finalNegative.trim()) body.final_negative_prompt = card.finalNegative.trim();
      }
      const created = await api.createImageJob(body);
      patch(id, { jobId: created.job_id });
      void reloadJob(created.job_id);
    } catch (e: any) {
      // 422 (references on a lane that ignores them, unknown workflow) and 404 (a reference file that is gone) land
      // here, and the message belongs on the card that caused it rather than in a toast that scrolls away.
      patch(id, { error: e.message || String(e) });
    } finally {
      setBusyId(null);
    }
  }, [patch, reloadJob]);

  const onUpload = useCallback((id: string, index: number, file: File) => {
    if (file.size > MAX_UPLOAD) return toast(tRef.current("canvas.uploadTooBig"), true);
    if (!/^image\/(png|jpeg)$/.test(file.type)) return toast(tRef.current("canvas.uploadType"), true);
    const fr = new FileReader();
    fr.onload = () => {
      // Bare base64: the validator decodes with `validate=True`, so a data: URL prefix would be refused.
      const image_b64 = dataUrlToBase64(String(fr.result || ""));
      setRef(id, index, { ...newReference(), kind: "upload", image_b64, label: clampLabel(file.name) });
    };
    fr.onerror = () => toast(tRef.current("canvas.uploadFailed"), true);
    fr.readAsDataURL(file);
  }, [setRef]);

  const onRemoveRef = useCallback((id: string, index: number) => {
    setCards((cs) => cs.map((c) => (c.id === id
      ? { ...c, references: c.references.filter((_, i) => i !== index) }
      : c)));
  }, []);

  /** Reference order is meaning (the first slot leads), so moving a slot is a first-class operation. */
  const onMoveRef = useCallback((id: string, index: number, delta: number) => {
    setCards((cs) => cs.map((c) => {
      if (c.id !== id) return c;
      const to = index + delta;
      if (to < 0 || to >= c.references.length) return c;
      const references = c.references.slice();
      const [moved] = references.splice(index, 1);
      references.splice(to, 0, moved);
      return { ...c, references };
    }));
  }, []);

  const onRemove = useCallback((id: string) => {
    if (!confirm(tRef.current("canvas.removeCardConfirm"))) return;
    setCards((cs) => cs.filter((c) => c.id !== id));
  }, []);

  const onOpenResult = useCallback((id: string, index: number) => setLightbox({ id, index }), []);
  const onPick = useCallback((id: string, index: number) => setPicker({ id, index }), []);

  /** A result becomes the reference of a *new* card placed to the right of this one: the parent keeps its own
      request, and the new card is where the next prompt is written. */
  const onUseResult = useCallback((id: string, c: Candidate) => {
    const src = cardsRef.current.find((x) => x.id === id);
    const job_id = src?.jobId || "";
    if (!src || !job_id) return;
    const d = defaultsRef.current;
    const ref: CanvasReference = {
      ...newReference(),
      kind: "job",
      job_id,
      file: candidatePath(c.file),
      label: clampLabel(`${src.prompt.trim().slice(0, 20) || tRef.current("canvas.cardRef")} · ${c.file}`),
      url: c.thumb || c.url,
    };
    setCards((cs) => [...cs, newCard({
      x: src.x + CARD_W + 26,
      y: src.y,
      style: src.style || d.style,
      model: src.model || d.model,
      variations: src.variations || d.variations,
      prompt: src.prompt,
      references: [ref],
    })]);
    toast(tRef.current("canvas.referenceCardCreated"));
  }, []);

  const onMake3D = useCallback((id: string, c: Candidate) => {
    const card = cardsRef.current.find((x) => x.id === id);
    const job = card?.jobId ? jobsRef.current[card.jobId] : null;
    if (!job) return;
    setAsset({ job, file: c.file });
  }, []);

  const onDragStart = useCallback((id: string, e: React.PointerEvent<HTMLElement>) => {
    // A button inside the header (delete) is a click, not a drag.
    if (e.button !== 0 || spaceRef.current || (e.target as HTMLElement).closest("button")) return;
    const card = cardsRef.current.find((c) => c.id === id);
    if (!card) return;
    drag.current = { id, x: e.clientX, y: e.clientY, cx: card.x, cy: card.y };
    setDragging(true);
    const el = viewportRef.current;
    el?.setPointerCapture(e.pointerId);
    e.stopPropagation();
  }, []);

  /** Start a pan: the empty background, the middle button anywhere, or a left drag while space is held. */
  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (drag.current) return;
    const onCard = !!(e.target as HTMLElement).closest("[data-card-id]");
    const middle = e.button === 1;
    if (!middle && !(e.button === 0 && (spaceRef.current || !onCard))) return;
    pan.current = { x: e.clientX, y: e.clientY, vx: viewRef.current.x, vy: viewRef.current.y };
    setPanning(true);
    viewportRef.current?.setPointerCapture(e.pointerId);
    e.preventDefault();
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const p = pan.current;
    if (p) {
      setView({ x: p.vx + (e.clientX - p.x), y: p.vy + (e.clientY - p.y), k: viewRef.current.k });
      return;
    }
    const d = drag.current;
    if (d) {
      // Screen pixels to world units: a drag has to move the card by what the hand moved, at any zoom.
      const k = viewRef.current.k;
      patch(d.id, { x: Math.round(d.cx + (e.clientX - d.x) / k), y: Math.round(d.cy + (e.clientY - d.y) / k) });
    }
  };

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    pan.current = null;
    drag.current = null;
    setPanning(false);
    setDragging(false);
    const el = viewportRef.current;
    if (el?.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId);
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

  const zoomBy = (factor: number) => {
    const el = viewportRef.current;
    const r = el?.getBoundingClientRect();
    setView((v) => zoomAbout(v, factor, (r?.width ?? 900) / 2, (r?.height ?? 600) / 2));
  };

  const fit = () => {
    const el = viewportRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    // Measured, not assumed: `offsetWidth` is the layout size, which a transform does not touch, so it is exactly
    // the card's size in world units however far the canvas is zoomed out.
    const boxes = cardsRef.current.map((c) => {
      const node = el.querySelector<HTMLElement>(`[data-card-id="${c.id}"]`);
      return node
        ? { x: c.x, y: c.y, w: node.clientWidth, h: node.clientHeight }
        : { x: c.x, y: c.y, w: CARD_W, h: 300 };
    });
    setView(fitView(boxes, r.width, r.height));
  };

  const target = picker ? cards.find((c) => c.id === picker.id) : null;
  const sources: PickerSource[] = cards
    .map((c, i) => ({ c, i }))
    .filter(({ c }) => c.id !== picker?.id && c.jobId && jobs[c.jobId])
    .map(({ c, i }) => ({
      key: c.id,
      label: c.prompt.trim().slice(0, 40) || t("canvas.cardN", { n: i + 1 }),
      job: jobs[c.jobId as string],
    }));

  const openCard = lightbox ? cards.find((c) => c.id === lightbox.id) : null;
  const openJob = openCard?.jobId ? jobs[openCard.jobId] : null;
  const openCandidates = openJob?.candidates || [];
  const items: LightboxItem[] = openCandidates.map((c) => ({
    url: withToken(c.url),
    preview: c.thumb ? withToken(c.thumb) : undefined,
    caption: c.file,
    note: c.seed != null ? t("session.seedLabel", { n: c.seed }) : "",
  }));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{t("canvas.title")}</h1>
          <p>{t("canvas.subtitle")}</p>
        </div>
        <div className="row">
          <button className="btn" onClick={() => addCard()}><SquarePlus size={14} /> {t("canvas.newCard")}</button>
        </div>
      </div>

      <div className="canvas-wrap">
        <div
          ref={viewportRef}
          className={"canvas-viewport" + (spaceDown ? " grab" : "") + (panning ? " panning" : "") + (dragging ? " dragging" : "")}
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
            {cards.map((c) => (
              <CanvasCard
                key={c.id}
                card={c}
                caps={caps}
                job={c.jobId ? jobs[c.jobId] || null : null}
                maxRefs={defaults.maxRefs}
                varMin={defaults.varMin}
                varMax={defaults.varMax}
                busy={busyId === c.id}
                onDragStart={onDragStart}
                onChange={patch}
                onRemove={onRemove}
                onGenerate={generate}
                onUpload={onUpload}
                onPick={onPick}
                onRemoveRef={onRemoveRef}
                onMoveRef={onMoveRef}
                onOpenResult={onOpenResult}
                onUseResult={onUseResult}
                onMake3D={onMake3D}
              />
            ))}
          </div>

          {!cards.length && (
            <div className="canvas-empty">
              <Empty
                icon={<SquarePlus size={22} />}
                title={t("canvas.emptyTitle")}
                hint={t("canvas.emptyHint")}
                action={<button className="btn primary" onClick={() => addCard()}><SquarePlus size={14} /> {t("canvas.newCard")}</button>}
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
            <button className="btn ghost sm" onClick={fit} title={t("canvas.fitHint")} disabled={!cards.length}>
              <Maximize2 size={13} /> {t("canvas.fit")}
            </button>
            <button className="btn ghost sm" onClick={() => setView(emptyView())} title={t("canvas.resetHint")}>
              <RotateCcw size={13} /> {t("canvas.reset")}
            </button>
            <span className="canvas-hint small muted">{t("canvas.panHint")}</span>
          </div>
        </div>
      </div>

      {target && picker && (
        <ReferencePicker
          sources={sources}
          onClose={() => setPicker(null)}
          onPick={(ref: PickedReference) => { setRef(picker.id, picker.index, { ...newReference(), kind: "job", ...ref }); setPicker(null); }}
        />
      )}

      {openCard && items.length > 0 && (
        <Lightbox
          items={items}
          index={Math.min(lightbox?.index ?? 0, items.length - 1)}
          onIndex={(n) => setLightbox({ id: openCard.id, index: n })}
          onClose={() => setLightbox(null)}
          action={(n) => {
            const c = openCandidates[n];
            // A candidate still being written has no file under artifacts/ yet, so it cannot be a reference or a source.
            if (!c || c.partial) return null;
            return (
              <>
                <button className="btn sm" onClick={() => onUseResult(openCard.id, c)}>
                  <Link2 size={13} /> {t("canvas.useAsReference")}
                </button>
                <button className="btn sm primary" onClick={() => onMake3D(openCard.id, c)}>
                  <Box size={13} /> {t("canvas.make3D")}
                </button>
              </>
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
            setAsset(null);
            toast(t("canvas.assetJobStarted", { n: created.length }));
          }}
        />
      )}
    </>
  );
}
