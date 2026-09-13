import { useEffect, useState } from "react";
import { Activity, Cpu, KeyRound, Moon, Sun, Monitor } from "lucide-react";
import { api, TOKEN_KEY } from "../api";
import { useStore } from "../store";

export default function SettingsPage() {
  const { settings, saveSettings, theme, setTheme, toast, loadSettings } = useStore();
  const [health, setHealth] = useState<any>(null);
  const [caps, setCaps] = useState<any>(null);
  const [tok, setTok] = useState<string>(() => {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
  });
  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth({ ok: false }));
    api.capabilities().then(setCaps).catch(() => {});
    loadSettings().catch(() => {});
  }, []);
  const save = async (p: Record<string, unknown>) => {
    try {
      await saveSettings(p);
      toast("Saved");
    } catch (e: any) {
      toast(e.message, true);
    }
  };
  const saveToken = () => {
    try {
      tok ? localStorage.setItem(TOKEN_KEY, tok) : localStorage.removeItem(TOKEN_KEY);
      toast("Token saved (reload to apply)");
    } catch {}
  };
  const g = caps?.gpu?.available ? caps.gpu.gpus[0] : null;
  return (
    <>
      <div className="page-head"><div><h1>Settings</h1><p>Defaults for new work, appearance, and service health.</p></div></div>
      <div className="grid wide" style={{ alignItems: "start" }}>
        <div className="card pad stack" style={{ gap: 14 }}>
          <h3 style={{ margin: 0 }}>Workflow</h3>
          {settings ? (
            <>
              <label className="switch" style={{ alignItems: "flex-start" }}>
                <input type="checkbox" checked={settings.auto_process} onChange={(e) => save({ auto_process: e.target.checked })} />
                <span><b>Process selected images automatically</b><div className="small muted">Off: selections wait in the queue for review until you press Process.</div></span>
              </label>
              <div className="field"><label>Default image model</label>
                <select className="input" value={settings.default_image_model} onChange={(e) => save({ default_image_model: e.target.value })}>
                  {(caps?.image_models || []).map((m: any) => <option key={m.id} value={m.id} disabled={m.available === false}>{m.label}{m.available === false ? " (unavailable)" : ""} · ~{m.est_s < 60 ? m.est_s + " s" : Math.round(m.est_s / 60) + " min"}/image</option>)}
                </select></div>
              <div className="field"><label>Default variations per prompt</label>
                <select className="input" value={settings.default_variations} onChange={(e) => save({ default_variations: Number(e.target.value) })}>{[1, 2, 3, 4, 6, 8].map((n) => <option key={n} value={n}>{n}</option>)}</select></div>
              <div className="field"><label>Default 3D quality</label>
                <select className="input" value={settings.default_quality} onChange={(e) => save({ default_quality: e.target.value })}><option value="balanced">Balanced (1024)</option><option value="quality">Quality (1536)</option></select></div>
              <div className="field"><label>Default style</label>
                <select className="input" value={settings.default_style} onChange={(e) => save({ default_style: e.target.value })}>{Object.entries(caps?.styles || {}).map(([id, s]: any) => <option key={id} value={id}>{s.label || id}</option>)}</select></div>
              <div className="row">
                <div className="field" style={{ flex: 1 }}><label>Default triangle budget</label>
                  <select className="input" value={settings.default_target_triangles} onChange={(e) => save({ default_target_triangles: Number(e.target.value) })}>{[3000, 6000, 8000, 12000, 20000, 30000, 50000].map((n) => <option key={n} value={n}>{n.toLocaleString()}</option>)}</select></div>
                <div className="field" style={{ flex: 1 }}><label>Default texture size</label>
                  <select className="input" value={settings.default_texture_size} onChange={(e) => save({ default_texture_size: Number(e.target.value) })}>{[512, 1024, 2048, 4096].map((n) => <option key={n} value={n}>{n} px</option>)}</select></div>
              </div>
            </>
          ) : <div className="skeleton" style={{ height: 120 }} />}
        </div>
        <div className="stack">
          <div className="card pad stack" style={{ gap: 10 }}>
            <h3 style={{ margin: 0 }}>Appearance</h3>
            <div className="row">
              {([["system", Monitor, "System"], ["light", Sun, "Light"], ["dark", Moon, "Dark"]] as const).map(([t, Icon, label]) => (
                <button key={t} className={"btn sm" + (theme === t ? " primary" : "")} onClick={() => setTheme(t)}><Icon size={14} /> {label}</button>
              ))}
            </div>
          </div>
          <div className="card pad stack" style={{ gap: 10 }}>
            <h3 style={{ margin: 0 }}><Activity size={15} style={{ verticalAlign: -2 }} /> Service</h3>
            {health ? (
              <dl className="kv">
                <dt>API</dt><dd>{health.ok ? <span className="chip ok">healthy</span> : <span className="chip danger">problem</span>} <span className="faint small">v{health.version}</span></dd>
                {health.runners && Object.entries(health.runners).map(([k, v]: any) => <><dt key={k}>{k} worker</dt><dd key={k + "v"}>{v.ok ? <span className="chip ok">{v.busy ? "busy" : "idle"}</span> : <span className="chip danger">down</span>}</dd></>)}
                {g && <><dt><Cpu size={13} style={{ verticalAlign: -2 }} /> GPU</dt><dd>{g.name} · {Math.round(g.used_mib / 1024)} / {Math.round(g.total_mib / 1024)} GB used</dd></>}
                <dt>Queue</dt><dd>{health.queue ? `${health.queue.running ? 1 : 0} running · ${health.queue.queued} queued · ${health.queue.held} held` : "–"}</dd>
                <dt>Models</dt><dd className="small">{caps?.dependencies ? "pinned, offline" : "–"} {caps?.features?.multiview_checkpoints_cached ? "· multi-view ckpts cached" : ""}</dd>
              </dl>
            ) : <div className="skeleton" style={{ height: 80 }} />}
          </div>
          <div className="card pad stack" style={{ gap: 10 }}>
            <h3 style={{ margin: 0 }}><KeyRound size={15} style={{ verticalAlign: -2 }} /> API token</h3>
            <div className="small muted">Only needed if the service was started with STUDIO_API_TOKEN (network exposure). Stored in this browser.</div>
            <div className="row"><input className="input" type="password" value={tok} onChange={(e) => setTok(e.target.value)} placeholder="not required on localhost" /><button className="btn" onClick={saveToken}>Save</button></div>
          </div>
        </div>
      </div>
    </>
  );
}
