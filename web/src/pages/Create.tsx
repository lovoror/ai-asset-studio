import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { AlertTriangle, Dice5, Sparkles, ChevronDown, ChevronUp, Wand2, PenLine } from "lucide-react";
import { api, Example, ImageModel, StyleEditRecord } from "../api";
import { useReadiness } from "../hooks";
import { useFmt, useGate, useT } from "../i18n";
import { useStore } from "../store";
import { Spinner } from "../components/ui";

interface StyleInfo {
  label: string; description?: string; best_for?: string; style_clause?: string; background?: string; negative_extra?: string;
  optimize_defaults?: Record<string, any>;
}
interface StyleEdit { label: string; style_clause: string; background: string; negative_extra: string; target_triangles: string; texture_size: string }

// Every style is editable: the editor is prefilled from the preset and edits are saved per style in the app's settings
// table (SQLite), so they survive browser resets and are shared with the API/CLI. localStorage is only a fallback.
const EDITS_KEY = "as-style-edits";
// The custom style's starting label is stored data (it goes into the settings table), so it is deliberately not
// translated here; the editor's placeholder shows the localised suggestion instead.
const CUSTOM_BASE: StyleEdit = { label: "My style", style_clause: "", background: "plain flat light grey studio background", negative_extra: "", target_triangles: "30000", texture_size: "2048" };
function presetEdit(id: string, s?: StyleInfo): StyleEdit {
  if (id === "custom" || !s) return CUSTOM_BASE;
  return {
    label: s.label || id, style_clause: s.style_clause || "", background: s.background || "plain flat light grey studio background",
    negative_extra: s.negative_extra || "", target_triangles: String(s.optimize_defaults?.target_triangles ?? 30000),
    texture_size: String(s.optimize_defaults?.texture_size ?? 2048),
  };
}
function loadEdits(): Record<string, StyleEdit> {
  try { const v = localStorage.getItem(EDITS_KEY); if (v) return JSON.parse(v); } catch {}
  return {};
}
function fromRecords(r?: Record<string, StyleEditRecord>): Record<string, StyleEdit> {
  const out: Record<string, StyleEdit> = {};
  for (const [id, e] of Object.entries(r || {})) {
    out[id] = {
      label: e.label || "", style_clause: e.style_clause || "", background: e.background || "", negative_extra: e.negative_extra || "",
      target_triangles: e.target_triangles ? String(e.target_triangles) : "", texture_size: e.texture_size ? String(e.texture_size) : "",
    };
  }
  return out;
}
function toRecords(m: Record<string, StyleEdit>): Record<string, StyleEditRecord> {
  const out: Record<string, StyleEditRecord> = {};
  for (const [id, e] of Object.entries(m)) {
    if (!e.style_clause.trim()) continue;
    out[id] = {
      label: e.label.trim() || undefined, style_clause: e.style_clause.trim(), background: e.background.trim() || undefined,
      negative_extra: e.negative_extra.trim() || undefined,
      target_triangles: e.target_triangles ? Number(e.target_triangles) : undefined, texture_size: e.texture_size ? Number(e.texture_size) : undefined,
    };
  }
  return out;
}
function sameEdit(a: StyleEdit, b: StyleEdit) {
  return (Object.keys(a) as (keyof StyleEdit)[]).every((k) => (a[k] || "").trim() === (b[k] || "").trim());
}

export default function Create() {
  const nav = useNavigate();
  const { settings, saveSettings, toast } = useStore();
  const t = useT();
  const { dur } = useFmt();
  const gate = useGate();
  const { readiness } = useReadiness();
  const blocked = !!readiness && !readiness.create_image.ready;
  const [examples, setExamples] = useState<Example[]>([]);
  const [styles, setStyles] = useState<Record<string, StyleInfo>>({});
  const [models, setModels] = useState<ImageModel[]>([]);
  const [model, setModel] = useState<string>("qwen-image-2512");
  const [prompt, setPrompt] = useState("");
  const [style, setStyle] = useState("mobile_factory");
  const [variations, setVariations] = useState(4);
  const [seed, setSeed] = useState("");
  const [height, setHeight] = useState("");
  const [materials, setMaterials] = useState("");
  const [palette, setPalette] = useState("");
  const [negative, setNegative] = useState("");
  const [title, setTitle] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [edits, setEdits] = useState<Record<string, StyleEdit>>(loadEdits);
  const [editing, setEditing] = useState(false);
  const saveTimer = useRef<number | null>(null);
  useEffect(() => {  // server copy wins once settings arrive
    if (settings?.style_edits) setEdits(fromRecords(settings.style_edits));
  }, [settings?.style_edits]);
  const base = presetEdit(style, styles[style]);
  const curEdit: StyleEdit = edits[style] ?? base;
  const edited = style === "custom" ? curEdit.style_clause.trim().length > 0 : !sameEdit(curEdit, base);
  const persist = (next: Record<string, StyleEdit>) => {
    setEdits(next);
    try { localStorage.setItem(EDITS_KEY, JSON.stringify(next)); } catch {}
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      // only real edits are stored: presets that match their defaults are dropped from the map
      const clean: Record<string, StyleEdit> = {};
      for (const [id, e] of Object.entries(next)) {
        if (id === "custom" ? e.style_clause.trim() : !sameEdit(e, presetEdit(id, styles[id]))) clean[id] = e;
      }
      saveSettings({ style_edits: toRecords(clean) }).catch((err: any) => toast(t("create.errSaveEdits", { msg: err.message || err }), true));
    }, 600);
  };
  const setEdit = (patch: Partial<StyleEdit>) => persist({ ...edits, [style]: { ...curEdit, ...patch } });
  const resetEdit = () => {
    const next = { ...edits };
    delete next[style];
    persist(next);
  };

  useEffect(() => {
    api.examples().then((r) => setExamples(r.items)).catch(() => {});
    api.capabilities().then((c) => { setStyles(c.styles); setModels(c.image_models || []); }).catch(() => {});
  }, []);
  useEffect(() => {
    if (settings) {
      setVariations(settings.default_variations);
      setStyle(settings.default_style);
      if (settings.default_image_model) setModel(settings.default_image_model);
    }
  }, [settings]);

  const apply = (ex: Example) => {
    setPrompt(ex.prompt);
    setStyle(ex.style);
    setHeight(ex.height_m ? String(ex.height_m) : "");
    setTitle(ex.title);
  };
  const surprise = () => examples.length && apply(examples[Math.floor(Math.random() * examples.length)]);
  const cur = models.find((m) => m.id === model);
  const estSeconds = variations * (cur?.est_s ?? 290) + (cur?.family === "qwen" ? 40 : 20); // + one-time model load

  const submit = async () => {
    if (prompt.trim().length < 3) return toast(t("create.errPrompt"), true);
    if (style === "custom" && curEdit.style_clause.trim().length < 10) return toast(t("create.errCustomStyle"), true);
    if (edited && curEdit.style_clause.trim().length < 10) return toast(t("create.errStyleShort"), true);
    setBusy(true);
    try {
      const body: Record<string, unknown> = { prompt: prompt.trim(), style, variations, model, title: title || undefined };
      if (edited) {
        body.custom_style = {
          label: curEdit.label.trim() || undefined, style_clause: curEdit.style_clause.trim(),
          background: curEdit.background.trim() || undefined, negative_extra: curEdit.negative_extra.trim(),
          target_triangles: curEdit.target_triangles ? Number(curEdit.target_triangles) : undefined,
          texture_size: curEdit.texture_size ? Number(curEdit.texture_size) : undefined,
        };
      }
      if (seed) body.seed = Number(seed);
      if (height) body.height_m = Number(height);
      if (materials) body.materials = materials;
      if (negative) body.negative_extra = negative;
      if (palette) body.palette = palette.split(",").map((s) => s.trim()).filter(Boolean);
      const r = await api.createImageJob(body);
      nav(`/sessions/${r.job_id}`);
    } catch (e: any) {
      toast(e.message || String(e), true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{t("create.title")}</h1>
          <p>{t("create.subtitle", { n: variations })}</p>
        </div>
      </div>
      <div className="split create">
        <div className="card pad stack loose">
          <div className="field">
            <label>{t("create.prompt")}</label>
            <textarea
              className="input"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder={t("create.promptPlaceholder")}
              onKeyDown={(e) => (e.ctrlKey || e.metaKey) && e.key === "Enter" && submit()}
            />
            <div className="hint">{t("create.promptHint", { kbd: "Ctrl+Enter" })}</div>
          </div>

          <div className="field">
            <label>{t("create.examples")}</label>
            <div className="examples">
              <button onClick={surprise} title={t("create.surpriseTitle")}><Dice5 size={14} /> {t("create.surprise")}</button>
              {examples.map((ex) => <button key={ex.id} onClick={() => apply(ex)}>{ex.title}</button>)}
            </div>
          </div>

          <div className="field">
            <label>{t("create.imageModel")}</label>
            <div className="styles">
              {models.map((m) => (
                <button key={m.id} className={"style-card" + (model === m.id ? " active" : "")} disabled={m.available === false}
                        onClick={() => setModel(m.id)} title={m.available === false ? m.reason : m.description}>
                  <b>{m.label} <span className={"chip " + (m.tier === "fast" ? "accent" : "")}>{m.tier === "fast" ? t("create.fast") : t("create.quality")}</span></b>
                  <span>{t("create.modelMeta", { time: m.est_s < 60 ? `~${m.est_s} s` : `~${Math.round(m.est_s / 60)} min`, steps: m.params?.steps, w: m.params?.width })}{m.available === false ? t("settings.unavailableSuffix") : ""}</span>
                </button>
              ))}
            </div>
            {cur && <div className="hint">{cur.description}</div>}
          </div>

          <div className="field">
            <label>{t("create.style")}</label>
            <div className="styles">
              {Object.entries(styles).map(([id, s]) => {
                const e = edits[id];
                const isEdited = !!e && !sameEdit(e, presetEdit(id, s));
                return (
                  <button key={id} className={"style-card" + (style === id ? " active" : "")} onClick={() => setStyle(id)} title={s.best_for}>
                    <b>{isEdited ? e.label || s.label : s.label || id}{isEdited && <span className="chip accent">{t("create.edited")}</span>}</b>
                    <span>{isEdited ? e.style_clause.slice(0, 110) + (e.style_clause.length > 110 ? "…" : "") : s.description}</span>
                    {s.optimize_defaults && (
                      <span className="budget">{t("create.budget", { tris: Number(isEdited ? e.target_triangles : s.optimize_defaults.target_triangles).toLocaleString(), tex: isEdited ? e.texture_size : s.optimize_defaults.texture_size })}</span>
                    )}
                  </button>
                );
              })}
              <button className={"style-card custom" + (style === "custom" ? " active" : "")} onClick={() => { setStyle("custom"); setEditing(true); }} title={t("create.customHint")}>
                <b><PenLine size={14} />{edits.custom?.style_clause?.trim() ? edits.custom.label || t("create.custom") : t("create.custom")}</b>
                <span>{edits.custom?.style_clause?.trim() ? edits.custom.style_clause.slice(0, 110) + (edits.custom.style_clause.length > 110 ? "…" : "") : t("create.customHint")}</span>
                <span className="budget">{t("create.budget", { tris: Number(edits.custom?.target_triangles || 30000).toLocaleString(), tex: edits.custom?.texture_size || 2048 })}</span>
              </button>
            </div>
            <div className="row tight">
              {style !== "custom" && styles[style]?.best_for && <div className="hint">{t("create.goodFor", { v: styles[style].best_for })}</div>}
              <div className="row tight" style={{ marginLeft: "auto" }}>
                <button className="btn ghost sm" onClick={() => setEditing(!editing)}>
                  <PenLine size={13} /> {editing ? t("create.hideEditor") : edited ? t("create.editThisStyleEdited") : t("create.editThisStyle")}
                </button>
                {edited && style !== "custom" && <button className="btn ghost sm" onClick={resetEdit}>{t("create.resetToPreset")}</button>}
              </div>
            </div>
            {(editing || style === "custom") && (
              <div className="card pad custom-style stack">
                <div className="grid cols-2">
                  <div className="field"><label>{t("create.styleName")}</label><input className="input" value={curEdit.label} maxLength={40} onChange={(e) => setEdit({ label: e.target.value })} placeholder={t("create.myStyle")} /></div>
                  <div className="field"><label>{t("create.background")}</label><input className="input" value={curEdit.background} maxLength={120} onChange={(e) => setEdit({ background: e.target.value })} placeholder={t("create.backgroundPlaceholder")} /></div>
                </div>
                <div className="field">
                  <label>{t("create.howItLooks")}</label>
                  <textarea className="input" value={curEdit.style_clause} maxLength={600} onChange={(e) => setEdit({ style_clause: e.target.value })}
                    placeholder={t("create.howItLooksPlaceholder")} />
                  <div className="hint">{t("create.howItLooksHint")}</div>
                </div>
                <div className="grid" style={{ gridTemplateColumns: "2fr 1fr 1fr" }}>
                  <div className="field"><label>{t("create.keepOut")}</label><input className="input" value={curEdit.negative_extra} maxLength={300} onChange={(e) => setEdit({ negative_extra: e.target.value })} placeholder={t("create.keepOutPlaceholder")} /></div>
                  <div className="field"><label>{t("create.triangleBudget")}</label><input className="input num" value={curEdit.target_triangles} inputMode="numeric" onChange={(e) => setEdit({ target_triangles: e.target.value.replace(/\D/g, "") })} placeholder="30000" /></div>
                  <div className="field"><label>{t("create.texture")}</label>
                    <select className="input" value={curEdit.texture_size} onChange={(e) => setEdit({ texture_size: e.target.value })}>
                      {[512, 1024, 2048, 4096].map((n) => <option key={n} value={String(n)}>{t("common.px", { n })}</option>)}
                    </select>
                  </div>
                </div>
              </div>
            )}
          </div>

          <div>
            <button className="btn ghost sm" onClick={() => setAdvanced(!advanced)}>
              {advanced ? <ChevronUp size={14} /> : <ChevronDown size={14} />} {t("create.advanced")}
            </button>
            {advanced && (
              <div className="grid cols-2" style={{ marginTop: 12 }}>
                <div className="field"><label>{t("create.materials")}</label><input className="input" value={materials} onChange={(e) => setMaterials(e.target.value)} placeholder={t("create.materialsPlaceholder")} /></div>
                <div className="field"><label>{t("create.palette")}</label><input className="input" value={palette} onChange={(e) => setPalette(e.target.value)} placeholder={t("create.palettePlaceholder")} /></div>
                <div className="field"><label>{t("create.avoid")}</label><input className="input" value={negative} onChange={(e) => setNegative(e.target.value)} placeholder={t("create.avoidPlaceholder")} /></div>
                <div className="field"><label>{t("create.height")}</label><input className="input num" value={height} onChange={(e) => setHeight(e.target.value)} placeholder="2.0" inputMode="decimal" /></div>
                <div className="field"><label>{t("create.seed")}</label><input className="input num" value={seed} onChange={(e) => setSeed(e.target.value.replace(/\D/g, ""))} placeholder={t("create.seedPlaceholder")} /></div>
                <div className="field"><label>{t("create.titleField")}</label><input className="input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t("create.titlePlaceholder")} /></div>
              </div>
            )}
          </div>
        </div>

        <div className="card pad stack sticky-col">
          <div className="field">
            <label>{t("create.variationsLabel")}</label>
            <div className="segmented">
              {[1, 2, 3, 4, 6, 8].map((n) => (
                <button key={n} className={variations === n ? "active" : ""} onClick={() => setVariations(n)}>{n}</button>
              ))}
            </div>
            <div className="hint">{t("create.variationsHint", { est: dur(estSeconds), model: cur?.label || t("create.imageModel") })}</div>
          </div>
          <div className="sep" />
          {blocked && (
            <div className="callout danger">
              <div className="callout-title"><AlertTriangle size={14} /> {t("gate.blockedTitle")}</div>
              <ul>
                {gate(readiness.create_image.problems).map((s, i) => <li key={i}>{s}</li>)}
              </ul>
              <Link className="link small" to="/settings">{t("settings.backends")} →</Link>
            </div>
          )}
          <div className="stack tight small muted">
            <div><Wand2 size={13} style={{ verticalAlign: -2 }} /> {t("create.tipSeed")}</div>
            <div>{t("create.tipFraming")}</div>
            <div>{t("create.tipNo3d")}</div>
          </div>
          <button className="btn primary lg block" disabled={busy || blocked} onClick={submit}>
            {busy ? <Spinner /> : <Sparkles size={16} />} {busy ? t("common.submitting") : t("create.generate")}
          </button>
        </div>
      </div>
    </>
  );
}
