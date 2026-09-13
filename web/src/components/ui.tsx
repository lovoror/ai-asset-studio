import { ReactNode, useEffect } from "react";
import { X } from "lucide-react";
import { Job, stageLabel } from "../api";
import { useStore } from "../store";

export function Toasts() {
  const toasts = useStore((s) => s.toasts);
  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={"toast" + (t.err ? " err" : "")}>{t.text}</div>
      ))}
    </div>
  );
}

export function Modal({ title, onClose, children, width }: { title: string; onClose: () => void; children: ReactNode; width?: number }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal card pad" role="dialog" aria-modal="true" style={width ? { width: `min(${width}px, 100%)` } : undefined}>
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
          <h2 style={{ margin: 0, fontSize: 17 }}>{title}</h2>
          <button className="btn ghost icon" onClick={onClose} aria-label="Close"><X size={16} /></button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function StatusChip({ job }: { job: Job }) {
  const s = job.status;
  const cls = s === "completed" ? "ok" : s === "failed" ? "danger" : s === "running" ? "accent" : s === "cancelled" ? "warn" : s === "held" ? "info" : "";
  const text = s === "running" ? stageLabel[job.stage] || job.stage : s === "held" ? "Held" : s[0].toUpperCase() + s.slice(1);
  return <span className={"chip " + cls}>{text}</span>;
}

export function Progress({ job }: { job: Job }) {
  if (job.status !== "running") return null;
  const pct = Math.round((job.progress || 0) * 100);
  return (
    <div className={"progress" + (pct < 3 ? " indeterminate" : "")} title={`${pct}%`}>
      <span style={{ width: `${Math.max(pct, 3)}%` }} />
    </div>
  );
}

export function Empty({ icon, title, hint, action }: { icon?: ReactNode; title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="empty card">
      {icon}
      <h3>{title}</h3>
      {hint && <p className="muted">{hint}</p>}
      {action}
    </div>
  );
}

export const Skeleton = ({ h = 20, w = "100%" }: { h?: number; w?: string | number }) => <div className="skeleton" style={{ height: h, width: w }} />;
