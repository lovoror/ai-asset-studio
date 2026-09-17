import { memo, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, ArrowRight, Box, GripVertical, ImagePlus, Link2, Trash2, Upload, X } from "lucide-react";
import { api, Candidate, ImageModel, Job, PromptPreview, withToken } from "../api";
import { CanvasCard as Card, CanvasReference, CARD_W, referenceReady } from "../canvas";
import { useT } from "../i18n";
import { Progress, Spinner, StatusChip } from "./ui";

/** Which of the two prompts the pipeline will read, spelled out on the card: the composed text is the *default*, not
    a decision the pipeline keeps, so which one is in force has to be visible. */
type Mode = "composed" | "verbatim";

/** Every callback takes the card id rather than closing over it, so the page can hand out one stable set of handlers
    and a pan (which re-renders the page on every frame) does not re-render every card. */
interface Props {
  card: Card;
  /** /capabilities, passed down so every option and range comes from the running server rather than a table here. */
  caps: any;
  /** This card's image job, fetched by the page. */
  job: Job | null;
  maxRefs: number;
  varMin: number;
  varMax: number;
  busy: boolean;
  /** Pointer-down on the card header, which is how the card is dragged around the canvas. */
  onDragStart: (id: string, e: React.PointerEvent<HTMLElement>) => void;
  onChange: (id: string, patch: Partial<Card>) => void;
  onRemove: (id: string) => void;
  onGenerate: (id: string) => void;
  onUpload: (id: string, index: number, file: File) => void;
  onPick: (id: string, index: number) => void;
  onRemoveRef: (id: string, index: number) => void;
  onMoveRef: (id: string, index: number, delta: number) => void;
  onOpenResult: (id: string, index: number) => void;
  onUseResult: (id: string, candidate: Candidate) => void;
  onMake3D: (id: string, candidate: Candidate) => void;
}

function CanvasCard({
  card, caps, job, maxRefs, varMin, varMax, busy, onDragStart, onChange, onRemove, onGenerate,
  onUpload, onPick, onRemoveRef, onMoveRef, onOpenResult, onUseResult, onMake3D,
}: Props) {
  const t = useT();
  const [preview, setPreview] = useState<PromptPreview | null>(null);
  const [previewErr, setPreviewErr] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  // Which slot the hidden file input is filling. A ref, not state: the dialog resolves long after this click.
  const uploadTarget = useRef(0);
  const id = card.id;

  const want = card.prompt.trim();
  useEffect(() => {
    if (want.length < 3) { setPreview(null); setPreviewErr(null); return; }
    // Debounced: this is one request per keystroke otherwise, and the answer only matters once the hand stops.
    let live = true;
    const h = window.setTimeout(() => {
      api.promptPreview({ prompt: want, style: card.style })
        .then((p) => { if (live) { setPreview(p); setPreviewErr(null); } })
        .catch((e: any) => { if (live) setPreviewErr(e.message || String(e)); });
    }, 450);
    return () => { live = false; window.clearTimeout(h); };
  }, [want, card.style]);

  const styles: [string, any][] = Object.entries(caps?.styles || {});
  const models: ImageModel[] = caps?.image_models || [];
  const model = models.find((m) => m.id === card.model);
  const mode: Mode = card.verbatim ? "verbatim" : "composed";
  const jobError = (job?.error as { message?: string } | null)?.message || "";
  const candidates = job?.candidates || [];

  /** Turning the toggle on seeds the fields with what the pipeline composed, so "edit the default" starts from the
      default instead of a blank box; anything already typed there is kept. */
  const setMode = (m: Mode) => {
    if (m === "composed") return onChange(id, { verbatim: false });
    onChange(id, {
      verbatim: true,
      finalPrompt: card.finalPrompt || preview?.prompt || want,
      finalNegative: card.finalNegative || preview?.negative_prompt || "",
    });
  };

  const num = (v: number | null) => (v == null ? "" : String(v));
  const toNum = (s: string) => {
    const n = Number(s.trim());
    return s.trim() === "" || !Number.isFinite(n) ? null : Math.round(n);
  };
  // The API rejects a size that is not a multiple of 16, so say so here rather than in a 422 after the click.
  const badSize = [card.width, card.height].some((v) => v != null && v % 16 !== 0);
  const tooShort = card.verbatim && card.finalPrompt.trim().length < 3;
  const noPrompt = want.length < 3;
  // A slot with nothing usable in it means generating now would silently drop a reference, so it blocks instead.
  const droppedRefs = card.references.filter((r) => !referenceReady(r)).length;

  return (
    <div className="canvas-card" data-card-id={id} style={{ left: card.x, top: card.y, width: CARD_W }}>
      <div className="canvas-card-head" data-card-drag onPointerDown={(e) => onDragStart(id, e)}>
        <GripVertical size={14} className="grip" />
        <span className={"chip sm " + (mode === "verbatim" ? "warn" : "")}>
          {mode === "verbatim" ? t("canvas.modeVerbatim") : t("canvas.modeComposed")}
        </span>
        <div className="row tight" style={{ marginLeft: "auto" }}>
          {job && <StatusChip job={job} />}
          <button className="btn ghost icon sm" onClick={() => onRemove(id)} title={t("canvas.removeCard")} aria-label={t("canvas.removeCard")}>
            <Trash2 size={13} />
          </button>
        </div>
      </div>

      <div className="field">
        <label>{t("create.prompt")}</label>
        <textarea
          className="input"
          rows={3}
          value={card.prompt}
          onChange={(e) => onChange(id, { prompt: e.target.value })}
          placeholder={t("create.promptPlaceholder")}
          onKeyDown={(e) => (e.ctrlKey || e.metaKey) && e.key === "Enter" && onGenerate(id)}
        />
      </div>

      <div className="canvas-composed">
        <div className="canvas-composed-head">
          <span className="small muted">
            {mode === "verbatim" ? t("canvas.sendingVerbatim") : t("canvas.sendingComposed")}
          </span>
          <div className="segmented" role="group" aria-label={t("canvas.modeLabel")}>
            <button type="button" className={mode === "composed" ? "active" : ""} onClick={() => setMode("composed")}>
              {t("canvas.modeComposed")}
            </button>
            <button type="button" className={mode === "verbatim" ? "active" : ""} onClick={() => setMode("verbatim")}>
              {t("canvas.modeVerbatim")}
            </button>
          </div>
        </div>

        {previewErr && <div className="callout danger small">{t("canvas.composeFailed")}: {previewErr}</div>}

        {mode === "composed" ? (
          <>
            <div className="canvas-text">
              {preview ? preview.prompt : t(noPrompt ? "canvas.composedEmpty" : "canvas.composing")}
            </div>
            {preview?.negative_prompt && (
              <div className="canvas-negative"><b>{t("canvas.negative")}</b> {preview.negative_prompt}</div>
            )}
            {preview && parts(preview).length > 0 && (
              <details className="canvas-parts">
                <summary>{t("canvas.parts", { n: parts(preview).length })}</summary>
                <div className="stack tight">
                  {parts(preview).map(([k, v]) => (
                    <div key={k} className="canvas-part">
                      <b>{t(`canvas.part.${k}`)}</b>
                      <span>{Array.isArray(v) ? v.join(", ") : String(v)}</span>
                    </div>
                  ))}
                </div>
              </details>
            )}
          </>
        ) : (
          <>
            <textarea className="input" rows={5} value={card.finalPrompt}
                      onChange={(e) => onChange(id, { finalPrompt: e.target.value })} />
            <div className="field">
              <label>{t("canvas.negative")}</label>
              <textarea className="input" rows={2} value={card.finalNegative}
                        placeholder={preview?.negative_prompt || t("canvas.negativePlaceholder")}
                        onChange={(e) => onChange(id, { finalNegative: e.target.value })} />
            </div>
            <div className="hint">{t("canvas.verbatimHint")}</div>
            {tooShort && <div className="callout danger small">{t("canvas.finalTooShort")}</div>}
          </>
        )}
      </div>

      <div className="field">
        <label>{t("canvas.references", { n: card.references.length, max: maxRefs })}</label>
        <div className="canvas-refs">
          {card.references.map((r, i) => (
            <div key={r.id} className={"canvas-ref" + (referenceReady(r) ? "" : " missing")}>
              <span className="canvas-ref-n">{i + 1}</span>
              <RefThumb r={r} />
              <button className="canvas-ref-x" onClick={() => onRemoveRef(id, i)} title={t("common.delete")} aria-label={t("common.delete")}>
                <X size={11} />
              </button>
              <div className="canvas-ref-ops">
                <button disabled={i === 0} onClick={() => onMoveRef(id, i, -1)} title={t("canvas.moveEarlier")} aria-label={t("canvas.moveEarlier")}>
                  <ArrowLeft size={11} />
                </button>
                <button disabled={i === card.references.length - 1} onClick={() => onMoveRef(id, i, 1)}
                        title={t("canvas.moveLater")} aria-label={t("canvas.moveLater")}>
                  <ArrowRight size={11} />
                </button>
              </div>
            </div>
          ))}
          {card.references.length < maxRefs && (
            <>
              <button className="canvas-ref add" title={t("canvas.upload")} aria-label={t("canvas.upload")}
                      onClick={() => { uploadTarget.current = card.references.length; fileRef.current?.click(); }}>
                <Upload size={14} />
              </button>
              <button className="canvas-ref add" title={t("canvas.pick")} aria-label={t("canvas.pick")}
                      onClick={() => onPick(id, card.references.length)}>
                <ImagePlus size={14} />
              </button>
            </>
          )}
        </div>
        <div className="hint">{t("canvas.referencesHint")}</div>
        {droppedRefs > 0 && <div className="callout danger small">{t("canvas.referenceDropped", { n: droppedRefs })}</div>}
        <input ref={fileRef} type="file" accept="image/png,image/jpeg" hidden
               onChange={(e) => {
                 const f = e.target.files?.[0];
                 if (f) onUpload(id, uploadTarget.current, f);
                 e.target.value = "";
               }} />
      </div>

      <div className="canvas-controls">
        <div className="field">
          <label>{t("create.style")}</label>
          <select className="input" value={card.style} onChange={(e) => onChange(id, { style: e.target.value })}>
            {styles.map(([sid, s]) => <option key={sid} value={sid}>{s?.label || sid}</option>)}
          </select>
        </div>
        <div className="field">
          <label>{t("create.imageModel")}</label>
          <select className="input" value={card.model} onChange={(e) => onChange(id, { model: e.target.value })}>
            <option value="">{t("canvas.modelAuto")}</option>
            {models.map((m) => (
              <option key={m.id} value={m.id} disabled={m.available === false}>
                {m.label}{m.available === false ? ` — ${m.reason || t("settings.unavailableSuffix")}` : ""}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>{t("create.variationsLabel")}</label>
          <input className="input num" inputMode="numeric" value={String(card.variations)}
                 onChange={(e) => {
                   const n = Number(e.target.value.replace(/[^\d]/g, ""));
                   const v = Number.isFinite(n) && n > 0 ? Math.min(varMax, Math.max(varMin, n)) : varMin;
                   onChange(id, { variations: v });
                 }} />
        </div>
        <div className="field">
          <label>{t("canvas.width")}</label>
          <input className="input num" inputMode="numeric" value={num(card.width)} placeholder={autoSize("w", model)}
                 onChange={(e) => onChange(id, { width: toNum(e.target.value) })} />
        </div>
        <div className="field">
          <label>{t("canvas.height")}</label>
          <input className="input num" inputMode="numeric" value={num(card.height)} placeholder={autoSize("h", model)}
                 onChange={(e) => onChange(id, { height: toNum(e.target.value) })} />
        </div>
        <div className="field">
          <label>{t("canvas.steps")}</label>
          <input className="input num" inputMode="numeric" value={num(card.steps)}
                 placeholder={model?.params?.steps ? String(model.params.steps) : t("canvas.auto")}
                 onChange={(e) => onChange(id, { steps: toNum(e.target.value) })} />
        </div>
        <div className="field">
          <label>{t("canvas.cfg")}</label>
          <input className="input num" inputMode="decimal" value={num(card.cfg)} placeholder={t("canvas.auto")}
                 onChange={(e) => {
                   const s = e.target.value.trim();
                   const n = Number(s);
                   onChange(id, { cfg: s === "" || !Number.isFinite(n) ? null : n });
                 }} />
        </div>
      </div>

      <button className="btn primary block" disabled={busy || noPrompt || tooShort || badSize || droppedRefs > 0} onClick={() => onGenerate(id)}>
        {busy ? <Spinner /> : <ImagePlus size={14} />} {busy ? t("common.submitting") : t("create.generate")}
      </button>

      {card.error && <div className="callout danger small">{t("canvas.submitFailed")}: {card.error}</div>}
      {job && job.status === "running" && <Progress job={job} />}
      {jobError && <div className="callout danger small">{t("canvas.jobFailed")}: {jobError}</div>}
      {job && (
        <Link className="link small" to={job.kind === "image" ? `/sessions/${job.id}` : `/assets/${job.id}`}>
          {t("canvas.openJob")}
        </Link>
      )}

      {candidates.length > 0 && (
        <div className="canvas-results">
          {candidates.map((c, i) => (
            <div key={c.file} className="canvas-result">
              <button className="canvas-result-img" onClick={() => onOpenResult(id, i)} title={t("cand.open")}>
                <img src={withToken(c.thumb || c.url)} alt="" loading="lazy" />
              </button>
              <div className="canvas-result-ops">
                <button onClick={() => onUseResult(id, c)} title={t("canvas.useAsReference")} aria-label={t("canvas.useAsReference")}>
                  <Link2 size={12} />
                </button>
                <button onClick={() => onMake3D(id, c)} title={t("canvas.make3D")} aria-label={t("canvas.make3D")}>
                  <Box size={12} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Only the template parts the server actually filled in, so the list is the composition and not the schema. */
function parts(p: PromptPreview): [string, unknown][] {
  return Object.entries(p.template).filter(([, v]) => v != null && String(v).length > 0);
}

/** The model preset's own size, shown as the placeholder so "auto" says what auto means right now. */
function autoSize(which: "w" | "h", model?: ImageModel) {
  const n = which === "w" ? model?.params?.width : model?.params?.height;
  return n ? String(n) : undefined;
}

/** A slot's picture: an upload draws from its own bytes, a job reference from the URL the API served it on. */
function RefThumb({ r }: { r: CanvasReference }) {
  if (r.image_b64) return <img src={`data:image/png;base64,${r.image_b64}`} alt="" />;
  if (r.url) return <img src={withToken(r.url)} alt="" />;
  return <span className="canvas-ref-none">?</span>;
}

// The canvas re-renders on every pan frame; every prop here is stable while a card is untouched, so memo pays for it.
export default memo(CanvasCard);
