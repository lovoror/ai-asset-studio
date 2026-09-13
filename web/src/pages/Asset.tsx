import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Download, FileJson, Images, Play, RotateCcw, ScrollText, Star, StarOff, Trash2, X, Wrench } from "lucide-react";
import { api, fmtAgo, fmtDuration, fmtInt, withToken } from "../api";
import { useJob } from "../hooks";
import { useStore } from "../store";
import JobProgress from "../components/JobProgress";
import { Modal, Skeleton, StatusChip } from "../components/ui";

declare global {
  namespace JSX {
    interface IntrinsicElements {
      "model-viewer": any;
    }
  }
}

export default function Asset() {
  const { id } = useParams();
  const nav = useNavigate();
  const { job, error, reload } = useJob(id, 40);
  const { toast } = useStore();
  const [variant, setVariant] = useState<string>("");
  const [log, setLog] = useState<{ stage: string; text: string } | null>(null);
  const [reopt, setReopt] = useState(false);
  const [tris, setTris] = useState(20000);
  const [tex, setTex] = useState(2048);
  const [zoom, setZoom] = useState<string | null>(null);

  useEffect(() => {
    if (job?.asset?.optimized && !variant) setVariant(job.asset.optimized);
    if (job?.request?.target_triangles) setTris(job.request.target_triangles);
    if (job?.request?.texture_size) setTex(job.request.texture_size);
  }, [job?.id, job?.asset?.optimized]);

  if (error) return <div className="card pad">Could not load: {error}</div>;
  if (!job) return <div className="stack"><Skeleton h={28} w={320} /><Skeleton h={520} /></div>;
  const s = job.summary;
  const a = job.asset;
  const art = (n: string) => withToken(`/v1/jobs/${job.id}/artifacts/${n}`);
  const variants: { label: string; file: string; tris?: number | null }[] = [];
  if (a?.optimized) variants.push({ label: "Optimized", file: a.optimized, tris: s?.triangles });
  (a?.lods || []).forEach((l, i) => variants.push({ label: `LOD${i + 1}`, file: l.file, tris: l.triangles }));
  if (a?.collision) variants.push({ label: "Collision", file: a.collision, tris: s?.collision_triangles });
  variants.push({ label: "Master (large)", file: "master.glb", tris: s?.master_triangles });
  const showLog = async (stage: string) => setLog({ stage, text: await api.log(job.id, stage) });
  const act = async (fn: () => Promise<unknown>, msg?: string) => {
    try {
      await fn();
      if (msg) toast(msg);
      reload();
    } catch (e: any) {
      toast(e.message, true);
    }
  };
  const remove = async () => {
    if (!confirm("Delete this asset and all its files? This cannot be undone.")) return;
    await api.remove(job.id, true);
    nav("/library");
  };

  return (
    <>
      <div className="page-head">
        <div>
          <Link to="/library" className="small muted"><ArrowLeft size={13} style={{ verticalAlign: -2 }} /> Library</Link>
          <h1 style={{ marginTop: 4 }}>{job.title || job.request.prompt}</h1>
          <p className="small">
            {job.request.style} · {job.request.quality} · {fmtAgo(job.created_at)} · <StatusChip job={job} />
            {job.parent_id && <> · <Link to={`/sessions/${job.parent_id}`} style={{ color: "var(--accent)" }}><Images size={12} style={{ verticalAlign: -2 }} /> from variations ({job.candidate})</Link></>}
          </p>
        </div>
        <div className="row">
          <button className="btn sm" onClick={() => act(() => api.patch(job.id, { favorite: !job.favorite }))}>{job.favorite ? <Star size={14} fill="currentColor" /> : <StarOff size={14} />} Favourite</button>
          {job.status === "held" && <button className="btn sm primary" onClick={() => act(() => api.release(job.id), "Processing started")}><Play size={14} /> Process now</button>}
          {(job.status === "running" || job.status === "queued") && <button className="btn sm danger" onClick={() => act(() => api.cancel(job.id))}><X size={14} /> Cancel</button>}
          {(job.status === "failed" || job.status === "cancelled") && <button className="btn sm" onClick={() => act(() => api.retry(job.id), "Retrying")}><RotateCcw size={14} /> Retry</button>}
          {job.status === "completed" && <button className="btn sm" onClick={() => setReopt(true)}><Wrench size={14} /> Re-optimise</button>}
          {job.status === "completed" && <a className="btn sm primary" href={withToken(`/v1/jobs/${job.id}/download.zip`)}><Download size={14} /> Download zip</a>}
          <button className="btn sm ghost" onClick={remove} title="Delete"><Trash2 size={14} /></button>
        </div>
      </div>

      {job.status !== "completed" && (
        <div className="card pad" style={{ marginBottom: 16 }}>
          <JobProgress job={job} />
          {job.status === "held" && <div className="small muted" style={{ marginTop: 8 }}>Waiting for review. Press <b>Process now</b> or manage it on the Queue page.</div>}
          {job.status === "running" && (
            <div className="row" style={{ marginTop: 10 }}>
              {["reference", "pixal3d", "blender"].map((st) => <button key={st} className="btn sm ghost" onClick={() => showLog(st)}><ScrollText size={13} /> {st} log</button>)}
            </div>
          )}
        </div>
      )}

      {job.status === "completed" && a && (
        <div className="grid" style={{ gridTemplateColumns: "minmax(0, 3fr) minmax(300px, 2fr)", alignItems: "start" }}>
          <div className="stack">
            <div className="viewer">
              {variant && (
                <model-viewer
                  key={variant}
                  src={art(variant)}
                  camera-controls
                  auto-rotate
                  shadow-intensity="0.6"
                  exposure="1.0"
                  environment-image="neutral"
                  alt={job.title || "asset"}
                  style={{ width: "100%", height: "100%" }}
                />
              )}
            </div>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <div className="tabs" style={{ margin: 0, borderBottom: 0 }}>
                {variants.map((v) => (
                  <button key={v.file} className={variant === v.file ? "active" : ""} onClick={() => setVariant(v.file)}>
                    {v.label}{v.tris != null && <span className="faint"> · {fmtInt(v.tris)}</span>}
                  </button>
                ))}
              </div>
              <a className="btn sm" href={art(variant)} download><Download size={14} /> This file</a>
            </div>
            {a.previews && a.previews.length > 0 && (
              <div className="previews">
                {a.previews.filter((p) => !p.endsWith("contact_sheet.png")).map((p) => (
                  <img key={p} src={art(p)} alt={p} loading="lazy" onClick={() => setZoom(art(p))} />
                ))}
                {(job.thumb || true) && <img src={art("reference.png")} alt="reference" loading="lazy" onClick={() => setZoom(art("reference.png"))} title="Reference image" />}
              </div>
            )}
          </div>
          <div className="stack">
            <div className="card pad">
              <h3 style={{ margin: "0 0 10px" }}>Asset</h3>
              <dl className="kv">
                <dt>Optimized</dt><dd>{fmtInt(s?.triangles)} tris · {job.request.texture_size || s?.maps ? `${job.request.texture_size ?? ""}px maps` : ""}</dd>
                <dt>Master</dt><dd>{fmtInt(s?.master_triangles)} tris · 4096px · Pixal3D {s?.resolution}</dd>
                <dt>LODs</dt><dd>{s?.lods?.length ? s.lods.map((l) => fmtInt(l.triangles)).join(" / ") : "none"}</dd>
                <dt>Collision</dt><dd>{s?.collision_triangles ? `${s.collision_triangles} tris convex hull` : "none"}</dd>
                <dt>Maps</dt><dd className="small">{s?.maps ? Object.entries(s.maps).map(([k, v]) => `${k}: ${v}`).join(" · ") : "–"}</dd>
                <dt>Reduction</dt><dd className="small">{s?.method}{s?.reduction?.result_error_relative != null ? ` · error ${(s.reduction.result_error_relative * 100).toFixed(1)} %` : ""}</dd>
                <dt>Height</dt><dd>{job.request.height_m ? `${job.request.height_m} m` : "as generated"}</dd>
                <dt>Validation</dt><dd>{s?.validation_ok ? <span className="chip ok">passed</span> : <span className="chip warn">see manifest</span>}</dd>
              </dl>
            </div>
            <div className="card pad">
              <h3 style={{ margin: "0 0 10px" }}>Timings & memory</h3>
              <dl className="kv">
                {Object.entries(s?.timings_s || {}).filter(([k]) => k !== "total").map(([k, v]) => <><dt key={k + "k"}>{k}</dt><dd key={k + "v"}>{fmtDuration(v as number)}</dd></>)}
                <dt>Peak VRAM</dt><dd>{s?.vram_mib?.reference ? `${(s.vram_mib.reference / 1024).toFixed(1)} GB image` : ""}{s?.vram_mib?.pixal3d ? ` · ${(s.vram_mib.pixal3d / 1024).toFixed(1)} GB 3D` : ""}</dd>
              </dl>
            </div>
            {(s?.warnings?.length || 0) > 0 && (
              <div className="card pad">
                <h3 style={{ margin: "0 0 8px" }}>Warnings</h3>
                <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>{s!.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
              </div>
            )}
            <div className="card pad row" style={{ gap: 8 }}>
              <a className="btn sm" href={art("manifest.json")} target="_blank" rel="noreferrer"><FileJson size={14} /> manifest.json</a>
              {["reference", "pixal3d", "blender"].map((st) => <button key={st} className="btn sm ghost" onClick={() => showLog(st)}><ScrollText size={13} /> {st}</button>)}
            </div>
            <div className="card pad small muted">
              Open a labelled comparison in your local Blender: <code className="mono">python cli\assetctl.py view {job.id}</code>
            </div>
          </div>
        </div>
      )}

      {log && (
        <Modal title={`${log.stage} log`} onClose={() => setLog(null)} width={900}>
          <pre className="log">{log.text || "(empty)"}</pre>
        </Modal>
      )}
      {zoom && <Modal title="Preview" onClose={() => setZoom(null)} width={1000}><img src={zoom} alt="" style={{ width: "100%", borderRadius: 8 }} /></Modal>}
      {reopt && (
        <Modal title="Re-optimise from the master" onClose={() => setReopt(false)}>
          <div className="stack" style={{ gap: 12 }}>
            <div className="small muted">Re-runs only the Blender stage (≈ 1–2 min). The image and the 3D master are kept.</div>
            <div className="row">
              <div className="field" style={{ flex: 1 }}><label>Triangle budget</label>
                <select className="input" value={tris} onChange={(e) => setTris(Number(e.target.value))}>{[3000, 6000, 8000, 12000, 20000, 30000, 50000, 100000].map((n) => <option key={n} value={n}>{n.toLocaleString()}</option>)}</select></div>
              <div className="field" style={{ flex: 1 }}><label>Texture</label>
                <select className="input" value={tex} onChange={(e) => setTex(Number(e.target.value))}>{[512, 1024, 2048, 4096].map((n) => <option key={n} value={n}>{n} px</option>)}</select></div>
            </div>
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button className="btn" onClick={() => setReopt(false)}>Cancel</button>
              <button className="btn primary" onClick={() => { setReopt(false); act(() => api.retry(job.id, { from_stage: "blender", optimize: { target_triangles: tris, texture_size: tex } }), "Re-optimising"); }}>Run</button>
            </div>
          </div>
        </Modal>
      )}
    </>
  );
}
