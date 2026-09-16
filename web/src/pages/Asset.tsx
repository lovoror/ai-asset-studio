import { Fragment, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { AlertTriangle, ArrowLeft, Download, FileJson, Images, Play, RotateCcw, ScrollText, Star, StarOff, Trash2, X, Wrench } from "lucide-react";
import { api, fmtInt, withToken } from "../api";
import { useJob } from "../hooks";
import { useFmt, useT } from "../i18n";
import { useStore } from "../store";
import JobProgress from "../components/JobProgress";
import Lightbox, { LightboxItem } from "../components/Lightbox";
import ModelViewer, { ViewerMap } from "../components/ModelViewer";
import { CardHead, Modal, Skeleton, StatusChip } from "../components/ui";

/** The baked maps the Blender stage always writes, keyed by the name the manifest uses for them. */
const MAP_FILES: Record<string, string> = {
  base_color: "textures/asset_baseColor.png",
  metallic: "textures/asset_metallicRoughness.png",
  roughness: "textures/asset_metallicRoughness.png",
  normal: "textures/asset_normal.png",
};

export default function Asset() {
  const { id } = useParams();
  const nav = useNavigate();
  const { job, error, reload } = useJob(id, 40);
  const { toast } = useStore();
  const t = useT();
  const { ago, dur, stage: stageText } = useFmt();
  const [variant, setVariant] = useState<string>("");
  const [log, setLog] = useState<{ stage: string; text: string } | null>(null);
  const [reopt, setReopt] = useState(false);
  const [tris, setTris] = useState(20000);
  const [tex, setTex] = useState(2048);
  const [zoom, setZoom] = useState<{ items: LightboxItem[]; index: number } | null>(null);

  useEffect(() => {
    if (job?.asset?.optimized && !variant) setVariant(job.asset.optimized);
    if (job?.request?.target_triangles) setTris(job.request.target_triangles);
    if (job?.request?.texture_size) setTex(job.request.texture_size);
  }, [job?.id, job?.asset?.optimized]);

  if (error) return <div className="card pad">{t("common.loadFailed", { msg: error })}</div>;
  if (!job) return <div className="stack"><Skeleton h={28} w={320} /><Skeleton h={520} /></div>;
  const s = job.summary;
  const a = job.asset;
  const art = (n: string) => withToken(`/v1/jobs/${job.id}/artifacts/${n}`);
  const variants: { label: string; file: string; tris?: number | null }[] = [];
  if (a?.optimized) variants.push({ label: t("asset.variantOptimized"), file: a.optimized, tris: s?.triangles });
  (a?.lods || []).forEach((l, i) => variants.push({ label: t("asset.variantLod", { n: i + 1 }), file: l.file, tris: l.triangles }));
  if (a?.collision) variants.push({ label: t("asset.variantCollision"), file: a.collision, tris: s?.collision_triangles });
  variants.push({ label: t("asset.variantMaster"), file: "master.glb", tris: s?.master_triangles });
  // The texture panel: one entry per baked map, with the file the manifest reports for it.
  const textureMaps: ViewerMap[] = [];
  for (const [key, state] of Object.entries(s?.maps || {})) {
    const path = MAP_FILES[key];
    if (!path || textureMaps.some((m) => m.url === art(path))) continue;
    textureMaps.push({ name: path.split("/").pop() || key, url: art(path), note: String(state) });
  }
  const showLog = async (stage: string) => setLog({ stage, text: await api.log(job.id, stage) });
  // Everything clickable opens in one lightbox list, so the previews can be browsed with ←/→.
  const previewFiles = [
    ...(a?.previews || []).filter((p) => !p.endsWith("contact_sheet.png")),
    "reference.png",
  ];
  const previewItems: LightboxItem[] = previewFiles.map((p) => ({
    url: art(p),
    caption: p.endsWith("reference.png") ? t("asset.referenceImage") : p.split("/").pop() || p,
  }));
  const textureItems: LightboxItem[] = textureMaps.map((m) => ({ url: m.url, caption: m.name, note: m.note }));
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
    if (!confirm(t("asset.deleteConfirm"))) return;
    await api.remove(job.id, true);
    nav("/library");
  };

  return (
    <>
      <div className="page-head">
        <div style={{ minWidth: 0 }}>
          <Link to="/library" className="crumb"><ArrowLeft size={13} /> {t("app.library")}</Link>
          <h1 style={{ marginTop: 6 }}>{job.title || job.request.prompt}</h1>
          <div className="row tight" style={{ marginTop: 8 }}>
            <StatusChip job={job} />
            <span className="chip">{job.request.style}</span>
            <span className="chip">{job.request.quality}</span>
            <span className="tiny faint">{ago(job.created_at)}</span>
            {job.parent_id && (
              <Link className="chip accent" to={`/sessions/${job.parent_id}`}>
                <Images size={12} /> {t("asset.fromVariations", { candidate: job.candidate })}
              </Link>
            )}
          </div>
        </div>
        <div className="row">
          <button className="btn sm" onClick={() => act(() => api.patch(job.id, { favorite: !job.favorite }))}>{job.favorite ? <Star size={14} fill="currentColor" /> : <StarOff size={14} />} {t("common.favourite")}</button>
          {job.status === "held" && <button className="btn sm primary" onClick={() => act(() => api.release(job.id), t("asset.processingStarted"))}><Play size={14} /> {t("asset.processNow")}</button>}
          {(job.status === "running" || job.status === "queued") && <button className="btn sm danger" onClick={() => act(() => api.cancel(job.id))}><X size={14} /> {t("common.cancel")}</button>}
          {(job.status === "failed" || job.status === "cancelled") && <button className="btn sm" onClick={() => act(() => api.retry(job.id), t("asset.retrying"))}><RotateCcw size={14} /> {t("common.retry")}</button>}
          {job.status === "completed" && <button className="btn sm" onClick={() => setReopt(true)}><Wrench size={14} /> {t("asset.reoptimise")}</button>}
          {job.status === "completed" && <a className="btn sm primary" href={withToken(`/v1/jobs/${job.id}/download.zip`)}><Download size={14} /> {t("asset.downloadZip")}</a>}
          <button className="btn sm ghost icon" onClick={remove} title={t("common.delete")} aria-label={t("common.delete")}><Trash2 size={14} /></button>
        </div>
      </div>

      {job.status !== "completed" && (
        <div className="card pad" style={{ marginBottom: 18 }}>
          <JobProgress job={job} />
          {job.status === "held" && <div className="small muted" style={{ marginTop: 8 }}>{t("asset.waitingReview")}</div>}
          {job.status === "running" && (
            <div className="row tight" style={{ marginTop: 12 }}>
              {["reference", "pixal3d", "blender"].map((st) => <button key={st} className="btn sm ghost" onClick={() => showLog(st)}><ScrollText size={13} /> {t("asset.logButton", { stage: stageText(st) })}</button>)}
            </div>
          )}
        </div>
      )}

      {job.status === "completed" && a && (
        <div className="split asset">
          <div className="stack loose">
            <ModelViewer
              variants={variants}
              file={variant}
              onSelectVariant={setVariant}
              urlFor={art}
              maps={textureMaps}
              onOpenImage={(url) => setZoom({ items: textureItems, index: Math.max(0, textureItems.findIndex((m) => m.url === url)) })}
            />
            {previewItems.length > 0 && (
              <div className="previews">
                {previewItems.map((item, i) => (
                  <img key={item.url} src={item.url} alt={item.caption || ""} title={item.caption}
                       loading="lazy" onClick={() => setZoom({ items: previewItems, index: i })} />
                ))}
              </div>
            )}
          </div>

          <div className="stack loose">
            <div className="card pad stack">
              <CardHead title={t("asset.heading")} />
              <dl className="kv rows">
                <dt>{t("asset.optimized")}</dt><dd>{job.request.texture_size || s?.maps ? t("asset.optimizedText", { tris: fmtInt(s?.triangles), tex: job.request.texture_size ?? "" }) : t("common.tris", { n: fmtInt(s?.triangles) })}</dd>
                <dt>{t("asset.master")}</dt><dd>{t("asset.masterText", { tris: fmtInt(s?.master_triangles), tex: job.request.master_texture_size ?? 4096, resolution: s?.resolution })}</dd>
                <dt>{t("asset.lods")}</dt><dd>{s?.lods?.length ? s.lods.map((l) => fmtInt(l.triangles)).join(" / ") : t("common.none")}</dd>
                <dt>{t("asset.collision")}</dt><dd>{s?.collision_triangles ? t("asset.collisionText", { n: s.collision_triangles }) : t("common.none")}</dd>
                <dt>{t("asset.maps")}</dt><dd className="small">{s?.maps ? Object.entries(s.maps).map(([k, v]) => `${k}: ${v}`).join(" · ") : "–"}</dd>
                <dt>{t("asset.reduction")}</dt><dd className="small">{s?.method}{s?.reduction?.result_error_relative != null ? t("asset.errorSuffix", { v: (s.reduction.result_error_relative * 100).toFixed(1) }) : ""}</dd>
                <dt>{t("asset.height")}</dt><dd>{job.request.height_m ? `${job.request.height_m} m` : t("asset.asGenerated")}</dd>
                <dt>{t("asset.validation")}</dt><dd>{s?.validation_ok ? <span className="chip ok">{t("asset.passed")}</span> : <span className="chip warn">{t("asset.seeManifest")}</span>}</dd>
              </dl>
            </div>

            <div className="card pad stack">
              <CardHead title={t("asset.timingsHeading")} />
              <dl className="kv rows">
                {Object.entries(s?.timings_s || {}).filter(([k]) => k !== "total").map(([k, v]) => (
                  <Fragment key={k}><dt>{stageText(k)}</dt><dd>{dur(v as number)}</dd></Fragment>
                ))}
                <dt>{t("asset.peakVram")}</dt><dd>{s?.vram_mib?.reference ? t("asset.gbImage", { v: (s.vram_mib.reference / 1024).toFixed(1) }) : ""}{s?.vram_mib?.pixal3d ? t("asset.gb3d", { v: (s.vram_mib.pixal3d / 1024).toFixed(1) }) : ""}</dd>
              </dl>
            </div>

            {(s?.warnings?.length || 0) > 0 && (
              <div className="callout warn">
                <div className="callout-title"><AlertTriangle size={14} /> {t("asset.warningsHeading")}</div>
                <ul>{s!.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
              </div>
            )}

            <div className="card pad stack">
              <CardHead title={t("asset.filesHeading")} />
              <div className="row tight">
                <a className="btn sm" href={art("manifest.json")} target="_blank" rel="noreferrer"><FileJson size={14} /> manifest.json</a>
                {["reference", "pixal3d", "blender"].map((st) => <button key={st} className="btn sm ghost" onClick={() => showLog(st)}><ScrollText size={13} /> {stageText(st)}</button>)}
              </div>
              <div className="small muted">{t("asset.openInBlender")} <code className="mono">python cli\assetctl.py view {job.id}</code></div>
            </div>
          </div>
        </div>
      )}

      {log && (
        <Modal title={t("asset.logButton", { stage: stageText(log.stage) })} onClose={() => setLog(null)} width={900}>
          <pre className="log">{log.text || t("common.emptyOutput")}</pre>
        </Modal>
      )}
      {zoom && (
        <Lightbox
          items={zoom.items}
          index={zoom.index}
          onIndex={(i) => setZoom({ items: zoom.items, index: i })}
          onClose={() => setZoom(null)}
        />
      )}
      {reopt && (
        <Modal
          title={t("asset.reoptTitle")}
          onClose={() => setReopt(false)}
          footer={
            <>
              <button className="btn" onClick={() => setReopt(false)}>{t("common.cancel")}</button>
              <button className="btn primary" onClick={() => { setReopt(false); act(() => api.retry(job.id, { from_stage: "blender", optimize: { target_triangles: tris, texture_size: tex } }), t("asset.reoptimising")); }}>{t("common.run")}</button>
            </>
          }
        >
          <div className="stack loose">
            <div className="small muted">{t("asset.reoptHint")}</div>
            <div className="grid cols-2">
              <div className="field"><label>{t("asset.triangleBudget")}</label>
                <select className="input" value={tris} onChange={(e) => setTris(Number(e.target.value))}>{[3000, 6000, 8000, 12000, 20000, 30000, 50000, 100000].map((n) => <option key={n} value={n}>{n.toLocaleString()}</option>)}</select></div>
              <div className="field"><label>{t("asset.texture")}</label>
                <select className="input" value={tex} onChange={(e) => setTex(Number(e.target.value))}>{[512, 1024, 2048, 4096].map((n) => <option key={n} value={n}>{t("common.px", { n })}</option>)}</select></div>
            </div>
          </div>
        </Modal>
      )}
    </>
  );
}
