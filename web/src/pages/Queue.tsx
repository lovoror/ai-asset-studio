import { useState } from "react";
import { Link } from "react-router-dom";
import { Box, Cpu, Pause, Play, PlayCircle, RotateCcw, X } from "lucide-react";
import { api, Job, fmtAgo, withToken } from "../api";
import { useLive } from "../hooks";
import { useStore } from "../store";
import JobProgress from "../components/JobProgress";
import { Empty, StatusChip } from "../components/ui";

export default function Queue() {
  const [items, setItems] = useState<Job[] | null>(null);
  const [gpu, setGpu] = useState<any>(null);
  const [recent, setRecent] = useState<Job[]>([]);
  const { settings, saveSettings, toast } = useStore();
  const load = async () => {
    try {
      const q = await api.queue();
      setItems(q.items);
      setGpu(q.gpu);
      const f = await api.library({ status: "failed", limit: 8, archived: false });
      setRecent(f.items);
    } catch (e: any) {
      toast(e.message, true);
    }
  };
  useLive(load, 5000);
  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      await load();
    } catch (e: any) {
      toast(e.message, true);
    }
  };
  const held = items?.filter((j) => j.status === "held") || [];
  const queued = items?.filter((j) => j.status === "queued") || [];
  const running = items?.filter((j) => j.status === "running") || [];
  const g = gpu?.available ? gpu.gpus[0] : null;

  const Item = ({ j }: { j: Job }) => (
    <div className="card q-item">
      {j.thumb ? <img className="thumb" src={withToken(j.thumb)} alt="" /> : <div className="thumb" style={{ display: "grid", placeItems: "center" }}><Box size={20} className="faint" /></div>}
      <div className="stack" style={{ gap: 6, minWidth: 0 }}>
        <div className="row" style={{ gap: 8 }}>
          <Link to={j.kind === "image" ? `/sessions/${j.id}` : `/assets/${j.id}`} style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{j.title || j.request.prompt}</Link>
          <StatusChip job={j} />
          <span className="chip">{j.kind === "image" ? `${j.settings?.reference?.candidates ?? "?"} variations` : `3D · ${j.request.quality}`}</span>
          {j.position && <span className="small faint">#{j.position}</span>}
        </div>
        <JobProgress job={j} compact />
        <div className="small faint">{fmtAgo(j.created_at)}{j.kind === "asset" && j.request.target_triangles ? ` · ${j.request.target_triangles.toLocaleString()} tris` : ""}</div>
      </div>
      <div className="row" style={{ gap: 6 }}>
        {j.status === "held" && <button className="btn sm primary" onClick={() => act(() => api.release(j.id))}><Play size={14} /> Process</button>}
        {j.status === "queued" && <button className="btn sm" onClick={() => act(() => api.hold(j.id))} title="Take out of the queue"><Pause size={14} /> Hold</button>}
        <button className="btn sm ghost" onClick={() => act(() => api.cancel(j.id))} title="Cancel"><X size={14} /></button>
      </div>
    </div>
  );

  return (
    <>
      <div className="page-head">
        <div><h1>Queue</h1><p>One job runs at a time on the GPU. Held items wait for you; queued items start automatically.</p></div>
        <div className="row">
          {g && <span className="chip" title={g.uuid}><Cpu size={13} /> {g.name} · {Math.round(g.used_mib / 1024)} / {Math.round(g.total_mib / 1024)} GB · {g.util_pct}%</span>}
          {settings && (
            <label className="switch"><input type="checkbox" checked={settings.auto_process} onChange={(e) => saveSettings({ auto_process: e.target.checked })} /> Process selections automatically</label>
          )}
        </div>
      </div>
      {items === null ? null : items.length === 0 && recent.length === 0 ? (
        <Empty icon={<PlayCircle size={36} className="faint" />} title="Nothing in the queue" hint="Generate variations on the Create page, then select the ones you want in 3D." action={<Link className="btn primary" to="/">Create</Link>} />
      ) : (
        <div className="stack" style={{ gap: 22 }}>
          {running.length > 0 && <section className="stack"><h3 style={{ margin: 0 }}>Running</h3>{running.map((j) => <Item key={j.id} j={j} />)}</section>}
          <section className="stack">
            <div className="row" style={{ justifyContent: "space-between" }}>
              <h3 style={{ margin: 0 }}>Held for review <span className="chip">{held.length}</span></h3>
              {held.length > 1 && <button className="btn sm" onClick={() => act(async () => { for (const j of held) await api.release(j.id); })}><Play size={14} /> Process all</button>}
            </div>
            {held.length ? held.map((j) => <Item key={j.id} j={j} />) : <div className="small muted">Nothing waiting for review.</div>}
          </section>
          <section className="stack">
            <h3 style={{ margin: 0 }}>Queued <span className="chip">{queued.length}</span></h3>
            {queued.length ? queued.map((j) => <Item key={j.id} j={j} />) : <div className="small muted">Queue is empty.</div>}
          </section>
          {recent.length > 0 && (
            <section className="stack">
              <h3 style={{ margin: 0 }}>Recently failed</h3>
              {recent.map((j) => (
                <div key={j.id} className="card q-item">
                  {j.thumb ? <img className="thumb" src={withToken(j.thumb)} alt="" /> : <div className="thumb" />}
                  <div className="stack" style={{ gap: 4, minWidth: 0 }}>
                    <Link to={j.kind === "image" ? `/sessions/${j.id}` : `/assets/${j.id}`} style={{ fontWeight: 600 }}>{j.title || j.request.prompt}</Link>
                    <div className="small" style={{ color: "var(--danger)" }}>{j.error && "message" in j.error ? String(j.error.message).slice(0, 160) : "failed"}</div>
                  </div>
                  <div className="row" style={{ gap: 6 }}>
                    <button className="btn sm" onClick={() => act(() => api.retry(j.id))}><RotateCcw size={14} /> Retry</button>
                    <button className="btn sm ghost" onClick={() => act(() => api.remove(j.id))}>Archive</button>
                  </div>
                </div>
              ))}
            </section>
          )}
        </div>
      )}
    </>
  );
}
