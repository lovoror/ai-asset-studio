import { useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, Box, Cpu, Pause, Play, PlayCircle, RotateCcw, X } from "lucide-react";
import { api, Job, withToken } from "../api";
import { useLive, useReadiness } from "../hooks";
import { useFmt, useGate, useT } from "../i18n";
import { useStore } from "../store";
import JobProgress from "../components/JobProgress";
import { Empty, Skeleton, StatusChip } from "../components/ui";

export default function Queue() {
  const [items, setItems] = useState<Job[] | null>(null);
  const [gpu, setGpu] = useState<any>(null);
  const [recent, setRecent] = useState<Job[]>([]);
  const { settings, saveSettings, toast } = useStore();
  const t = useT();
  const { ago } = useFmt();
  const gate = useGate();
  const { readiness } = useReadiness();
  // Releasing a held job starts it, so it is gated exactly like a new submission (the API enforces the same).
  const gateFor = (j: Job) => (j.kind === "image" ? readiness?.create_image : readiness?.create_asset);
  const load = async () => {
    try {
      const q = await api.queue();
      setItems(q.items);
      setGpu(q.gpu);
      const f = await api.library({ status: "failed", limit: 8, archived: false });
      setRecent(f.items);
    } catch (e: any) {
      toast(e.message, true);
      setItems([]);
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
      {j.thumb ? <img className="thumb" src={withToken(j.thumb)} alt="" />
               : <div className="thumb" style={{ display: "grid", placeItems: "center" }}><Box size={18} className="faint" /></div>}
      <div className="stack tight" style={{ minWidth: 0 }}>
        <div className="row tight">
          <Link to={j.kind === "image" ? `/sessions/${j.id}` : `/assets/${j.id}`} className="bold truncate">{j.title || j.request.prompt}</Link>
          <StatusChip job={j} />
          <span className="chip">{j.kind === "image" ? t("queue.variations", { n: j.settings?.reference?.candidates ?? "?" }) : t("queue.asset", { q: j.request.quality })}</span>
          {j.position && <span className="tiny faint num">#{j.position}</span>}
        </div>
        <JobProgress job={j} compact />
        <div className="tiny faint">{ago(j.created_at)}{j.kind === "asset" && j.request.target_triangles ? t("queue.trisSuffix", { n: j.request.target_triangles.toLocaleString() }) : ""}</div>
      </div>
      <div className="row tight q-actions">
        {j.status === "held" && (
          <button className="btn sm primary" disabled={!gateFor(j)?.ready}
                  title={gateFor(j)?.ready ? undefined : gate(gateFor(j)?.problems || []).join(" · ")}
                  onClick={() => act(() => api.release(j.id))}>
            <Play size={14} /> {t("queue.process")}
          </button>
        )}
        {j.status === "queued" && <button className="btn sm" onClick={() => act(() => api.hold(j.id))} title={t("queue.holdTitle")}><Pause size={14} /> {t("queue.hold")}</button>}
        <button className="btn sm ghost icon" onClick={() => act(() => api.cancel(j.id))} title={t("common.cancel")} aria-label={t("common.cancel")}><X size={14} /></button>
      </div>
    </div>
  );

  return (
    <>
      <div className="page-head">
        <div><h1>{t("queue.title")}</h1><p>{t("queue.subtitle")}</p></div>
        <div className="row">
          {g && <span className="chip" title={g.uuid}><Cpu size={13} /> {g.name} · {Math.round(g.used_mib / 1024)} / {Math.round(g.total_mib / 1024)} GB · {g.util_pct}%</span>}
          {settings && (
            <label className="switch"><input type="checkbox" checked={settings.auto_process} onChange={(e) => saveSettings({ auto_process: e.target.checked })} /> {t("queue.processAuto")}</label>
          )}
        </div>
      </div>

      {readiness && !readiness.create_image.ready && (
        <div className="callout danger" style={{ marginBottom: 18 }}>
          <div className="callout-title"><AlertTriangle size={14} /> {t("gate.blockedTitle")}</div>
          <ul>
            {gate([...readiness.create_image.problems, ...readiness.create_asset.problems]).map((s, i) => <li key={i}>{s}</li>)}
          </ul>
          <Link className="link small" to="/settings">{t("settings.backends")} →</Link>
        </div>
      )}

      {items === null ? (
        <div className="stack" style={{ gap: 10 }}>
          {Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} h={80} />)}
        </div>
      ) : items.length === 0 && recent.length === 0 ? (
        <Empty icon={<PlayCircle size={24} />} title={t("queue.empty")} hint={t("queue.emptyHint")} action={<Link className="btn primary" to="/">{t("app.create")}</Link>} />
      ) : (
        <div className="stack" style={{ gap: 24 }}>
          {running.length > 0 && (
            <section className="stack">
              <div className="section-head"><h2>{t("queue.running")}</h2></div>
              {running.map((j) => <Item key={j.id} j={j} />)}
            </section>
          )}
          <section className="stack">
            <div className="section-head">
              <h2>{t("queue.held")} <span className="count">{held.length}</span></h2>
              {held.length > 1 && <button className="btn sm" onClick={() => act(async () => { for (const j of held) await api.release(j.id); })}><Play size={14} /> {t("queue.processAll")}</button>}
            </div>
            {held.length ? held.map((j) => <Item key={j.id} j={j} />) : <div className="small muted">{t("queue.nothingReview")}</div>}
          </section>
          <section className="stack">
            <div className="section-head"><h2>{t("queue.queued")} <span className="count">{queued.length}</span></h2></div>
            {queued.length ? queued.map((j) => <Item key={j.id} j={j} />) : <div className="small muted">{t("queue.queueEmpty")}</div>}
          </section>
          {recent.length > 0 && (
            <section className="stack">
              <div className="section-head"><h2>{t("queue.recentlyFailed")}</h2></div>
              {recent.map((j) => (
                <div key={j.id} className="card q-item">
                  {j.thumb ? <img className="thumb" src={withToken(j.thumb)} alt="" /> : <div className="thumb" />}
                  <div className="stack tight" style={{ minWidth: 0 }}>
                    <Link to={j.kind === "image" ? `/sessions/${j.id}` : `/assets/${j.id}`} className="bold truncate">{j.title || j.request.prompt}</Link>
                    <div className="small" style={{ color: "var(--danger)" }}>{j.error && "message" in j.error ? String(j.error.message).slice(0, 160) : t("queue.failed")}</div>
                  </div>
                  <div className="row tight q-actions">
                    <button className="btn sm" onClick={() => act(() => api.retry(j.id))}><RotateCcw size={14} /> {t("queue.retry")}</button>
                    <button className="btn sm ghost" onClick={() => act(() => api.remove(j.id))}>{t("common.archive")}</button>
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
