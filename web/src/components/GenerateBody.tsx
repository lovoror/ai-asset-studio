import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, ArrowRight, Check, ImagePlus, X } from "lucide-react";
import { api, withToken, type ImageModel, type PromptPreview } from "../api";
import { GenerateNode, ResolvedRef, generateOutput, resolveGenerate } from "../canvas";
import { useT } from "../i18n";
import { Progress, Spinner } from "./ui";
import type { NodeProps } from "./CanvasNode";

/** Which of the two prompts the pipeline will read, spelled out on the node: the composed text is the *default*,
    not a decision the pipeline keeps, so which one is in force has to be visible. */
type Mode = "composed" | "verbatim";

/** The 2D generation node.
 *
 * Everything it reads comes from the graph (`resolveGenerate`), so this component never holds the prompt or the
 * style itself - that is the whole difference from the card this replaced, where all of it was folded into one
 * form and could not be shared or reused.
 */
export default function GenerateBody(props: NodeProps) {
  const { node, graph, jobs, caps, maxRefs, varMin, varMax, fallbackStyle, busy,
          onChange, onGenerate, onOpenResult, onSelectOutput, onMoveRef, onRemoveRef } = props;
  const t = useT();
  // CanvasNode only renders this for a generate node; the cast is what keeps the hooks below unconditional.
  const gen = node as GenerateNode;
  const inputs = resolveGenerate(graph, gen, { defaultStyle: fallbackStyle, jobs });
  const [preview, setPreview] = useState<PromptPreview | null>(null);
  const [previewErr, setPreviewErr] = useState<string | null>(null);

  const job = gen.jobId ? jobs[gen.jobId] || null : null;
  const candidates = job?.candidates || [];
  const outputFile = generateOutput(gen, jobs)?.file ?? null;
  const mode: Mode = gen.verbatim ? "verbatim" : "composed";
  const jobError = (job?.error as { message?: string } | null)?.message || "";
  const want = inputs.prompt;
  // The palette is an array on every render, so the effect below keys on its text: depending on the array itself
  // would re-fetch the preview on every keystroke anywhere on the board.
  const paletteKey = inputs.palette.join(",");

  useEffect(() => {
    if (want.length < 3) { setPreview(null); setPreviewErr(null); return; }
    // Debounced: this is one request per keystroke otherwise, and the answer only matters once the hand stops.
    let live = true;
    const h = window.setTimeout(() => {
      api.promptPreview({
        prompt: want, style: inputs.style,
        materials: inputs.materials || undefined,
        palette: inputs.palette.length ? inputs.palette : undefined,
        negative_extra: inputs.negativeExtra || undefined,
      })
        .then((p) => { if (live) { setPreview(p); setPreviewErr(null); } })
        .catch((e: any) => { if (live) setPreviewErr(e.message || String(e)); });
    }, 450);
    return () => { live = false; window.clearTimeout(h); };
  }, [want, inputs.style, inputs.materials, inputs.negativeExtra, paletteKey]);

  const models: ImageModel[] = caps?.image_models || [];
  const model = models.find((m) => m.id === gen.model);
  const styleName = (caps?.styles?.[inputs.style]?.label) || inputs.style;

  /** Turning the toggle on seeds the fields with what the pipeline composed, so "edit the default" starts from the
      default instead of a blank box; anything already typed there is kept. */
  const setMode = (m: Mode) => {
    if (m === "composed") return onChange(gen.id, { verbatim: false });
    onChange(gen.id, {
      verbatim: true,
      finalPrompt: gen.finalPrompt || preview?.prompt || want,
      finalNegative: gen.finalNegative || preview?.negative_prompt || "",
    });
  };

  const num = (v: number | null) => (v == null ? "" : String(v));
  const toNum = (s: string) => {
    const n = Number(s.trim());
    return s.trim() === "" || !Number.isFinite(n) ? null : Math.round(n);
  };
  // The API rejects a size that is not a multiple of 16, so say so here rather than in a 422 after the click.
  const badSize = [gen.width, gen.height].some((v) => v != null && v % 16 !== 0);
  const noPrompt = want.length < 3;
  const tooShort = gen.verbatim && gen.finalPrompt.trim().length < 3;
  const blocked = busy || noPrompt || tooShort || badSize || inputs.unusable > 0;

  return (
    <>
      {noPrompt && <div className="callout danger small">{t("canvas.needPrompt")}</div>}

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
            <textarea className="input" rows={5} value={gen.finalPrompt}
                      onChange={(e) => onChange(gen.id, { finalPrompt: e.target.value })} />
            <div className="field">
              <label>{t("canvas.negative")}</label>
              <textarea className="input" rows={2} value={gen.finalNegative}
                        placeholder={preview?.negative_prompt || t("canvas.negativePlaceholder")}
                        onChange={(e) => onChange(gen.id, { finalNegative: e.target.value })} />
            </div>
            <div className="hint">{t("canvas.verbatimHint")}</div>
            {tooShort && <div className="callout danger small">{t("canvas.finalTooShort")}</div>}
          </>
        )}
        <div className="hint">
          {t("canvas.styleInForce", { name: styleName })}
          {inputs.styleFrom === "default" ? ` · ${t("canvas.styleFromSettings")}` : ""}
        </div>
      </div>

      {/* The wired-in references, in wire order. Removing a chip deletes its wire, which is the only way a
          reference can leave the node - it is not a field of this node any more. */}
      <div className="field">
        <label>{t("canvas.references", { n: inputs.refs.length, max: maxRefs })}</label>
        {inputs.refs.length > 0 && (
          <div className="canvas-refs">
            {inputs.refs.map((r, i) => (
              <div key={r.edge} className={"canvas-ref" + (r.body ? "" : " missing")}>
                <span className="canvas-ref-n">{i + 1}</span>
                <RefThumb r={r} />
                <button className="canvas-ref-x" onClick={() => onRemoveRef(gen.id, r.edge)}
                        title={t("canvas.edgeDelete")} aria-label={t("canvas.edgeDelete")}>
                  <X size={11} />
                </button>
                <div className="canvas-ref-ops">
                  <button disabled={i === 0} onClick={() => onMoveRef(gen.id, r.edge, -1)}
                          title={t("canvas.moveEarlier")} aria-label={t("canvas.moveEarlier")}>
                    <ArrowLeft size={11} />
                  </button>
                  <button disabled={i === inputs.refs.length - 1} onClick={() => onMoveRef(gen.id, r.edge, 1)}
                          title={t("canvas.moveLater")} aria-label={t("canvas.moveLater")}>
                    <ArrowRight size={11} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
        <div className="hint">{inputs.refs.length ? t("canvas.referencesHint") : t("canvas.refsEmpty")}</div>
        {inputs.unusable > 0 && <div className="callout danger small">{t("canvas.needRefs", { n: inputs.unusable })}</div>}
      </div>

      <div className="canvas-controls">
        <div className="field">
          <label>{t("create.imageModel")}</label>
          <select className="input" value={gen.model} onChange={(e) => onChange(gen.id, { model: e.target.value })}>
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
          <input className="input num" inputMode="numeric" value={String(gen.variations)}
                 onChange={(e) => {
                   const n = Number(e.target.value.replace(/[^\d]/g, ""));
                   const v = Number.isFinite(n) && n > 0 ? Math.min(varMax, Math.max(varMin, n)) : varMin;
                   onChange(gen.id, { variations: v });
                 }} />
        </div>
        <div className="field">
          <label>{t("canvas.width")}</label>
          <input className="input num" inputMode="numeric" value={num(gen.width)} placeholder={autoSize("w", model)}
                 onChange={(e) => onChange(gen.id, { width: toNum(e.target.value) })} />
        </div>
        <div className="field">
          <label>{t("canvas.height")}</label>
          <input className="input num" inputMode="numeric" value={num(gen.height)} placeholder={autoSize("h", model)}
                 onChange={(e) => onChange(gen.id, { height: toNum(e.target.value) })} />
        </div>
        <div className="field">
          <label>{t("canvas.steps")}</label>
          <input className="input num" inputMode="numeric" value={num(gen.steps)}
                 placeholder={model?.params?.steps ? String(model.params.steps) : t("canvas.auto")}
                 onChange={(e) => onChange(gen.id, { steps: toNum(e.target.value) })} />
        </div>
        <div className="field">
          <label>{t("canvas.cfg")}</label>
          <input className="input num" inputMode="decimal" value={num(gen.cfg)} placeholder={t("canvas.auto")}
                 onChange={(e) => {
                   const s = e.target.value.trim();
                   const n = Number(s);
                   onChange(gen.id, { cfg: s === "" || !Number.isFinite(n) ? null : n });
                 }} />
        </div>
      </div>

      <button className="btn primary block" disabled={blocked} onClick={() => onGenerate(gen.id)}>
        {busy ? <Spinner /> : <ImagePlus size={14} />} {busy ? t("common.submitting") : t("create.generate")}
      </button>

      {gen.error && <div className="callout danger small">{t("canvas.submitFailed")}: {gen.error}</div>}
      {job && job.status === "running" && <Progress job={job} />}
      {jobError && <div className="callout danger small">{t("canvas.jobFailed")}: {jobError}</div>}
      {job && (
        <Link className="link small" to={job.kind === "image" ? `/sessions/${job.id}` : `/assets/${job.id}`}>
          {t("canvas.openJob")}
        </Link>
      )}

      {candidates.length > 0 && (
        <>
          <div className="canvas-results">
            {candidates.map((c, i) => (
              <div key={c.file} className={"canvas-result" + (c.file === outputFile ? " is-output" : "")}>
                <button className="canvas-result-img" onClick={() => onOpenResult(gen.id, i)} title={t("cand.open")}>
                  <img src={withToken(c.thumb || c.url)} alt="" loading="lazy" />
                </button>
                <div className="canvas-result-ops">
                  <button className={c.file === outputFile ? "on" : ""} disabled={!!c.partial}
                          onClick={() => onSelectOutput(gen.id, c.file)}
                          title={t("canvas.setAsOutput")} aria-label={t("canvas.setAsOutput")}>
                    <Check size={12} />
                  </button>
                </div>
              </div>
            ))}
          </div>
          <div className="hint">
            {outputFile ? t("canvas.outputIs", { file: outputFile }) : t("canvas.outputNone")}
          </div>
        </>
      )}
    </>
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

/** A reference chip's picture: a wire from an image node draws that node's bytes, a wire from another generate
    node draws the served thumbnail of the candidate it carries. */
function RefThumb({ r }: { r: ResolvedRef }) {
  if (r.b64) return <img src={`data:image/png;base64,${r.b64}`} alt="" />;
  if (r.url) return <img src={withToken(r.url)} alt="" />;
  return <span className="canvas-ref-none">?</span>;
}
