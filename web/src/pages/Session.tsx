import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Box, Images, RefreshCw, Star, StarOff, Trash2, X } from "lucide-react";
import { api, Candidate, fmtAgo, withToken } from "../api";
import { useJob } from "../hooks";
import { useStore } from "../store";
import CandidateGrid from "../components/CandidateGrid";
import JobProgress from "../components/JobProgress";
import MakeAssetDialog from "../components/MakeAssetDialog";
import { Modal, Skeleton, StatusChip } from "../components/ui";

export default function Session() {
  const { id } = useParams();
  const nav = useNavigate();
  const { job, error, reload } = useJob(id, 20);
  const { toast } = useStore();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [dialog, setDialog] = useState(false);
  const [zoom, setZoom] = useState<Candidate | null>(null);
  const expected = job?.settings?.reference?.candidates || job?.request?.variations || 4;

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

  if (error) return <div className="card pad">Could not load: {error}</div>;
  if (!job) return <div className="stack"><Skeleton h={28} w={320} /><Skeleton h={300} /></div>;

  const toggle = (f: string) => {
    const n = new Set(selected);
    n.has(f) ? n.delete(f) : n.add(f);
    setSelected(n);
  };
  const more = async () => {
    try {
      const r = await api.createImageJob({ ...job.request, seed: undefined, title: job.title || undefined });
      toast("Generating more variations");
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
    if (!confirm("Archive this session? Its 3D assets stay in the library.")) return;
    await api.remove(job.id);
    nav("/library?kind=image");
  };

  return (
    <>
      <div className="page-head">
        <div>
          <Link to="/library?kind=image" className="small muted"><ArrowLeft size={13} style={{ verticalAlign: -2 }} /> Variations</Link>
          <h1 style={{ marginTop: 4 }}>{job.title || job.request.prompt}</h1>
          <p className="small">{job.request.style} · {expected} variations · {fmtAgo(job.created_at)} · <StatusChip job={job} /></p>
        </div>
        <div className="row">
          <button className="btn sm" onClick={fav}>{job.favorite ? <Star size={14} fill="currentColor" /> : <StarOff size={14} />} {job.favorite ? "Favourite" : "Favourite"}</button>
          <button className="btn sm" onClick={more}><RefreshCw size={14} /> More variations</button>
          {job.status === "running" && <button className="btn sm danger" onClick={() => api.cancel(job.id).then(reload)}><X size={14} /> Cancel</button>}
          <button className="btn sm ghost" onClick={remove} title="Archive"><Trash2 size={14} /></button>
        </div>
      </div>

      <div className="card pad" style={{ marginBottom: 16 }}>
        <div className="stack" style={{ gap: 10 }}>
          <div className="small muted" style={{ whiteSpace: "pre-wrap" }}>{job.request.prompt}</div>
          <JobProgress job={job} compact={job.status === "completed"} />
        </div>
      </div>

      {job.status === "failed" ? (
        <div className="card pad row" style={{ justifyContent: "space-between" }}>
          <span>This batch failed. {job.error && "message" in job.error ? String(job.error.message).slice(0, 200) : ""}</span>
          <button className="btn" onClick={() => api.retry(job.id).then(reload)}>Retry</button>
        </div>
      ) : (
        <CandidateGrid job={job} selected={selected} onToggle={toggle} expected={expected} onOpen={setZoom} />
      )}

      {(job.candidates?.length || 0) > 0 && (
        <div className="card sticky-bar">
          <div className="muted small">
            {selected.size ? `${selected.size} selected` : "Select the variations you want as 3D assets"} · <kbd>Ctrl</kbd>+<kbd>A</kbd> all · <kbd>Enter</kbd> zoom
          </div>
          <div className="row">
            {selected.size > 0 && <button className="btn sm" onClick={() => setSelected(new Set())}>Clear</button>}
            <button className="btn primary" disabled={!selected.size} onClick={() => setDialog(true)}><Box size={16} /> Make 3D ({selected.size})</button>
          </div>
        </div>
      )}

      {(job.children?.length || 0) > 0 && (
        <div style={{ marginTop: 22 }}>
          <h3 style={{ margin: "0 0 10px" }}><Images size={15} style={{ verticalAlign: -2 }} /> 3D assets from this session</h3>
          <div className="grid cards">
            {job.children!.map((c) => (
              <Link key={c.id} to={`/assets/${c.id}`} className="card lib-card">
                <div className="thumb">{c.thumb ? <img src={withToken(c.thumb)} alt="" /> : <Box size={28} className="faint" />}</div>
                <div className="body">
                  <div className="title">{c.title}</div>
                  <div className="row" style={{ justifyContent: "space-between" }}><StatusChip job={c} /><span className="small faint">{fmtAgo(c.created_at)}</span></div>
                </div>
              </Link>
            ))}
          </div>
        </div>
      )}

      {dialog && (
        <MakeAssetDialog job={job} files={[...selected]} onClose={() => setDialog(false)} onDone={() => { setDialog(false); setSelected(new Set()); reload(); }} />
      )}
      {zoom && (
        <Modal title={zoom.file} onClose={() => setZoom(null)} width={960}>
          <img src={withToken(zoom.url)} alt={zoom.file} style={{ width: "100%", borderRadius: 8 }} />
          <div className="row" style={{ justifyContent: "space-between", marginTop: 10 }}>
            <span className="small muted">seed {zoom.seed ?? "?"} · {Array.isArray(zoom.reasons) && zoom.reasons.length ? zoom.reasons.join("; ") : "framing checks passed"}</span>
            <button className="btn primary sm" onClick={() => { toggle(zoom.file); setZoom(null); }}>{selected.has(zoom.file) ? "Deselect" : "Select"}</button>
          </div>
        </Modal>
      )}
    </>
  );
}
