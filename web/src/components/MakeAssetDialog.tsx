import { useState } from "react";
import { AlertTriangle } from "lucide-react";
import { api, Job } from "../api";
import { useReadiness } from "../hooks";
import { useGate, useT } from "../i18n";
import { useStore } from "../store";
import { Modal, Spinner } from "./ui";

interface Props {
  job: Job;             // the image job
  files: string[];      // selected candidate files
  onClose: () => void;
  onDone: (created: { job_id: string; status: string }[]) => void;
}

/** 3D settings for the selected variations. Submitting queues the job straight away; whether it starts at once or
 * waits for a click follows the auto_process setting, which is changed in Settings or on the Queue page. */
export default function MakeAssetDialog({ job, files, onClose, onDone }: Props) {
  const { settings, toast } = useStore();
  const t = useT();
  const gate = useGate();
  const { readiness } = useReadiness();
  // The 3D servers are needed the moment this is submitted; if they are down the API refuses, so say so here.
  const blocked = !!readiness && !readiness.create_asset.ready;
  const [quality, setQuality] = useState<"balanced" | "quality">(settings?.default_quality || "balanced");
  const [height, setHeight] = useState<string>(job.request.height_m ? String(job.request.height_m) : "");
  const [tris, setTris] = useState<number>(settings?.default_target_triangles || 20000);
  const [tex, setTex] = useState<number>(settings?.default_texture_size || 2048);
  const [lods, setLods] = useState(true);
  const [collision, setCollision] = useState(true);
  const [fallback, setFallback] = useState(true);
  // Starting immediately is the default and is not asked per job: the queue can still pause a job afterwards, and
  // the setting below is the one place that decides.
  const auto = settings?.auto_process ?? true;
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
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
      toast(t(auto ? "dialog.queued" : "dialog.held", { n: created.length }));
      onDone(created);
    } catch (e: any) {
      toast(e.message || String(e), true);
    } finally {
      setBusy(false);
    }
  };
  const est = files.length * (quality === "quality" ? 6 : 5.5);
  return (
    <Modal
      title={t("dialog.title", { n: files.length })}
      onClose={onClose}
      footer={
        <>
          <span className="small muted" style={{ marginRight: "auto" }}>
            {t("dialog.est", { n: Math.round(est) })}{auto ? "" : t("dialog.estReleased")}
          </span>
          <button className="btn" onClick={onClose}>{t("common.cancel")}</button>
          <button className="btn primary" disabled={busy || blocked} onClick={submit}>
            {busy && <Spinner />} {busy ? t("common.submitting") : auto ? t("dialog.queueStart") : t("dialog.addReview")}
          </button>
        </>
      }
    >
      <div className="stack loose">
        <div className="field">
          <label>{t("dialog.qualityLabel")}</label>
          <div className="row" style={{ gap: 8, flexWrap: "nowrap" }}>
            {(["balanced", "quality"] as const).map((q) => (
              <button key={q} className={"style-card" + (quality === q ? " active" : "")} style={{ flex: 1 }} onClick={() => setQuality(q)}>
                <b>{q === "balanced" ? t("dialog.balanced") : t("dialog.quality")}</b>
                <span>{q === "balanced" ? t("dialog.balancedHint") : t("dialog.qualityHint")}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="grid cols-2">
          <div className="field">
            <label>{t("dialog.height")}</label>
            <input className="input num" value={height} onChange={(e) => setHeight(e.target.value)} placeholder={t("dialog.heightPlaceholder")} inputMode="decimal" />
          </div>
          <div className="field">
            <label>{t("dialog.triangleBudget")}</label>
            <select className="input" value={tris} onChange={(e) => setTris(Number(e.target.value))}>
              {[3000, 6000, 8000, 12000, 20000, 30000, 50000, 100000].map((n) => <option key={n} value={n}>{n.toLocaleString()}</option>)}
            </select>
          </div>
          <div className="field">
            <label>{t("dialog.texture")}</label>
            <select className="input" value={tex} onChange={(e) => setTex(Number(e.target.value))}>
              {[512, 1024, 2048, 4096].map((n) => <option key={n} value={n}>{t("common.px", { n })}</option>)}
            </select>
          </div>
        </div>

        <div className="row" style={{ gap: 18 }}>
          <label className="switch"><input type="checkbox" checked={lods} onChange={(e) => setLods(e.target.checked)} /> {t("dialog.lods")}</label>
          <label className="switch"><input type="checkbox" checked={collision} onChange={(e) => setCollision(e.target.checked)} /> {t("dialog.collision")}</label>
          <label className="switch"><input type="checkbox" checked={fallback} onChange={(e) => setFallback(e.target.checked)} /> {t("dialog.fallback")}</label>
        </div>

        {!auto && (
          <div className="callout">
            <div className="callout-title">{t("dialog.holdReview")}</div>
            <div className="small muted">{t("dialog.holdHint")}</div>
          </div>
        )}

        {blocked && (
          <div className="callout danger">
            <div className="callout-title"><AlertTriangle size={14} /> {t("gate.blockedTitle")}</div>
            <ul>
              {gate(readiness.create_asset.problems).map((s, i) => <li key={i}>{s}</li>)}
            </ul>
          </div>
        )}
      </div>
    </Modal>
  );
}
