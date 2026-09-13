import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Dice5, Sparkles, ChevronDown, ChevronUp, Wand2 } from "lucide-react";
import { api, Example } from "../api";
import { useStore } from "../store";

interface StyleInfo { label: string; description?: string; optimize_defaults?: Record<string, any> }

export default function Create() {
  const nav = useNavigate();
  const { settings, toast } = useStore();
  const [examples, setExamples] = useState<Example[]>([]);
  const [styles, setStyles] = useState<Record<string, StyleInfo>>({});
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

  useEffect(() => {
    api.examples().then((r) => setExamples(r.items)).catch(() => {});
    api.capabilities().then((c) => setStyles(c.styles)).catch(() => {});
  }, []);
  useEffect(() => {
    if (settings) {
      setVariations(settings.default_variations);
      setStyle(settings.default_style);
    }
  }, [settings]);

  const apply = (ex: Example) => {
    setPrompt(ex.prompt);
    setStyle(ex.style);
    setHeight(ex.height_m ? String(ex.height_m) : "");
    setTitle(ex.title);
  };
  const surprise = () => examples.length && apply(examples[Math.floor(Math.random() * examples.length)]);
  const est = useMemo(() => Math.round(variations * 5), [variations]);

  const submit = async () => {
    if (prompt.trim().length < 3) return toast("Describe the object first", true);
    setBusy(true);
    try {
      const body: Record<string, unknown> = { prompt: prompt.trim(), style, variations, title: title || undefined };
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
          <p>Describe one object. You'll get {variations} image variations to choose from before anything is built in 3D.</p>
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
            <label>Style</label>
            <div className="styles">
              {Object.entries(styles).map(([id, s]) => (
                <button key={id} className={"style-card" + (style === id ? " active" : "")} onClick={() => setStyle(id)}>
                  <b>{s.label || id}</b>
                  <span>{s.description || (s.optimize_defaults ? `${s.optimize_defaults.target_triangles?.toLocaleString()} tris · ${s.optimize_defaults.texture_size}px` : "")}</span>
                </button>
              ))}
            </div>
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
            <div className="small muted">≈ {est} min on the GPU (about 5 min per image at full quality). Pick your favourites afterwards.</div>
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
