import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { AlertTriangle, ArrowLeft, Box, Images, RefreshCw, Star, StarOff, Trash2, X } from "lucide-react";
import { api, withToken } from "../api";
import { useJob } from "../hooks";
import { useFmt, useT } from "../i18n";
import { useStore } from "../store";
import CandidateGrid from "../components/CandidateGrid";
import JobProgress from "../components/JobProgress";
import Lightbox, { LightboxItem } from "../components/Lightbox";
import MakeAssetDialog from "../components/MakeAssetDialog";
import { Skeleton, StatusChip } from "../components/ui";

export default function Session() {
  const { id } = useParams();
  const nav = useNavigate();
  const { job, error, reload } = useJob(id, 20);
  const { toast } = useStore();
  const t = useT();
  const { ago } = useFmt();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [dialog, setDialog] = useState(false);
  const [zoomAt, setZoomAt] = useState<number | null>(null);
  const expected = job?.settings?.reference?.candidates || job?.request?.variations || 4;
  // The lightbox browses whatever the grid is showing, in the same order.
  const candidateList = job?.candidates || [];
  const candidateItems: LightboxItem[] = candidateList.map((c) => ({
    // full resolution: the grid uses the small thumb, the lightbox is where you look closely
    url: withToken(c.url),
    preview: c.thumb ? withToken(c.thumb) : undefined,
    caption: c.file,
    note: [
      c.seed != null ? t("session.seedLabel", { n: c.seed }) : "",
      Array.isArray(c.reasons) && c.reasons.length ? c.reasons.join("; ") : t("cand.framingPassed"),
    ].filter(Boolean).join(" · "),
  }));

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "a" && (e.ctrlKey || e.metaKey) && job?.candidates?.length) {
        e.preventDefault();
        setSelected(new Set(job.candidates.filter((c) => !c.partial).map((c) => c.file)));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [job]);

  if (error) return <div className="card pad">{t("common.loadFailed", { msg: error })}</div>;
  if (!job) return <div className="stack"><Skeleton h={28} w={320} /><Skeleton h={300} /></div>;

  const toggle = (f: string) => {
    const n = new Set(selected);
    n.has(f) ? n.delete(f) : n.add(f);
    setSelected(n);
  };
  const more = async () => {
    try {
      const r = await api.createImageJob({ ...job.request, seed: undefined, title: job.title || undefined });
      toast(t("session.generatingMore"));
      nav(`/sessions/${r.job_id}`);
    } catch (e: any) {
      toast(e.message, true);
    }
  };
  const fav = async () => {
    await api.patch(job.id, { favorite: !job.favorite });
    reload();
  };
  const remove = async () => {
    if (!confirm(t("session.archiveConfirm"))) return;
    await api.remove(job.id);
    nav("/library?kind=image");
  };

  return (
    <>
      <div className="page-head">
        <div style={{ minWidth: 0 }}>
          <Link to="/library?kind=image" className="crumb"><ArrowLeft size={13} /> {t("app.variations")}</Link>
          <h1 style={{ marginTop: 6 }}>{job.title || job.request.prompt}</h1>
          <div className="row tight" style={{ marginTop: 8 }}>
            <StatusChip job={job} />
            <span className="chip">{job.settings?.reference?.model || job.request.model || "qwen-image-2512"}</span>
            <span className="chip">{job.request.style}</span>
            <span className="chip">{expected} {t("app.variations")}</span>
            <span className="tiny faint">{ago(job.created_at)}</span>
          </div>
        </div>
        <div className="row">
          <button className="btn sm" onClick={fav}>{job.favorite ? <Star size={14} fill="currentColor" /> : <StarOff size={14} />} {t("common.favourite")}</button>
          <button className="btn sm" onClick={more}><RefreshCw size={14} /> {t("session.moreVariations")}</button>
          {job.status === "running" && <button className="btn sm danger" onClick={() => api.cancel(job.id).then(reload)}><X size={14} /> {t("common.cancel")}</button>}
          <button className="btn sm ghost icon" onClick={remove} title={t("session.archive")} aria-label={t("session.archive")}><Trash2 size={14} /></button>
        </div>
      </div>

      <div className="card pad" style={{ marginBottom: 18 }}>
        <div className="stack" style={{ gap: 12 }}>
          <div className="small muted" style={{ whiteSpace: "pre-wrap" }}>{job.request.prompt}</div>
          <JobProgress job={job} compact={job.status === "completed"} />
        </div>
      </div>

      {job.status === "failed" ? (
        <div className="callout danger">
          <div className="callout-title"><AlertTriangle size={14} /> {t("session.failedBatch")}</div>
          {job.error && "message" in job.error && <div className="small">{String(job.error.message).slice(0, 200)}</div>}
          <div className="row end" style={{ marginTop: 4 }}>
            <button className="btn sm" onClick={() => api.retry(job.id).then(reload)}>{t("common.retry")}</button>
          </div>
        </div>
      ) : (
        <CandidateGrid job={job} selected={selected} onToggle={toggle} expected={expected} onOpen={setZoomAt} />
      )}

      {(job.candidates?.length || 0) > 0 && (
        <div className="card sticky-bar">
          <div className="muted small">
            {selected.size ? t("session.selectedCount", { n: selected.size }) : t("session.selectPrompt")} · <kbd>Ctrl</kbd>+<kbd>A</kbd> {t("session.all")} · {t("cand.open")}
          </div>
          <div className="row tight">
            {selected.size > 0 && <button className="btn sm" onClick={() => setSelected(new Set())}>{t("common.clear")}</button>}
            <button className="btn primary" disabled={!selected.size} onClick={() => setDialog(true)}><Box size={16} /> {t("session.make3d", { n: selected.size })}</button>
          </div>
        </div>
      )}

      {(job.children?.length || 0) > 0 && (
        <section style={{ marginTop: 26 }}>
          <div className="section-head">
            <h2><Images size={15} /> {t("session.assetsFromSession")}</h2>
          </div>
          <div className="grid cards">
            {job.children!.map((c) => (
              <Link key={c.id} to={`/assets/${c.id}`} className="card lib-card hover">
                <div className="thumb">{c.thumb ? <img src={withToken(c.thumb)} alt="" /> : <Box size={28} className="faint" />}</div>
                <div className="body">
                  <div className="title">{c.title}</div>
                  <div className="row between"><StatusChip job={c} /><span className="small faint">{ago(c.created_at)}</span></div>
                </div>
              </Link>
            ))}
          </div>
        </section>
      )}

      {dialog && (
        <MakeAssetDialog job={job} files={[...selected]} onClose={() => setDialog(false)} onDone={() => { setDialog(false); setSelected(new Set()); reload(); }} />
      )}
      {zoomAt !== null && (
        <Lightbox
          items={candidateItems}
          index={zoomAt}
          onIndex={setZoomAt}
          onClose={() => setZoomAt(null)}
          action={(i) => {
            const c = candidateList[i];
            if (!c || c.partial) return null;
            return (
              <button className="btn sm" onClick={() => toggle(c.file)}>
                {selected.has(c.file) ? t("common.deselect") : t("common.select")}
              </button>
            );
          }}
        />
      )}
    </>
  );
}
