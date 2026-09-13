import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Dice5, Sparkles, ChevronDown, ChevronUp, Wand2, PenLine } from "lucide-react";
import { api, Example, ImageModel, StyleEditRecord } from "../api";
import { useStore } from "../store";

interface StyleInfo {
  label: string; description?: string; best_for?: string; style_clause?: string; background?: string; negative_extra?: string;
  optimize_defaults?: Record<string, any>;
}
interface StyleEdit { label: string; style_clause: string; background: string; negative_extra: string; target_triangles: string; texture_size: string }

// Every style is editable: the editor is prefilled from the preset and edits are saved per style in the app's settings
// table (SQLite), so they survive browser resets and are shared with the API/CLI. localStorage is only a fallback.
const EDITS_KEY = "as-style-edits";
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
      saveSettings({ style_edits: toRecords(clean) }).catch((err: any) => toast("Could not save style edits: " + (err.message || err), true));
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
  const est = useMemo(() => {
    const per = cur?.est_s ?? 290;
    const s = variations * per + (cur?.family === "qwen" ? 40 : 20); // + one-time model load
    return s < 120 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`;
  }, [variations, cur]);

  const submit = async () => {
    if (prompt.trim().length < 3) return toast("Describe the object first", true);
    if (style === "custom" && curEdit.style_clause.trim().length < 10) return toast("Describe your custom style first (at least a short sentence)", true);
    if (edited && curEdit.style_clause.trim().length < 10) return toast("The style description is too short", true);
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
          <h1>Create</h1>
          <p>Describe one object. You'll get {variations} image variation{variations > 1 ? "s" : ""} to choose from before anything is built in 3D.</p>
        </div>
      </div>
      <div className="grid" style={{ gridTemplateColumns: "minmax(0, 2fr) minmax(280px, 1fr)", alignItems: "start" }}>
        <div className="card pad stack" style={{ gap: 16 }}>
          <div className="field">
            <label>Prompt</label>
            <textarea
              className="input"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Stylized industrial water pump station for a mobile factory game, chunky readable silhouette, painted teal metal, copper pipes, small concrete foundation"
              onKeyDown={(e) => (e.ctrlKey || e.metaKey) && e.key === "Enter" && submit()}
            />
            <div className="small faint">Single object, no text or lettering (models garble it). The pipeline adds framing, lighting and background rules for you. <kbd>Ctrl</kbd>+<kbd>Enter</kbd> to generate.</div>
          </div>
          <div className="field">
            <label>Examples</label>
            <div className="examples">
              <button onClick={surprise} title="Random example"><Dice5 size={14} style={{ verticalAlign: -2 }} /> Surprise me</button>
              {examples.map((ex) => <button key={ex.id} onClick={() => apply(ex)}>{ex.title}</button>)}
            </div>
          </div>
          <div className="field">
            <label>Image model</label>
            <div className="styles">
              {models.map((m) => (
                <button key={m.id} className={"style-card" + (model === m.id ? " active" : "")} disabled={m.available === false}
                        onClick={() => setModel(m.id)} title={m.available === false ? m.reason : m.description}>
                  <b>{m.label} <span className={"chip " + (m.tier === "fast" ? "accent" : "")} style={{ marginLeft: 4 }}>{m.tier === "fast" ? "fast" : "quality"}</span></b>
                  <span>{m.est_s < 60 ? `~${m.est_s} s` : `~${Math.round(m.est_s / 60)} min`} / image · {m.params?.steps} steps · {m.params?.width}px{m.available === false ? " · unavailable" : ""}</span>
                </button>
              ))}
            </div>
            {cur && <div className="small faint">{cur.description}</div>}
          </div>
          <div className="field">
            <label>Style</label>
            <div className="styles">
              {Object.entries(styles).map(([id, s]) => {
                const e = edits[id];
                const isEdited = !!e && !sameEdit(e, presetEdit(id, s));
                return (
                  <button key={id} className={"style-card" + (style === id ? " active" : "")} onClick={() => setStyle(id)} title={s.best_for}>
                    <b>{isEdited ? e.label || s.label : s.label || id}{isEdited && <span className="chip accent" style={{ marginLeft: 6 }}>edited</span>}</b>
                    <span>{isEdited ? e.style_clause.slice(0, 110) + (e.style_clause.length > 110 ? "…" : "") : s.description}</span>
                    {s.optimize_defaults && (
                      <span className="budget">{Number(isEdited ? e.target_triangles : s.optimize_defaults.target_triangles).toLocaleString()} tris · {isEdited ? e.texture_size : s.optimize_defaults.texture_size} px default</span>
                    )}
                  </button>
                );
              })}
              <button className={"style-card custom" + (style === "custom" ? " active" : "")} onClick={() => { setStyle("custom"); setEditing(true); }} title="Write your own style">
                <b><PenLine size={14} style={{ verticalAlign: -2, marginRight: 6 }} />{edits.custom?.style_clause?.trim() ? edits.custom.label || "Custom" : "Custom"}</b>
                <span>{edits.custom?.style_clause?.trim() ? edits.custom.style_clause.slice(0, 110) + (edits.custom.style_clause.length > 110 ? "…" : "") : "Describe the look yourself. Saved with your settings."}</span>
                <span className="budget">{Number(edits.custom?.target_triangles || 30000).toLocaleString()} tris · {edits.custom?.texture_size || 2048} px default</span>
              </button>
            </div>
            <div className="row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              {style !== "custom" && styles[style]?.best_for && <div className="small faint">Good for: {styles[style].best_for}</div>}
              <button className="btn ghost sm" onClick={() => setEditing(!editing)}>
                <PenLine size={13} /> {editing ? "Hide editor" : edited ? "Edit this style (edited)" : "Edit this style"}
              </button>
              {edited && style !== "custom" && <button className="btn ghost sm" onClick={resetEdit}>Reset to preset</button>}
            </div>
            {(editing || style === "custom") && (
              <div className="card pad stack custom-style" style={{ gap: 12 }}>
                <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                  <div className="field"><label>Style name</label><input className="input" value={curEdit.label} maxLength={40} onChange={(e) => setEdit({ label: e.target.value })} placeholder="My style" /></div>
                  <div className="field"><label>Background</label><input className="input" value={curEdit.background} maxLength={120} onChange={(e) => setEdit({ background: e.target.value })} placeholder="plain flat light grey studio background" /></div>
                </div>
                <div className="field">
                  <label>How it should look</label>
                  <textarea className="input" value={curEdit.style_clause} maxLength={600} onChange={(e) => setEdit({ style_clause: e.target.value })}
                    placeholder="e.g. chunky voxel-style game asset, blocky cubic forms, flat solid colours with slight shading, no rounded edges, readable silhouette" />
                  <div className="small faint">This sentence goes into the image prompt as the style. Keep it about the look, not the object; framing, lighting and the no-text rule are added automatically. Edits are saved in the app's settings.</div>
                </div>
                <div className="grid" style={{ gridTemplateColumns: "2fr 1fr 1fr", gap: 12 }}>
                  <div className="field"><label>Keep out</label><input className="input" value={curEdit.negative_extra} maxLength={300} onChange={(e) => setEdit({ negative_extra: e.target.value })} placeholder="e.g. photorealistic grime, tiny greebles" /></div>
                  <div className="field"><label>Triangle budget</label><input className="input" value={curEdit.target_triangles} inputMode="numeric" onChange={(e) => setEdit({ target_triangles: e.target.value.replace(/\D/g, "") })} placeholder="30000" /></div>
                  <div className="field"><label>Texture</label>
                    <select className="input" value={curEdit.texture_size} onChange={(e) => setEdit({ texture_size: e.target.value })}>
                      {[512, 1024, 2048, 4096].map((t) => <option key={t} value={String(t)}>{t} px</option>)}
                    </select>
                  </div>
                </div>
              </div>
            )}
          </div>
          <button className="btn ghost sm" onClick={() => setAdvanced(!advanced)} style={{ alignSelf: "flex-start" }}>
            {advanced ? <ChevronUp size={14} /> : <ChevronDown size={14} />} Advanced
          </button>
          {advanced && (
            <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <div className="field"><label>Materials</label><input className="input" value={materials} onChange={(e) => setMaterials(e.target.value)} placeholder="painted metal, brushed copper" /></div>
              <div className="field"><label>Palette (comma separated)</label><input className="input" value={palette} onChange={(e) => setPalette(e.target.value)} placeholder="teal, copper, warm grey" /></div>
              <div className="field"><label>Avoid</label><input className="input" value={negative} onChange={(e) => setNegative(e.target.value)} placeholder="extra things to keep out of the image" /></div>
              <div className="field"><label>Approx. height (m)</label><input className="input" value={height} onChange={(e) => setHeight(e.target.value)} placeholder="2.0" inputMode="decimal" /></div>
              <div className="field"><label>Seed (blank = random)</label><input className="input" value={seed} onChange={(e) => setSeed(e.target.value.replace(/\D/g, ""))} placeholder="random" /></div>
              <div className="field"><label>Title</label><input className="input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Shown in the library" /></div>
            </div>
          )}
        </div>
        <div className="card pad stack" style={{ gap: 14, position: "sticky", top: 24 }}>
          <div className="field">
            <label>Variations</label>
            <div className="row" style={{ gap: 6 }}>
              {[1, 2, 3, 4, 6, 8].map((n) => (
                <button key={n} className={"btn sm" + (variations === n ? " primary" : "")} onClick={() => setVariations(n)}>{n}</button>
              ))}
            </div>
            <div className="small muted">≈ {est} on the GPU with {cur?.label || "the selected model"}. Pick your favourites afterwards.</div>
          </div>
          <div className="sep" />
          <div className="small muted stack" style={{ gap: 4 }}>
            <div><Wand2 size={13} style={{ verticalAlign: -2 }} /> Each variation uses a different seed.</div>
            <div>Framing checks flag clipping or busy backgrounds as hints; you decide.</div>
            <div>Nothing is turned into 3D until you select an image.</div>
          </div>
          <button className="btn primary" style={{ justifyContent: "center", padding: "11px 14px" }} disabled={busy} onClick={submit}>
            <Sparkles size={16} /> {busy ? "Submitting…" : "Generate variations"}
          </button>
        </div>
      </div>
    </>
  );
}
