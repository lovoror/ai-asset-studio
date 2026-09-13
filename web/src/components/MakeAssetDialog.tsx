import { useEffect, useState } from "react";
import { api, Job } from "../api";
import { useStore } from "../store";
import { Modal } from "./ui";

interface Props {
  job: Job;             // the image job
  files: string[];      // selected candidate files
  onClose: () => void;
  onDone: (created: { job_id: string; status: string }[]) => void;
}

/** 3D settings for the selected variations + "start now / hold for review" (remembered as the auto_process setting). */
export default function MakeAssetDialog({ job, files, onClose, onDone }: Props) {
  const { settings, saveSettings, toast } = useStore();
  const [quality, setQuality] = useState<"balanced" | "quality">(settings?.default_quality || "balanced");
  const [height, setHeight] = useState<string>(job.request.height_m ? String(job.request.height_m) : "");
  const [tris, setTris] = useState<number>(settings?.default_target_triangles || 20000);
  const [tex, setTex] = useState<number>(settings?.default_texture_size || 2048);
  const [lods, setLods] = useState(true);
  const [collision, setCollision] = useState(true);
  const [fallback, setFallback] = useState(true);
  const [auto, setAuto] = useState<boolean>(!!settings?.auto_process);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (settings) setAuto(!!settings.auto_process);
  }, [settings]);

  const submit = async () => {
    setBusy(true);
    try {
      if (settings && settings.auto_process !== auto) await saveSettings({ auto_process: auto });
      const created = [];
      for (const f of files) {
        created.push(
          await api.createAssetJob({
            image_job_id: job.id, candidate: f, quality, hold: !auto,
            height_m: height ? Number(height) : undefined, target_triangles: tris, texture_size: tex,
            generate_lods: lods, generate_collision: collision, allow_quality_fallback: fallback,
          }),
        );
      }
      toast(auto ? `${created.length} asset job(s) queued` : `${created.length} asset job(s) held for review`);
      onDone(created);
    } catch (e: any) {
      toast(e.message || String(e), true);
    } finally {
      setBusy(false);
    }
  };
  const est = files.length * (quality === "quality" ? 6 : 5.5);
  return (
    <Modal title={`Make 3D from ${files.length} variation${files.length > 1 ? "s" : ""}`} onClose={onClose}>
      <div className="stack" style={{ gap: 14 }}>
        <div className="row" style={{ gap: 8 }}>
          {(["balanced", "quality"] as const).map((q) => (
            <button key={q} className={"style-card" + (quality === q ? " active" : "")} style={{ flex: 1 }} onClick={() => setQuality(q)}>
              <b>{q === "balanced" ? "Balanced" : "Quality"}</b>
              <span>{q === "balanced" ? "1024 reconstruction · ~4 min" : "1536 reconstruction · ~4–5 min, more VRAM"}</span>
            </button>
          ))}
        </div>
        <div className="row" style={{ alignItems: "stretch" }}>
          <div className="field" style={{ flex: 1 }}>
            <label>Height (m)</label>
            <input className="input" value={height} onChange={(e) => setHeight(e.target.value)} placeholder="e.g. 2.0" inputMode="decimal" />
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label>Triangle budget</label>
            <select className="input" value={tris} onChange={(e) => setTris(Number(e.target.value))}>
              {[3000, 6000, 8000, 12000, 20000, 30000, 50000, 100000].map((n) => <option key={n} value={n}>{n.toLocaleString()}</option>)}
            </select>
          </div>
          <div className="field" style={{ flex: 1 }}>
            <label>Texture</label>
            <select className="input" value={tex} onChange={(e) => setTex(Number(e.target.value))}>
              {[512, 1024, 2048, 4096].map((n) => <option key={n} value={n}>{n} px</option>)}
            </select>
          </div>
        </div>
        <div className="row" style={{ gap: 18 }}>
          <label className="switch"><input type="checkbox" checked={lods} onChange={(e) => setLods(e.target.checked)} /> LODs (50 % / 25 %)</label>
          <label className="switch"><input type="checkbox" checked={collision} onChange={(e) => setCollision(e.target.checked)} /> Collision hull</label>
          <label className="switch"><input type="checkbox" checked={fallback} onChange={(e) => setFallback(e.target.checked)} /> Allow quality fallback on OOM</label>
        </div>
        <div className="sep" />
        <label className="switch" style={{ alignItems: "flex-start" }}>
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
          <span>
            <b>{auto ? "Start processing now" : "Hold for review"}</b>
            <div className="small muted">{auto ? "Jobs go straight into the worker queue. Remembered as your default." : "Jobs wait in the Queue page until you press Process. Remembered as your default."}</div>
          </span>
        </label>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span className="small muted">≈ {Math.round(est)} min of GPU time{auto ? "" : " once released"}</span>
          <div className="row">
            <button className="btn" onClick={onClose}>Cancel</button>
            <button className="btn primary" disabled={busy} onClick={submit}>{busy ? "Submitting…" : auto ? "Queue & start" : "Add to review queue"}</button>
          </div>
        </div>
      </div>
    </Modal>
  );
}
